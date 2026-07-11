"""Parse, validate and canonicalize a WireGuard (wg-quick) ``.conf``.

The wg-quick format is provider-agnostic, so one parser serves every provider
that hands out a WireGuard config (Proton, self-hosted, ...). We
read only the keys sing-box can act on and ignore wg-quick-only directives
(``PostUp``, ``Table``, ``FwMark``, ...). An imported ``.conf`` is a snapshot of
*one* peer (one server); to roam locations you re-import another file — a
second ``[Peer]`` section is rejected outright rather than silently merged
into a hybrid of two servers.

Validation is strict and aggregated: every problem in the file is reported in
one :class:`ConfError`, each annotated with its source line, so a bad config
fails at import with actionable messages instead of surfacing later as an
opaque ``sing-box check`` rejection.

A note on the endpoint: we keep whatever the file provides. A hostname is
re-resolved by sing-box on each reconcile (so it survives provider IP changes); a
literal IP is pinned and will fail if that server is ever retired.
"""

from __future__ import annotations

import base64
import ipaddress
import re

from alle.credentials import mask

WG_KEEPALIVE = 25
DEFAULT_ALLOWED_IPS = ["0.0.0.0/0", "::/0"]

# Keys wg-quick treats as accumulating lists: a repeated line appends. Every
# other key is scalar, and a duplicate is an error (which server did you mean?).
_LIST_KEYS = {"address", "allowedips", "dns"}

# RFC 1123 hostname: dot-separated labels of letters/digits/hyphens, no
# leading/trailing hyphen, each label ≤ 63 chars, total ≤ 253.
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$",
    re.IGNORECASE,
)


class ConfError(Exception):
    """The pasted/loaded text is not a usable WireGuard config.

    May aggregate several problems — one per line, each prefixed with the
    source line it occurs on where one is known.
    """


