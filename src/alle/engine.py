"""Translate the state store into one sing-box config, and run heartbeat probes.

Every reconcile rebuilds the whole config from the store: one ``mixed`` inbound +
one WireGuard ``endpoint`` per channel, wired by a route rule. The config is
written and sing-box restarted only if it actually changed (handled in
``singbox.Runner.apply``), so no-op reconciles never blip live tunnels.

Channels already carry their resolved WireGuard params (from a provider's API at
``channels add`` time), so reconcile does no network I/O — it just renders
``ch.wg`` into sing-box's endpoint shape. The separate probe path *does* do
network I/O (through each proxy) and writes the result back into the store.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
import time

from alle import applog, geodata, probe, routes, singbox
from alle.constants import (
    OUTBOUND_PREFIX,
    ROUTER_INBOUND_TAG,
    TUN_ADDRESS,
    TUN_ADDRESS_V6,
    TUN_DNS_TAG,
    TUN_DNS_UPSTREAM,
    TUN_INBOUND_TAG,
    TUN_MTU,
    WG_MTU,
)
from alle.providers import ProviderError, supports_ipv6
from alle.state import Channel, Store, channel_fingerprint

# Probe concurrency: enough parallelism that a fleet of dead channels stays
# bounded, small enough not to burst-load every tunnel at once.
PROBE_POOL_SIZE = 8
# Wall-clock bound for one whole probe pass; anything still unfinished is
# recorded as failed so the pass can never stall the daemon's probe cadence.
PROBE_PASS_DEADLINE = 60.0

# What a sing-box startup failure over a stolen port looks like in its log:
# "start inbound/mixed[in-…]: listen tcp 127.0.0.1:<port>: bind: address already in use"
# The host part follows the configured listen address (see _listen_addr), so
# match any IPv4 host or a bracketed IPv6 one, not just loopback.
_LOOPBACK_PORT = re.compile(r"(?:(?:\d{1,3}\.){3}\d{1,3}|\]):(\d+)")


def _ports_in_use(err_text: str) -> set[int]:
    """Ports named before an address-in-use message on the same line."""
    return {
        int(port)
        for line in err_text.splitlines()
        if "address already in use" in line
        for port in _LOOPBACK_PORT.findall(line.split("address already in use", 1)[0])
    }


def _listen_addr() -> str:
    """The address channel/router inbounds bind. Default (and the only host
    behavior): loopback. ``ALLE_LISTEN`` — set to ``0.0.0.0`` by the container
    image, where the container boundary is the trust boundary — widens it;
    an invalid value is logged and ignored rather than killing the daemon
    or silently widening the bind."""
    value = (os.environ.get("ALLE_LISTEN") or "").strip()
    if not value:
        return "127.0.0.1"
    try:
        ipaddress.ip_address(value)
    except ValueError:
        applog.log(f"ALLE_LISTEN={value!r} is not an IP address — using 127.0.0.1")
        return "127.0.0.1"
    return value


def _wg_mtu() -> int:
    """Tunnel MTU emitted on every WireGuard endpoint. Default: the
    can't-crash conservative ``constants.WG_MTU`` (see the rationale there).
    ``ALLE_WG_MTU`` overrides it for operators who know their path; an
    invalid value is logged and ignored rather than killing the daemon.
    1280 is the floor (IPv6's minimum — WireGuard carries v6 inside)."""
    value = (os.environ.get("ALLE_WG_MTU") or "").strip()
    if not value:
        return WG_MTU
    try:
        mtu = int(value)
    except ValueError:
        applog.log(f"ALLE_WG_MTU={value!r} is not a number — using {WG_MTU}")
        return WG_MTU
    if not 1280 <= mtu <= 9000:
        applog.log(f"ALLE_WG_MTU={mtu} is outside 1280-9000 — using {WG_MTU}")
        return WG_MTU
    return mtu


def _probe_detail(ref: str, result: dict) -> str:
    if result.get("ok"):
        bits = [ref, "ok"]
        if result.get("latency_ms") is not None:
            bits.append(f"{result['latency_ms']}ms")
        if result.get("ip"):
            bits.append(f"ip={result['ip']}")
        return " ".join(bits)

    state = "stopped" if result.get("error") == "stopped" else "failed"
    detail = f"{ref} {state}"
    if result.get("error") and result.get("error") != state:
        detail += f" {result['error']}"
    # the verbose explanation (sources tried, last exception) rides the log,
    # not the table — keeps the STATE column to a brief category word.
    if result.get("detail"):
        detail += f" — {result['detail']}"
    return detail


def _probe_log(channels: list[Channel], results: dict[str, dict]) -> str:
    healthy = sum(1 for result in results.values() if result.get("ok"))
    failed = len(results) - healthy
    summary = f"probe: {len(channels)} channel(s), {healthy} healthy, {failed} failed"
    if results and all(r.get("error") == "stopped" for r in results.values()):
        summary = f"probe: sing-box stopped; {len(channels)} channel(s)"
    details = "; ".join(_probe_detail(ref, results[ref]) for ref in sorted(results))
    return f"{summary}: {details}" if details else summary


def _v4_only(cidrs: list) -> list:
    """The IPv4 entries of an address/allowed_ips list."""
    out = []
    for value in cidrs or []:
        try:
            if ipaddress.ip_network(str(value), strict=False).version == 4:
                out.append(value)
        except ValueError:
            continue
    return out


def _has_global_v6(cidrs: list) -> bool:
    for value in cidrs or []:
        try:
            net = ipaddress.ip_network(str(value), strict=False)
        except ValueError:
            continue
        if net.version == 6 and net.network_address.is_global:
            return True
    return False


def channel_ipv6(ch: Channel) -> bool:
    """Whether this channel carries IPv6 inside its tunnel.

    Both halves must hold: the provider explicitly supports v6 (registry
    ``ipv6`` flag — NordVPN off, ProtonVPN on), AND this channel's own
    WireGuard config has a *global* v6 interface address (per-server
    capability, e.g. Proton's ~20% v4-only servers, detected locally — a
    ULA/link-local address is plumbing, not connectivity).
    """
    if not supports_ipv6(ch.provider):
        return False
    return _has_global_v6((ch.wg or {}).get("address") or [])


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.runner = singbox.Runner()
        self._errors: dict[str, str] = {}  # "<provider>/<id>" -> build error
        # Explicitly declared ports found taken by another process during
        # stolen-port recovery. A declaration is a contract with outside
        # configuration (compose wiring, firewalls), so it is never moved;
        # instead its owner is excluded from the retried config and reported
        # degraded until the user frees the port or changes the declaration.
        self._held_ports: set[int] = set()

    # ---- config assembly ---------------------------------------------------
    def _endpoint(self, ch: Channel) -> dict:
        wg = ch.wg
        if not wg or "peer" not in wg:
            raise ProviderError(
                f"channel {ch.provider}/{ch.id} has no usable WireGuard config."
            )
        peer = wg["peer"]
        v6 = channel_ipv6(ch)
        # IPv6 is an explicit per-provider decision (providers.supports_ipv6):
        # a non-supporting provider gets v6 stripped from BOTH the interface
        # addresses and allowed_ips, even if its config smuggled some in —
        # sing-box must never try to route v6 into a tunnel that can't carry
        # it. A supporting provider's v4-only server strips the same way.
        address = wg["address"] if v6 else _v4_only(wg["address"])
        allowed = peer["allowed_ips"] if v6 else _v4_only(peer["allowed_ips"])
        if not allowed:
            # Every entry was v6/unparseable on a v4-only channel: fall back to
            # the v4 default rather than the original list (which would smuggle
            # v6 ranges back into a tunnel with no v6 source). An empty list
            # would make sing-box reject the config; ["0.0.0.0/0"] is the
            # honest "route all v4" intent of a VPN tunnel.
            allowed = ["0.0.0.0/0"]
        wg_peer = {
            "address": peer["endpoint_host"],
            "port": peer["endpoint_port"],
            "public_key": peer["public_key"],
            "allowed_ips": allowed,
            "persistent_keepalive_interval": peer["keepalive"],
        }
        if peer.get("preshared_key"):
            wg_peer["pre_shared_key"] = peer["preshared_key"]
        return {
            "type": "wireguard",
            "tag": ch.outbound_tag,
            "system": False,
            "address": address,
            "mtu": _wg_mtu(),
            "private_key": wg["private_key"],
            "peers": [wg_peer],
        }

    def _build_config(self) -> tuple[dict, dict[str, str]]:
        inbounds, endpoints, rules, errors = [], [], [], {}
        built: set[tuple[str, str]] = set()
        v6_capable: set[tuple[str, str]] = set()
        for ch in self.store.channels():
            if not ch.enabled:
                # A disabled channel is not materialised at all: no inbound,
                # no WireGuard endpoint (so no handshake/keepalive toward the
                # provider — the whole point), no route rule. Not an error;
                # a rule that somehow still targets it compiles fail-closed
                # to reject via the `built` miss below.
                continue
            if ch.port in self._held_ports:
                errors[f"{ch.provider}/{ch.id}"] = (
                    f"declared port {ch.port} is in use by another process — "
                    "a port: declaration is a contract and is never moved; "
                    "free the port (then restart alle) or change the "
                    "declaration"
                )
                continue
            try:
                endpoint = self._endpoint(ch)
            except ProviderError as e:
                errors[f"{ch.provider}/{ch.id}"] = str(e)
                continue
            inbounds.append(
                {
                    "type": "mixed",
                    "tag": ch.inbound_tag,
                    "listen": _listen_addr(),
                    "listen_port": ch.port,
                }
            )
            endpoints.append(endpoint)
            rules.append({"inbound": [ch.inbound_tag], "outbound": ch.outbound_tag})
            built.add((ch.provider, ch.id))
            if channel_ipv6(ch):
                v6_capable.add((ch.provider, ch.id))
        rule_sets: list[dict] = []
        self._router_config(inbounds, rules, built, errors, rule_sets, v6_capable)
        api = singbox.clash_api()
        config: dict = {
            "log": {"level": "warn", "timestamp": True},
            "experimental": {
                "clash_api": {
                    "external_controller": api["address"],
                    "secret": api["secret"],
                }
            },
            "inbounds": inbounds,
            "outbounds": [{"type": "direct", "tag": "direct"}],
            "endpoints": endpoints,
            "route": {"rules": rules, "final": "direct"},
        }
        if rule_sets:
            # Always type: local — alle downloaded and verified these files on
            # an explicit user action; the engine itself must never fetch or
            # auto-update rule-set data (no-background-traffic, end to end).
            config["route"]["rule_set"] = rule_sets
        if self.store.router.get("tun"):
            # alle owns resolution in TUN mode: hijacked queries are answered
            # from this upstream, dialed direct (see constants.TUN_DNS_UPSTREAM
            # for the stance). default_domain_resolver is required alongside a
            # dns section since sing-box 1.12; auto_detect_interface keeps
            # sing-box's own sockets bound to the physical interface (loop
            # safety). Both are tun-only so the explicit-proxy config stays
            # byte-identical.
            # No detour on the server: sing-box dials it directly by default
            # and rejects an explicit detour to a plain direct outbound at
            # runtime ("makes no sense") even though `check` accepts it.
            config["dns"] = {
                "servers": [
                    {
                        "type": "udp",
                        "tag": TUN_DNS_TAG,
                        "server": TUN_DNS_UPSTREAM,
                    }
                ],
                # With no v6-capable channel, AAAA answers would only feed
                # connections the ::/0 reject (the IPv6 leak fix) then blocks
                # — don't hand them out (ipv4_only). Once a v6-capable channel
                # exists, prefer_ipv4 keeps v4 first (safe everywhere) while
                # letting v6-only destinations resolve and ride a capable
                # channel — or fail closed at a v4-only channel's guard.
                "strategy": "prefer_ipv4" if v6_capable else "ipv4_only",
            }
            config["route"]["default_domain_resolver"] = TUN_DNS_TAG
            config["route"]["auto_detect_interface"] = True
        return config, errors

    @staticmethod
    def _tun_interface_name() -> str:
        # Fixed name so teardown checks are deterministic. Darwin only accepts
        # utun<N>; a high N stays clear of the kernel's low auto-assigned
        # numbers (Tier 3 verifies the Darwin semantics).
        return "utun225" if sys.platform == "darwin" else "alle-tun"

    def _tun_inbound(self) -> dict:
        inbound = {
            "type": "tun",
            "tag": TUN_INBOUND_TAG,
            "interface_name": self._tun_interface_name(),
            # The v6 address is the IPv6 LEAK FIX, not IPv6 support: it makes
            # auto_route seize the v6 default route so IPv6 is captured (and
            # then rejected — see the ::/0 rule) instead of bypassing the VPN
            # out the physical interface. See constants.TUN_ADDRESS_V6.
            "address": [TUN_ADDRESS, TUN_ADDRESS_V6],
            "mtu": TUN_MTU,
            "auto_route": True,
            "strict_route": True,
            "stack": "system",
        }
        if sys.platform == "linux":
            # Upstream calls auto_redirect "always recommended" on Linux:
            # better routing, higher performance than tproxy, and no conflicts
            # between the TUN and Docker bridge networks. Linux-only field.
            inbound["auto_redirect"] = True
        return inbound

    def _router_config(
        self,
        inbounds: list[dict],
        rules: list[dict],
        built: set[tuple[str, str]],
        errors: dict[str, str],
        rule_sets: list[dict],
        v6_capable: set[tuple[str, str]] | None = None,
    ) -> None:
        """Append the shared-rule entry inbounds (router, tun) and the one
        compiled rule table both share.

        The router entrypoint and the tun inbound are two doors into the same
        route table: every compiled rule lists both tags in its ``inbound``
        (one source of truth — never a second rule set), while the per-channel
        exact rules stay pinned to their own inbound ("never demoted").

        Layout (order is law): the per-channel exact rules already precede
        these; then a ``sniff`` action (the pinned sing-box dropped inbound
        sniffing — IP-dialing apps need it for domain rules), the port half
        of the LAN-direct block (UDP DHCP/SSDP/mDNS ports — ahead of the DNS
        hijack on purpose: unicast mDNS is wire-format DNS, so the sniffer
        would classify it as protocol ``dns`` and the hijack would swallow it
        into a resolver that cannot answer ``.local``), the tun-only DNS
        hijack (ahead of the CIDR LAN-direct block, so port-53 queries to a
        LAN resolver are still answered by alle, not leaked), the CIDR half
        of the built-in LAN/local default-direct block (both halves only when
        ``lan_direct`` is on — ahead of every user rule, so no catch-all can
        shadow them), the user rules in stored order,
        and unmatched handling — a trailing ``reject`` when the kill-switch is
        on (with tun active that is genuinely system-wide), otherwise
        fall-through to ``route.final: direct``.

        Fail closed: a rule whose channel target is not in this build (removed
        behind our back, or the channel itself failed to build) compiles to
        ``action: reject`` — its matcher keeps matching, but the traffic is
        blocked instead of silently falling through to ``route.final: direct``
        outside the tunnel. The miss is still reported via ``errors``. (A
        channel that failed to build also loses its own inbound, so its proxy
        port refuses connections — fail-closed on that surface already.)
        Emitting the rule at all matters: sing-box would reject the whole
        config over one missing outbound tag.
        """
        router = self.store.router
        port = int(router.get("port") or 0)
        if port and port in self._held_ports:
            errors["router/entrypoint"] = (
                f"declared router port {port} is in use by another process — "
                "a declared port is never moved; free the port (then restart "
                "alle) or change the router: port: declaration"
            )
            port = 0  # the entrypoint stays down (degraded), everything else lives
        tun = bool(router.get("tun"))
        entry: list[str] = []
        if port:  # 0 until the first daemon start allocates it
            inbounds.append(
                {
                    "type": "mixed",
                    "tag": ROUTER_INBOUND_TAG,
                    "listen": _listen_addr(),
                    "listen_port": port,
                }
            )
            entry.append(ROUTER_INBOUND_TAG)
        if tun:
            inbounds.append(self._tun_inbound())
            entry.append(TUN_INBOUND_TAG)
        if not entry:
            return
        rules.append({"inbound": list(entry), "action": "sniff"})
        lan_direct = bool(router.get("lan_direct", True))
        if lan_direct:
            rules.append(
                {
                    "inbound": list(entry),
                    "network": ["udp"],
                    "port": list(routes.LAN_DIRECT_UDP_PORTS),
                    "outbound": "direct",
                }
            )
        if tun:
            rules.append(
                {
                    "inbound": [TUN_INBOUND_TAG],
                    "protocol": "dns",
                    "action": "hijack-dns",
                }
            )
        if lan_direct:
            rules.append(
                {
                    "inbound": list(entry),
                    "ip_cidr": list(routes.LAN_DIRECT_CIDRS),
                    "outbound": "direct",
                }
            )
        v6_capable = v6_capable or set()
        if tun and not v6_capable:
            # IPv6 leak fix — block, don't leak. With no v6-capable channel in
            # the fleet, IPv6 cannot ride any tunnel; without this rule (and
            # the tun's v6 address that captures the traffic), IPv6 would
            # silently bypass the VPN via the physical interface, exposing the
            # home address. Placed after LAN-direct (link-local/ULA v6 to LAN
            # devices stays reachable when that is on) and before user rules
            # (a catch-all must not steer v6 into an IPv4-only channel).
            # Tun-only: explicit-proxy mode never captured IPv6 at all.
            # When v6-capable channels exist, this blanket reject is replaced
            # by per-rule guards below: v6 flows into capable channels and is
            # rejected — same matcher, fail closed — ahead of v4-only ones.
            rules.append(
                {
                    "inbound": [TUN_INBOUND_TAG],
                    "ip_cidr": ["::/0"],
                    "action": "reject",
                }
            )
        # One domain semantic: every domain rule compiles to sing-box's
        # dot-boundary domain_suffix (the domain + its subdomains). Legacy
        # exact "domain" rows are normalized away at state load.
        matcher_fields = {
            "domain_suffix": "domain_suffix",
            "ip_cidr": "ip_cidr",
        }
        rule_set_tags: dict[str, dict] = {}
        for rule in router.get("rules") or []:
            ref = f"rule {rule.get('id')}"
            compiled: dict = {"inbound": list(entry)}
            mtype = rule.get("type")
            if mtype in matcher_fields:
                compiled[matcher_fields[mtype]] = [rule.get("value")]
            elif mtype in routes.GEO_TYPES:
                # Geo matchers reference a cached, digest-verified .srs file.
                # Without the file the matcher cannot exist at all (sing-box
                # rejects an unknown rule_set tag) — fail CLOSED (reject), the
                # same shape as an unusable-channel target below, so matching
                # traffic is blocked rather than falling through to direct
                # (which would leak it, defeating the rule's intent).
                name = str(rule.get("value"))
                path = geodata.cached_path(self.store, mtype, name)
                if path is None:
                    errors[ref] = (
                        f"{mtype} category {name!r} is not cached (or failed "
                        "its digest check) — matching traffic is blocked until "
                        "it is fetched: alle routes geo refresh"
                    )
                    compiled["action"] = "reject"
                    rules.append(compiled)
                    continue
                tag = f"{mtype}-{name}"
                rule_set_tags[tag] = {
                    "type": "local",
                    "tag": tag,
                    "format": "binary",
                    "path": str(path),
                }
                compiled["rule_set"] = [tag]
            elif mtype != "all":
                errors[ref] = f"unknown matcher type {mtype!r}"
                continue
            target = str(rule.get("target", ""))
            if target == "direct":
                compiled["outbound"] = "direct"
            elif target == "block":
                compiled["action"] = "reject"
            else:
                provider, _, cid = target.partition("/")
                if (provider, cid) not in built:
                    errors[ref] = (
                        f"references unusable channel {target}; "
                        "matching traffic is blocked until this is fixed"
                    )
                    compiled["action"] = "reject"
                    rules.append(compiled)
                    continue
                if v6_capable and (provider, cid) not in v6_capable and tun:
                    # Mixed fleet, tun on, and this rule targets a v4-only
                    # channel: the blanket ::/0 reject is gone, so guard this
                    # rule with a same-matcher v6 reject compiled FIRST —
                    # matching v6 traffic is blocked, never steered into a
                    # tunnel that can't carry it and never falls through. The
                    # guard inherits the compiled rule's inbounds (router+tun),
                    # so v6 is rejected on both surfaces.
                    guard = {k: v for k, v in compiled.items() if k != "outbound"}
                    guard["ip_version"] = 6
                    guard["action"] = "reject"
                    rules.append(guard)
                compiled["outbound"] = f"{OUTBOUND_PREFIX}{provider}-{cid}"
            rules.append(compiled)
        # Geo rule-sets referenced above, deduped in stable order so the
        # compiled config does not churn on a refresh that changed nothing.
        rule_sets.extend(rule_set_tags[tag] for tag in sorted(rule_set_tags))
        if tun and v6_capable:
            # Catch-all v6 reject (after user rules, before the kill-switch):
            # with v6-capable channels the blanket ::/0 reject is gone, so v6
            # matching NO rule would otherwise fall through to route.final
            # (direct) and leak the home address. Catch-alls (all → channel)
            # and v6-capable targets still match their v6 earlier in the list;
            # only truly unmatched v6 is rejected. LAN v6 (ULA/link-local/
            # multicast) is untouched — those CIDR rules precede user rules.
            rules.append(
                {
                    "inbound": [TUN_INBOUND_TAG],
                    "ip_cidr": ["::/0"],
                    "action": "reject",
                }
            )
        if router.get("killswitch"):
            rules.append({"inbound": list(entry), "action": "reject"})

    # ---- reconcile ---------------------------------------------------------
    def reconcile(self) -> dict[str, str]:
        """Rebuild the config and (re)start sing-box to match the store.

        Returns ``{"<provider>/<id>": error}`` for channels that could not be
        built (left out of the live config, their proxy ports closed) and
        ``{"rule <id>": error}`` for router rules whose target is unusable
        (compiled to ``reject`` so their traffic is blocked, not leaked).

        Ports are allocated when a channel is added, so another process can
        grab one before a later sing-box (re)start — and sing-box treats a
        single unbindable inbound as fatal for the whole config. When a start
        fails that way, the stolen ports are reallocated and the apply retried
        once.

        Raises :class:`singbox.ConfigRejectedError` when sing-box refuses the
        generated config (deterministic — a timer retry cannot help, only a
        state change can) and :class:`singbox.SingBoxRuntimeError` when a valid
        config failed at runtime (environmental — worth retrying).
        """
        config, errors = self._build_config()
        self._errors = errors
        for ref, err in sorted(errors.items()):
            applog.log(f"reconcile: {ref}: {err}")
        result = self.runner.apply(config)
        if result.outcome is singbox.ApplyOutcome.RUNTIME_FAILED and (
            self._recover_stolen_ports(result.detail)
        ):
            config, errors = self._build_config()  # store reloaded with new ports
            self._errors = errors
            result = self.runner.apply(config)
        if result.outcome is singbox.ApplyOutcome.REJECTED:
            raise singbox.ConfigRejectedError(
                result.detail or "sing-box rejected the generated config"
            )
        if result.outcome is singbox.ApplyOutcome.RUNTIME_FAILED:
            raise singbox.SingBoxRuntimeError(
                result.detail or "sing-box failed at runtime"
            )
        if result.outcome is singbox.ApplyOutcome.APPLIED:
            tags = {i["tag"] for i in config["inbounds"]}
            live = len(tags - {ROUTER_INBOUND_TAG, TUN_INBOUND_TAG})
            router = "+ router" if ROUTER_INBOUND_TAG in tags else "no router"
            tun = " + tun" if TUN_INBOUND_TAG in tags else ""
            applog.log(f"reconciled sing-box: {live} channel(s) live, {router}{tun}")
        return errors

    def _recover_stolen_ports(self, err_text: str) -> bool:
        """Reallocate local ports named in an address-in-use start failure.

        Covers both channel proxy ports (moved in the store) and the Clash API
        port (its endpoint file is regenerated). True if anything was freed.
        """
        stolen = _ports_in_use(err_text)
        if not stolen:
            return False
        recovered = False
        api = singbox.clash_api()
        api_port = int(api["address"].rsplit(":", 1)[1])
        if api_port in stolen:
            singbox.forget_clash_api()
            applog.log(
                f"reconcile: clash api port {api_port} was taken by another "
                "process — regenerated the endpoint"
            )
            recovered = True
        moved, held = self.store.reallocate_channel_ports(stolen)
        for provider, cid, old, new in moved:
            applog.log(
                f"reconcile: port {old} of {provider}/{cid} was taken by "
                f"another process — moved to :{new}"
            )
            recovered = True
        for provider, cid, port in held:
            owner = (
                "the router entrypoint"
                if (provider, cid) == ("router", "entrypoint")
                else f"channel {provider}/{cid}"
            )
            applog.log(
                f"reconcile: declared port {port} of {owner} is taken by "
                "another process — declarations are a contract and never "
                "move; the owner stays degraded until you free the port or "
                "change the declaration"
            )
            self._held_ports.add(port)
            recovered = True  # the retry rebuilds without the held owner
        return recovered

    # ---- probing -----------------------------------------------------------
    @staticmethod
    def _probe_one(ch: Channel) -> dict:
        """One channel's probe, plus the supplementary IPv6-exit lookup for
        v6-capable channels. The v6 echo never affects the health verdict —
        a healthy v4 probe with no v6 answer just shows no v6 exit."""
        result = probe.probe_channel(ch.port)
        if result.get("ok") and channel_ipv6(ch):
            result["ipv6"] = probe.probe_ipv6(ch.port)
        return result

    def probe_all(self, channels: list[Channel] | None = None) -> dict[str, dict]:
        """Probe each channel through its proxy and persist the result.

        Defaults to the **enabled** channels: a disabled one has no inbound to
        probe, so probing it would only manufacture failures. Returns
        ``{"<provider>/<id>": probe_dict}``. Only meaningful while
        sing-box is running; if it isn't, every channel records a failure.

        Channels are probed concurrently on a capped worker pool, and the
        whole pass carries a wall-clock deadline: each channel already bounds
        itself (``probe.CHANNEL_DEADLINE``), so with the pool the pass costs
        ``ceil(n / pool) × deadline`` at worst — never ``n × sources ×
        timeout`` serially. Work that hasn't started when the pass deadline
        hits is cancelled and recorded as a failure, not left running.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed

        if channels is None:
            channels = [ch for ch in self.store.channels() if ch.enabled]
        running = self.runner.is_running()
        out: dict[str, dict] = {}
        if not running:
            for ch in channels:
                out[f"{ch.provider}/{ch.id}"] = {
                    "ok": False,
                    "at": int(time.time()),
                    "latency_ms": None,
                    "ip": None,
                    "error": "stopped",
                }
        elif channels:
            deadline = time.monotonic() + PROBE_PASS_DEADLINE
            pool = ThreadPoolExecutor(max_workers=min(PROBE_POOL_SIZE, len(channels)))
            futures = {pool.submit(self._probe_one, ch): ch for ch in channels}
            try:
                try:
                    remaining = max(0.0, deadline - time.monotonic())
                    for fut in as_completed(futures, timeout=remaining):
                        ch = futures[fut]
                        out[f"{ch.provider}/{ch.id}"] = fut.result()
                except TimeoutError:
                    pass
                for fut, ch in futures.items():
                    ref = f"{ch.provider}/{ch.id}"
                    if ref in out:
                        continue
                    # Once the absolute deadline expires, even a result racing
                    # with this sweep is late and cannot be published.
                    fut.cancel()
                    out[ref] = {
                        "ok": False,
                        "at": int(time.time()),
                        "latency_ms": None,
                        "ip": None,
                        "error": (
                            f"probe pass deadline ({PROBE_PASS_DEADLINE:g}s) exceeded"
                        ),
                    }
            finally:
                # Do not let a non-cooperative probe extend the pass deadline.
                # Workers only return data; all state publication stays here.
                pool.shutdown(wait=False, cancel_futures=True)
        updates = {
            (ch.provider, ch.id): (
                channel_fingerprint(ch),
                out[f"{ch.provider}/{ch.id}"],
            )
            for ch in channels
        }
        self.store.set_probes(updates)
        applog.log(_probe_log(channels, out))
        return out
