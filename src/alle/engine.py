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

from alle import applog, probe, singbox
from alle.providers import ProviderError
from alle.state import Channel, Store

# What a sing-box startup failure over a stolen port looks like in its log:
# "start inbound/mixed[in-…]: listen tcp 127.0.0.1:<port>: bind: address already in use"
_ADDR_IN_USE = re.compile(r"127\.0\.0\.1:(\d+).*?address already in use")


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

    # ---- reconcile ---------------------------------------------------------
    def reconcile(self) -> dict[str, str]:
        """Rebuild the config and (re)start sing-box to match the store.

        Returns ``{"<provider>/<id>": error}`` for channels that could not be
        built; those are simply left out of the live config.

        Ports are allocated when a channel is added, so another process can
        grab one before a later sing-box (re)start — and sing-box treats a
        single unbindable inbound as fatal for the whole config. When a start
        fails that way, the stolen ports are reallocated and the apply retried
        once; anything else (or a second failure) propagates to the caller.
        """
        config, errors = self._build_config()
        self._errors = errors
        for ref, err in sorted(errors.items()):
            applog.log(f"reconcile: left {ref} out of the config: {err}")
        try:
            changed = self.runner.apply(config)
        except singbox.SingBoxError as e:
            if not self._recover_stolen_ports(str(e)):
                raise
            config, errors = self._build_config()  # store reloaded with new ports
            self._errors = errors
            changed = self.runner.apply(config)  # a second failure propagates
        if changed:
            applog.log(
                f"reconciled sing-box: {len(config['inbounds'])} channel(s) live"
            )
        return errors

    def _recover_stolen_ports(self, err_text: str) -> bool:
        """Reallocate local ports named in an address-in-use start failure.

        Covers both channel proxy ports (moved in the store) and the Clash API
        port (its endpoint file is regenerated). True if anything was freed.
        """
        stolen = {int(port) for port in _ADDR_IN_USE.findall(err_text)}
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