def valid_host(host: str) -> bool:
    """True for an IP literal (v4/v6) or an RFC 1123 hostname."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return bool(_HOST_RE.match(host))
    return True


def _sections(text: str) -> tuple[dict, dict, list[str]]:
    """Split a wg-quick file into ``[Interface]``/``[Peer]`` maps of
    ``key -> (value, line)``, collecting structural errors.

    Exactly one ``[Interface]`` and one ``[Peer]`` are supported (commercial
    configs ship exactly one server): duplicate sections, unknown sections,
    non-``Key = value`` lines, keys before any section, and duplicate scalar
    keys are all reported with their line numbers. Repeated list keys
    (Address, AllowedIPs, DNS) append, matching wg-quick.
    """
    section: str | None = None
    interface: dict[str, tuple[str, int]] = {}
    peer: dict[str, tuple[str, int]] = {}
    errors: list[str] = []
    seen_sections: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip().lower()
            if name not in ("interface", "peer"):
                errors.append(f"line {lineno}: unsupported section [{name}]")
                section = "ignored"
                continue
            if name in seen_sections:
                errors.append(
                    f"line {lineno}: duplicate [{name.title()}] section — a "
                    ".conf describes exactly one server (first on line "
                    f"{seen_sections[name]})"
                )
                section = "ignored"  # never merge two servers into a hybrid
                continue
            seen_sections[name] = lineno
            section = name
            continue
        if "=" not in line:
            errors.append(f"line {lineno}: not a 'Key = value' line")
            continue
        key, value = line.split("=", 1)  # base64 padding keeps '=' in the value
        key, value = key.strip().lower(), value.strip()
        if section == "ignored":
            continue  # under a section already reported above
        if section is None:
            errors.append(
                f"line {lineno}: {key} appears before any [Interface]/[Peer] section"
            )
            continue
        target = interface if section == "interface" else peer
        if key not in target:
            target[key] = (value, lineno)
        elif key in _LIST_KEYS:
            target[key] = (f"{target[key][0]}, {value}", target[key][1])
        else:
            errors.append(
                f"line {lineno}: duplicate {key} — already set on line {target[key][1]}"
            )
    return interface, peer, errors


def _csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    """``host:port`` → ``(host, port)``, handling bracketed IPv6 literals."""
    if endpoint.startswith("["):  # [2001:db8::1]:51820
        host, _, port = endpoint[1:].partition("]:")
    else:
        host, _, port = endpoint.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) <= 65535:
        raise ConfError(f"endpoint {endpoint!r} is not host:port")
    if not valid_host(host):
        raise ConfError(f"endpoint host {host!r} is not an IP address or hostname")
    return host, int(port)


def _require_wg_key(name: str, value: str) -> None:
    """WireGuard keys are exactly 32 bytes, base64 (44 chars ending ``=``).

    Checked at import time so a truncated paste or a mixed-up value fails with
    a message naming the field, instead of surfacing later as an opaque
    ``sing-box check`` rejection of the generated config.

    The decode→re-encode round trip requires the *canonical* form, matching
    WireGuard's own key parser — and unlike ``validate=True`` alone it behaves
    identically on every Python (only 3.11+ rejects excess padding natively).
    """
    try:
        decoded = base64.b64decode(value, validate=True)
        ok = len(decoded) == 32 and base64.b64encode(decoded).decode() == value
    except ValueError:
        ok = False
    if not ok:
        raise ConfError(f"{name} is not a valid WireGuard key (32 bytes, base64)")


def check_addresses(entries: list[str]) -> str | None:
    """The first entry that is not an IP interface address (``10.5.0.2/32``,
    bare IP allowed), or ``None`` when all parse."""
    for entry in entries:
        try:
            ipaddress.ip_interface(entry)
        except ValueError:
            return entry
    return None


def check_cidrs(entries: list[str]) -> str | None:
    """The first entry that is not a CIDR (host bits forgiven, bare IP
    allowed), or ``None`` when all parse."""
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            return entry
    return None


def parse(text: str) -> dict:
    """Validate a wg-quick config and return the fields sing-box needs.

    Strict and aggregated: every problem — structural or per-field — is
    collected and raised as one :class:`ConfError`, each line-annotated.
    """
    interface, peer, errors = _sections(text)

    def field(section: dict, key: str) -> tuple[str | None, int | None]:
        entry = section.get(key)
        return (entry[0], entry[1]) if entry else (None, None)

    private_key, priv_line = field(interface, "privatekey")
    address, addr_line = field(interface, "address")
    public_key, pub_line = field(peer, "publickey")
    endpoint, ep_line = field(peer, "endpoint")
    missing = [
        name
        for name, val in (
            ("[Interface] PrivateKey", private_key),
            ("[Interface] Address", address),
            ("[Peer] PublicKey", public_key),
            ("[Peer] Endpoint", endpoint),
        )
        if not val
    ]
    if missing:
        errors.append("missing required field(s): " + ", ".join(missing))

    def check(line: int | None, fn, *args) -> None:
        try:
            fn(*args)
        except ConfError as e:
            errors.append(f"line {line}: {e}" if line else str(e))

    if private_key:
        check(priv_line, _require_wg_key, "[Interface] PrivateKey", private_key)
    if public_key:
        check(pub_line, _require_wg_key, "[Peer] PublicKey", public_key)
    preshared, psk_line = field(peer, "presharedkey")
    if preshared:
        check(psk_line, _require_wg_key, "[Peer] PresharedKey", preshared)

    addresses = _csv(address) if address else []
    if address:
        bad = check_addresses(addresses)
        if bad is not None:
            errors.append(
                f"line {addr_line}: Address {bad!r} is not an IP interface address"
            )

    host, port = "", 0
    if endpoint:
        try:
            host, port = _split_endpoint(endpoint)
        except ConfError as e:
            errors.append(f"line {ep_line}: {e}")

    allowed_raw, allowed_line = field(peer, "allowedips")
    allowed = _csv(allowed_raw) if allowed_raw else []
    if allowed:
        bad = check_cidrs(allowed)
        if bad is not None:
            errors.append(f"line {allowed_line}: AllowedIPs {bad!r} is not a CIDR")

    # DNS is not acted on (sing-box owns resolution), but garbage in it means
    # a mangled file — validate rather than silently carry it. wg-quick allows
    # both IP resolvers and search-domain names.
    dns_raw, dns_line = field(interface, "dns")
    for entry in _csv(dns_raw) if dns_raw else []:
        if not valid_host(entry):
            errors.append(
                f"line {dns_line}: DNS {entry!r} is not an IP address or domain"
            )

    keepalive_raw, ka_line = field(peer, "persistentkeepalive")
    keepalive = WG_KEEPALIVE
    if keepalive_raw is not None:
        # never silently replace a malformed value with the default — the file
        # said something, and what it said must be what applies
        if keepalive_raw.isdigit() and 0 <= int(keepalive_raw) <= 65535:
            keepalive = int(keepalive_raw)
        else:
            errors.append(
                f"line {ka_line}: PersistentKeepalive {keepalive_raw!r} must be "
                "a number of seconds (0-65535)"
            )

    if errors:
        raise ConfError("\n".join(errors))

    return {
        "private_key": private_key,
        "address": addresses,
        "peer": {
            "public_key": public_key,
            "endpoint_host": host,
            "endpoint_port": port,
            "preshared_key": preshared or None,
            "allowed_ips": allowed or list(DEFAULT_ALLOWED_IPS),
            "keepalive": keepalive,
        },
    }


def canonical(parsed: dict, *, masked: bool = False) -> str:
    """Re-emit a normalized ``.conf``. With ``masked=True`` the secret keys are
    shown only as a preview (for confirmation echo), never in full."""
    p = parsed["peer"]
    priv = mask(parsed["private_key"]) if masked else parsed["private_key"]
    lines = [
        "[Interface]",
        f"PrivateKey = {priv}",
        f"Address = {', '.join(parsed['address'])}",
        "",
        "[Peer]",
        f"PublicKey = {p['public_key']}",
    ]
    if p["preshared_key"]:
        psk = mask(p["preshared_key"]) if masked else p["preshared_key"]
        lines.append(f"PresharedKey = {psk}")
    lines += [
        f"Endpoint = {p['endpoint_host']}:{p['endpoint_port']}",
        f"AllowedIPs = {', '.join(p['allowed_ips'])}",
        f"PersistentKeepalive = {p['keepalive']}",
    ]
    return "\n".join(lines) + "\n"
