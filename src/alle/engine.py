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

from alle import applog, probe, routes, singbox
from alle.constants import (
    OUTBOUND_PREFIX,
    ROUTER_INBOUND_TAG,
    TUN_ADDRESS,
    TUN_ADDRESS_V6,
    TUN_DNS_TAG,
    TUN_DNS_UPSTREAM,
    TUN_INBOUND_TAG,
    TUN_MTU,
)
from alle.providers import ProviderError
from alle.state import Channel, Store

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


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.runner = singbox.Runner()
        self._errors: dict[str, str] = {}  # "<provider>/<id>" -> build error

    # ---- config assembly ---------------------------------------------------
    def _endpoint(self, ch: Channel) -> dict:
        wg = ch.wg
        if not wg or "peer" not in wg:
            raise ProviderError(
                f"channel {ch.provider}/{ch.id} has no usable WireGuard config."
            )
        peer = wg["peer"]
        wg_peer = {
            "address": peer["endpoint_host"],
            "port": peer["endpoint_port"],
            "public_key": peer["public_key"],
            "allowed_ips": peer["allowed_ips"],
            "persistent_keepalive_interval": peer["keepalive"],
        }
        if peer.get("preshared_key"):
            wg_peer["pre_shared_key"] = peer["preshared_key"]
        return {
            "type": "wireguard",
            "tag": ch.outbound_tag,
            "system": False,
            "address": wg["address"],
            "private_key": wg["private_key"],
            "peers": [wg_peer],
        }

    def _build_config(self) -> tuple[dict, dict[str, str]]:
        inbounds, endpoints, rules, errors = [], [], [], {}
        built: set[tuple[str, str]] = set()
        for ch in self.store.channels():
            if not ch.enabled:
                # A disabled channel is not materialised at all: no inbound,
                # no WireGuard endpoint (so no handshake/keepalive toward the
                # provider — the whole point), no route rule. Not an error;
                # a rule that somehow still targets it compiles fail-closed
                # to reject via the `built` miss below.
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
        self._router_config(inbounds, rules, built, errors)
        api = singbox.clash_api()
        config = {
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
                # ipv4_only: the supported providers' WireGuard is IPv4-only,
                # so AAAA answers would only feed connections the ::/0 reject
                # (the IPv6 leak fix) then blocks — don't hand them out.
                "strategy": "ipv4_only",
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
    ) -> None:
        """Append the shared-rule entry inbounds (router, tun) and the one
        compiled rule table both share.

        The router entrypoint and the tun inbound are two doors into the same
        route table: every compiled rule lists both tags in its ``inbound``
        (one source of truth — never a second rule set), while the per-channel
        exact rules stay pinned to their own inbound ("never demoted").

        Layout (order is law): the per-channel exact rules already precede
        these; then a ``sniff`` action (the pinned sing-box dropped inbound
        sniffing — IP-dialing apps need it for domain rules), the tun-only
        DNS hijack (ahead of LAN-direct, so queries to a LAN resolver are
        still answered by alle, not leaked), the built-in LAN/local
        default-direct block (when ``lan_direct`` is on — ahead of every user
        rule, so no catch-all can shadow it), the user rules in stored order,
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
        if tun:
            rules.append(
                {
                    "inbound": [TUN_INBOUND_TAG],
                    "protocol": "dns",
                    "action": "hijack-dns",
                }
            )
        if router.get("lan_direct", True):
            rules.append(
                {
                    "inbound": list(entry),
                    "ip_cidr": list(routes.LAN_DIRECT_CIDRS),
                    "outbound": "direct",
                }
            )
        if tun:
            # IPv6 leak fix — block, don't leak. The supported providers'
            # WireGuard configs are IPv4-only, so IPv6 cannot ride the tunnel;
            # without this rule (and the tun's v6 address that captures the
            # traffic), IPv6 would silently bypass the VPN via the physical
            # interface, exposing the home address. Placed after LAN-direct
            # (link-local/ULA v6 to LAN devices stays reachable when that is
            # on) and before user rules (a catch-all must not steer v6 into an
            # IPv4-only channel). Tun-only: explicit-proxy mode never captured
            # IPv6 in the first place.
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
        for rule in router.get("rules") or []:
            ref = f"rule {rule.get('id')}"
            compiled: dict = {"inbound": list(entry)}
            mtype = rule.get("type")
            if mtype in matcher_fields:
                compiled[matcher_fields[mtype]] = [rule.get("value")]
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
                compiled["outbound"] = f"{OUTBOUND_PREFIX}{provider}-{cid}"
            rules.append(compiled)
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
        for provider, cid, old, new in self.store.reallocate_channel_ports(stolen):
            applog.log(
                f"reconcile: port {old} of {provider}/{cid} was taken by "
                f"another process — moved to :{new}"
            )
            recovered = True
        return recovered

    # ---- probing -----------------------------------------------------------
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
            with ThreadPoolExecutor(
                max_workers=min(PROBE_POOL_SIZE, len(channels))
            ) as pool:
                futures = {
                    pool.submit(probe.probe_channel, ch.port): ch for ch in channels
                }
                try:
                    for fut in as_completed(futures, timeout=PROBE_PASS_DEADLINE):
                        ch = futures[fut]
                        out[f"{ch.provider}/{ch.id}"] = fut.result()
                except TimeoutError:
                    for fut, ch in futures.items():
                        ref = f"{ch.provider}/{ch.id}"
                        if ref in out:
                            continue
                        if fut.done() and not fut.cancelled():
                            out[ref] = fut.result()  # finished during the sweep
                            continue
                        fut.cancel()  # unstarted work is dropped, not orphaned
                        out[ref] = {
                            "ok": False,
                            "at": int(time.time()),
                            "latency_ms": None,
                            "ip": None,
                            "error": (
                                "probe pass deadline "
                                f"({PROBE_PASS_DEADLINE:g}s) exceeded"
                            ),
                        }
        # Persist sequentially from this thread: results land in stable
        # channel order and the store transactions never contend.
        for ch in channels:
            ref = f"{ch.provider}/{ch.id}"
            self.store.set_probe(ch.provider, ch.id, out[ref])
        applog.log(_probe_log(channels, out))
        return out
