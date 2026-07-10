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

import re
import time

from alle import applog, probe, routes, singbox
from alle.constants import OUTBOUND_PREFIX, ROUTER_INBOUND_TAG
from alle.providers import ProviderError
from alle.state import Channel, Store

# What a sing-box startup failure over a stolen port looks like in its log:
# "start inbound/mixed[in-…]: listen tcp 127.0.0.1:<port>: bind: address already in use"
_LOOPBACK_PORT = re.compile(r"127\.0\.0\.1:(\d+)")


def _ports_in_use(err_text: str) -> set[int]:
    """Loopback ports named before an address-in-use message on the same line."""
    return {
        int(port)
        for line in err_text.splitlines()
        if "address already in use" in line
        for port in _LOOPBACK_PORT.findall(line.split("address already in use", 1)[0])
    }


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
            try:
                endpoint = self._endpoint(ch)
            except ProviderError as e:
                errors[f"{ch.provider}/{ch.id}"] = str(e)
                continue
            inbounds.append(
                {
                    "type": "mixed",
                    "tag": ch.inbound_tag,
                    "listen": "127.0.0.1",
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
        return config, errors

    def _router_config(
        self,
        inbounds: list[dict],
        rules: list[dict],
        built: set[tuple[str, str]],
        errors: dict[str, str],
    ) -> None:
        """Append the always-on router entrypoint and its compiled rules.

        Layout (order is law): the per-channel exact rules already precede
        these; then a ``sniff`` action (the pinned sing-box dropped inbound
        sniffing — IP-dialing apps need it for domain rules), the built-in
        LAN/local default-direct block (when ``lan_direct`` is on — ahead of
        every user rule, so no catch-all can shadow it), the user rules in
        stored order, and unmatched handling — a trailing ``reject`` when the
        kill-switch is on, otherwise fall-through to ``route.final: direct``.

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
        if not port:
            return  # not yet allocated (first daemon start does it)
        inbounds.append(
            {
                "type": "mixed",
                "tag": ROUTER_INBOUND_TAG,
                "listen": "127.0.0.1",
                "listen_port": port,
            }
        )
        rules.append({"inbound": [ROUTER_INBOUND_TAG], "action": "sniff"})
        if router.get("lan_direct", True):
            rules.append(
                {
                    "inbound": [ROUTER_INBOUND_TAG],
                    "ip_cidr": list(routes.LAN_DIRECT_CIDRS),
                    "outbound": "direct",
                }
            )
        matcher_fields = {
            "domain": "domain",
            "domain_suffix": "domain_suffix",
            "ip_cidr": "ip_cidr",
        }
        for rule in router.get("rules") or []:
            ref = f"rule {rule.get('id')}"
            compiled: dict = {"inbound": [ROUTER_INBOUND_TAG]}
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
            rules.append({"inbound": [ROUTER_INBOUND_TAG], "action": "reject"})

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
        once; anything else (or a second failure) propagates to the caller.
        """
        config, errors = self._build_config()
        self._errors = errors
        for ref, err in sorted(errors.items()):
            applog.log(f"reconcile: {ref}: {err}")
        try:
            changed = self.runner.apply(config)
        except singbox.SingBoxError as e:
            if not self._recover_stolen_ports(str(e)):
                raise
            config, errors = self._build_config()  # store reloaded with new ports
            self._errors = errors
            changed = self.runner.apply(config)  # a second failure propagates
        if changed:
            live = sum(1 for i in config["inbounds"] if i["tag"] != ROUTER_INBOUND_TAG)
            router = "+ router" if len(config["inbounds"]) > live else "no router"
            applog.log(f"reconciled sing-box: {live} channel(s) live, {router}")
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

        Returns ``{"<provider>/<id>": probe_dict}``. Only meaningful while
        sing-box is running; if it isn't, every channel records a failure.
        """
        channels = self.store.channels() if channels is None else channels
        running = self.runner.is_running()
        out: dict[str, dict] = {}
        for ch in channels:
            ref = f"{ch.provider}/{ch.id}"
            if not running:
                result = {
                    "ok": False,
                    "at": int(time.time()),
                    "latency_ms": None,
                    "ip": None,
                    "error": "stopped",
                }
            else:
                result = probe.probe_channel(ch.port)
            self.store.set_probe(ch.provider, ch.id, result)
            out[ref] = result
        applog.log(_probe_log(channels, out))
        return out
