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

import time

from alle import applog, probe, singbox
from alle.constants import CLASH_API_ADDRESS
from alle.providers import ProviderError
from alle.state import Channel, Store


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.runner = singbox.Runner()
        self._errors: dict[str, str] = {}  # "<provider>/<id>" -> build error

    # ---- config assembly ---------------------------------------------------
    def _endpoint(self, ch: Channel) -> dict:
        wg = ch.wg
        if not wg or "peer" not in wg:
            raise ProviderError(f"channel {ch.provider}/{ch.id} has no usable WireGuard config.")
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
        config = {
            "log": {"level": "warn", "timestamp": True},
            "experimental": {"clash_api": {"external_controller": CLASH_API_ADDRESS}},
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
        """
        config, errors = self._build_config()
        self._errors = errors
        changed = self.runner.apply(config)
        if changed:
            applog.log(f"reconciled sing-box: {len(config['inbounds'])} channel(s) live")
        return errors

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
        applog.log(f"probed {len(channels)} channel(s)")
        return out
