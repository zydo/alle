"""Parse, validate and canonicalize a WireGuard (wg-quick) ``.conf``.

The wg-quick format is provider-agnostic, so one parser serves every provider
that hands out a WireGuard config (Proton, self-hosted, ...). We
read only the keys sing-box can act on and ignore wg-quick-only directives
(``PostUp``, ``Table``, ``FwMark``, ...). An imported ``.conf`` is a snapshot of
*one* peer (one server); to roam locations you re-import another file.

A note on the endpoint: we keep whatever the file provides. A hostname is
re-resolved by sing-box on each reconcile (so it survives provider IP changes); a
literal IP is pinned and will fail if that server is ever retired.
"""

from __future__ import annotations

from alle.credentials import mask

WG_KEEPALIVE = 25
DEFAULT_ALLOWED_IPS = ["0.0.0.0/0", "::/0"]


class ConfError(Exception):
    """The pasted/loaded text is not a usable WireGuard config."""


def _sections(text: str) -> tuple[dict, dict]:
    """Split a wg-quick file into case-insensitive ``[Interface]``/``[Peer]`` maps.

    Only a single peer is supported (commercial configs ship exactly one); a
    second ``[Peer]`` simply overrides the first.
    """
    section = None
    interface: dict[str, str] = {}
    peer: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)  # base64 padding keeps '=' in the value
        key, value = key.strip().lower(), value.strip()
        if section == "interface":
            interface[key] = value
        elif section == "peer":
            peer[key] = value
    return interface, peer


def _csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    """``host:port`` → ``(host, port)``, handling bracketed IPv6 literals."""
    if endpoint.startswith("["):  # [2001:db8::1]:51820
        host, _, port = endpoint[1:].partition("]:")
    else:
        host, _, port = endpoint.rpartition(":")
    if not host or not port.isdigit():
        raise ConfError(f"endpoint {endpoint!r} is not host:port")
    return host, int(port)


def parse(text: str) -> dict:
    """Validate a wg-quick config and return the fields sing-box needs."""
    interface, peer = _sections(text)
    private_key = interface.get("privatekey")
    address = interface.get("address")
    public_key = peer.get("publickey")
    endpoint = peer.get("endpoint")
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
        raise ConfError("missing required field(s): " + ", ".join(missing))
    if private_key is None or address is None or public_key is None or endpoint is None:
        raise AssertionError("required WireGuard fields were checked but not narrowed")

    keepalive = peer.get("persistentkeepalive")
    host, port = _split_endpoint(endpoint)
    return {
        "private_key": private_key,
        "address": _csv(address),
        "peer": {
            "public_key": public_key,
            "endpoint_host": host,
            "endpoint_port": port,
            "preshared_key": peer.get("presharedkey") or None,
            "allowed_ips": _csv(peer.get("allowedips", ""))
            or list(DEFAULT_ALLOWED_IPS),
            "keepalive": int(keepalive)
            if keepalive and keepalive.isdigit()
            else WG_KEEPALIVE,
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


def suggest_name(parsed: dict) -> str:
    """A reasonable default channel label when the user gives none: the host."""
    return parsed["peer"]["endpoint_host"]


# ---- alle metadata headers ---------------------------------------------
#
# Utilities for embedding alle bookkeeping (provider, country, city, port, ...)
# as ``# alle-<key>: <value>`` comment lines at the top of a WireGuard .conf.
# ``parse`` ignores ``#`` lines, so these headers are invisible to wg-quick and
# other WireGuard tooling. Prepared for the post-MVP generic .conf import path.

META_PREFIX = "# alle-"
_META_KEYS = ("provider", "country", "city", "port", "enabled", "kind", "name")


def meta_block(meta: dict) -> str:
    """Render alle metadata as ``# alle-<key>: <value>`` header lines."""
    lines = []
    for key in _META_KEYS:
        if key not in meta or meta[key] in (None, ""):
            continue
        value = meta[key]
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"{META_PREFIX}{key}: {value}")
    return "\n".join(lines) + "\n\n"


def read_meta(text: str) -> dict[str, str]:
    """Parse the ``# alle-<key>: <value>`` headers back out of a .conf."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(META_PREFIX):
            continue
        body = line[len(META_PREFIX) :]
        if ":" in body:
            key, value = body.split(":", 1)
            out[key.strip()] = value.strip()
    return out
