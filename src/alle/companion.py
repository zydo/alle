"""Thin client of the hardened ``/api/v1`` loopback control API.

This is the non-GUI core of the desktop companion (menu-bar app / tray). It is
deliberately a *client*: it holds no business logic, talks to the already
running ``alled`` daemon over its loopback control API, and treats ``/api/v1``
as a **versioned contract** it can skew against — so it degrades gracefully on
unknown fields and unknown endpoints (404) rather than crashing when it runs a
version ahead of or behind the daemon.

Trust: the companion reads the Bearer secret from ``control_api.json`` (same
per-installation secret the Web UI uses) and, before every authed call, runs
the ``/health`` HMAC challenge to prove *our* daemon is behind the contract
port — so a foreign process squatting the port is never handed the secret.
All traffic is loopback.

The tray/menu-bar frontends (rumps/PyObjC v1 spike; SwiftUI/AppKit later) are
thin renderers over :class:`CompanionClient`; the guardrail is that they add no
capability this client does not already expose (status, channel summary,
lifecycle, tun, kill-switch, open Web UI).
"""

from __future__ import annotations

import hmac
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass

from alle.webui import auth, server


class CompanionError(RuntimeError):
    """A companion-facing failure: daemon not running, unreachable, or a
    control-API error surfaced verbatim for the tray to show."""


class DaemonUnavailable(CompanionError):
    """The daemon is not running or not answering the health challenge. The
    tray shows a disconnected state and keeps polling, never a hard crash."""


@dataclass
class TrayState:
    """The flat snapshot the tray renders — every field defensively derived
    from the status payload, so an older/newer daemon shape never crashes the
    render (the version-skew contract)."""

    running: bool
    tun: bool
    killswitch: bool
    channel_summary: str
    channel_count: int
    provider_count: int
    installed_version: str | None
    web_ui_url: str | None
    router_port: int | None


class CompanionClient:
    """A thin, reconnect-tolerant client of one alled's ``/api/v1``.

    Construct cheaply; every call re-reads the endpoint so a daemon restart
    (new port/secret) is picked up without reconstructing the client.
    """

    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout

    # -- endpoint + trust ----------------------------------------------------
    def _endpoint(self) -> dict:
        """The current ``{address, secret, host}`` — or raise if unconfigured.

        Reads ``control_api.json`` fresh each time, **read-only**: a client
        never mints the daemon's secret. A missing/invalid file means the
        daemon has never generated its endpoint (never started)."""
        try:
            cfg = server._valid_control_api(
                json.loads(server._config_path().read_text())
            )
        except (OSError, ValueError):
            cfg = None
        if cfg is None:
            raise DaemonUnavailable(
                "alle daemon is not configured yet (no control endpoint). "
                "Start it: alle start"
            )
        return cfg

    def health_ok(self) -> bool:
        """True only if *our* daemon is behind the contract port (HMAC proof).

        A bare TCP connect is insufficient — the port could be squatted — so
        readiness is the same challenge ``alle ui`` uses. Never sends the
        secret; never raises."""
        try:
            api = self._endpoint()
        except CompanionError:
            return False
        return self._challenge_ok(api)

    @staticmethod
    def _challenge_ok(api: dict) -> bool:
        """The ``/health`` HMAC challenge against one endpoint: True only when
        the responder proves knowledge of the shared secret — neither side
        ever sends it. Never raises."""
        nonce = secrets.token_urlsafe(16)
        req = urllib.request.Request(
            f"http://{api['address']}/health?nonce={nonce}"  # noqa: S5332
        )
        try:
            with urllib.request.urlopen(req, timeout=1) as r:  # noqa: S310
                data = json.loads(r.read(4096))
        except (OSError, ValueError):
            return False
        proof = str((data or {}).get("proof") or "")
        return hmac.compare_digest(proof, auth.health_proof(api["secret"], nonce))

    # -- transport -----------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """One authed ``/api/v1`` round trip. Maps transport + control-API
        errors onto companion exceptions; unknown endpoints (404) raise
        :class:`CompanionError` so callers can treat them as "feature not on
        this daemon" (the version-skew contract).

        The Bearer secret is only sent after the endpoint passes the HMAC
        challenge — a squatter on the contract port learns nothing."""
        api = self._endpoint()
        if not self._challenge_ok(api):
            raise DaemonUnavailable(
                f"no alle daemon is answering the health challenge at "
                f"{api['address']} (not running, or a foreign process holds "
                "the port). Start it: alle start"
            )
        url = f"http://{api['address']}/api/v1/{path.lstrip('/')}"  # noqa: S5332
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {api['secret']}",
            # The address is 127.0.0.1:<port>, which the server's loopback
            # Host allow-list accepts; the per-install *.localhost name would
            # need its port appended to match the canonical host, so the
            # literal loopback address is simpler and equally valid.
            "Host": api["address"],
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except (OSError, ValueError):
                msg = str(e)
            if e.code == 404:
                raise CompanionError(f"endpoint /{path} not available on this daemon")
            raise CompanionError(msg) from e
        except (OSError, ValueError) as e:
            raise DaemonUnavailable(f"cannot reach the alle daemon: {e}") from e

    # -- read ----------------------------------------------------------------
    def status(self) -> dict:
        return self._request("GET", "status")

    def tray_state(self) -> TrayState:
        """The status payload projected onto :class:`TrayState`, every field
        read defensively (``.get``) so a shape from a newer/older daemon never
        raises during a menu refresh."""
        s = self.status()
        router = s.get("router") or {}
        channels = s.get("channels") or []
        return TrayState(
            running=bool(s.get("running")),
            tun=bool(router.get("tun")),
            killswitch=bool(router.get("killswitch")),
            channel_summary=_channel_summary(channels),
            channel_count=int(s.get("channel_count") or len(channels)),
            provider_count=int(
                s.get("provider_count")
                or len({c.get("provider") for c in channels if c.get("provider")})
            ),
            installed_version=(s.get("daemon") or {}).get("installed_version")
            or (s.get("daemon") or {}).get("version"),
            web_ui_url=s.get("web_ui") or None,
            router_port=router.get("port"),
        )

    def web_ui_login_url(self) -> str:
        """A one-time login URL to open the Web UI from the tray. Minted
        locally (same machine, same secret) — not an API round trip."""
        return server.mint_login_url()

    # -- lifecycle + toggles (the whole tray scope; nothing richer) ----------
    def start(self) -> dict:
        return self._request("POST", "lifecycle/start", {})

    def stop(self) -> dict:
        return self._request("POST", "lifecycle/stop", {})

    def restart(self) -> dict:
        return self._request("POST", "lifecycle/restart", {})

    def set_tun(self, enabled: bool) -> dict:
        return self._request("POST", "tun", {"enabled": bool(enabled)})

    def set_killswitch(self, enabled: bool) -> dict:
        return self._request("POST", "routes/killswitch", {"enabled": bool(enabled)})


def _channel_summary(channels: list[dict]) -> str:
    """A one-line "N healthy / M total" summary for the tray title/menu."""
    total = len(channels)
    if not total:
        return "no channels"
    healthy = sum(
        1 for c in channels if (c.get("state") or "").lower() in ("active", "healthy")
    )
    return f"{healthy}/{total} healthy"
