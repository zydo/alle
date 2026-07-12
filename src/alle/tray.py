"""macOS menu-bar companion — a v1 thin-client spike over :mod:`alle.companion`.

Deliberately tiny and rendering-only: every action is a one-liner onto
:class:`alle.companion.CompanionClient`, so the tray adds no capability the
client does not already expose (the tray-scope guardrail). ``rumps`` is an
optional dependency (``pip install alle-proxy[tray]``, macOS only); importing
this module without it raises a clear message rather than a bare ImportError.

Scope (hard contract — nothing richer ever lives here): status line, channel
summary, start/stop/restart, tun on/off, kill-switch, open Web UI. Rule editing
and everything else stays in the CLI and Web UI.

Not unit-tested through a live GUI (rumps drives a real NSStatusItem); the
logic that *is* tested lives in :mod:`alle.companion`. Quitting the tray
deactivates TUN mode (a machine-wide route table should not outlive the app
that turned it on) but never stops alled unless the user explicitly asks.
"""

from __future__ import annotations

from alle.companion import CompanionClient, CompanionError, DaemonUnavailable

REFRESH_SECONDS = 5


def _require_rumps():
    try:
        import rumps
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise SystemExit(
            "the menu-bar companion needs the optional 'tray' extra (macOS):\n"
            "  pip install 'alle-proxy[tray]'\n"
            "or use the CLI (alle status / alle tun) and the Web UI (alle ui)."
        ) from e
    return rumps


def build_app():
    """Construct the rumps App wired to a :class:`CompanionClient`.

    Split out from :func:`main` so the wiring is importable for a smoke check
    without entering rumps' run loop."""
    rumps = _require_rumps()
    client = CompanionClient()

    class AlleTray(rumps.App):
        def __init__(self):
            # quit_button=None is rumps' documented way to remove the default
            # Quit item (its annotation says str, hence the ignore) — ours
            # below deactivates tun before quitting.
            super().__init__("alle", quit_button=None)  # type: ignore[arg-type]
            self.status_item = rumps.MenuItem("Starting…")
            self.channels_item = rumps.MenuItem("")
            self.tun_item = rumps.MenuItem("System VPN (TUN)", callback=self.toggle_tun)
            self.ks_item = rumps.MenuItem("Kill-switch", callback=self.toggle_ks)
            self.menu = [
                self.status_item,
                self.channels_item,
                None,
                self.tun_item,
                self.ks_item,
                None,
                rumps.MenuItem("Start", callback=self.on_start),
                rumps.MenuItem("Stop", callback=self.on_stop),
                rumps.MenuItem("Restart", callback=self.on_restart),
                None,
                rumps.MenuItem("Open Web UI…", callback=self.open_web_ui),
                None,
                rumps.MenuItem("Quit alle tray", callback=self.on_quit),
            ]
            self._refresh(None)
            rumps.Timer(self._refresh, REFRESH_SECONDS).start()

        # -- rendering --
        def _refresh(self, _):
            try:
                st = client.tray_state()
            except DaemonUnavailable:
                self.title = "alle ○"
                self.status_item.title = "Daemon not running — alle start"
                self.channels_item.title = ""
                self.tun_item.state = self.ks_item.state = False
                return
            except CompanionError as e:
                self.status_item.title = f"Error: {e}"
                return
            self.title = "alle ●" if st.running else "alle ○"
            ver = f" v{st.installed_version}" if st.installed_version else ""
            self.status_item.title = ("Running" if st.running else "Stopped") + ver
            self.channels_item.title = f"Channels: {st.channel_summary}"
            self.tun_item.state = st.tun
            self.tun_item.title = (
                "System VPN (TUN) — system-wide" if st.tun else "System VPN (TUN)"
            )
            self.ks_item.state = st.killswitch

        def _act(self, fn):
            rumps = _require_rumps()
            try:
                fn()
            except CompanionError as e:
                rumps.alert("alle", str(e))
            self._refresh(None)

        # -- actions (each a one-liner onto the client; no logic here) --
        def toggle_tun(self, sender):
            self._act(lambda: client.set_tun(not sender.state))

        def toggle_ks(self, sender):
            self._act(lambda: client.set_killswitch(not sender.state))

        def on_start(self, _):
            self._act(client.start)

        def on_stop(self, _):
            self._act(client.stop)

        def on_restart(self, _):
            self._act(client.restart)

        def open_web_ui(self, _):
            import webbrowser

            try:
                webbrowser.open(client.web_ui_login_url())
            except CompanionError as e:
                _require_rumps().alert("alle", str(e))

        def on_quit(self, _):
            # Deactivating tun is best-effort: a machine-wide route table must
            # not outlive the app that armed it. alled itself is left running.
            try:
                client.set_tun(False)
            except CompanionError:
                pass
            _require_rumps().quit_application()

    return AlleTray()


def main():
    build_app().run()


if __name__ == "__main__":  # pragma: no cover
    main()
