"""Planned macOS menu-bar companion prototype over :mod:`alle.companion`.

Deliberately tiny and rendering-only: every action delegates to
:class:`alle.companion.CompanionClient` through one coalescing background
worker, so the tray adds no capability the client does not already expose and
never blocks the AppKit callback thread. This source is retained for in-tree
development, but the released wheel deliberately excludes it and provides no
``tray`` extra or ``alle-tray`` launcher. ``rumps`` must therefore be installed
separately when exercising the prototype from a checkout.

Scope (hard contract — nothing richer ever lives here): status line, channel
summary, start/stop/restart, tun on/off, kill-switch, open Web UI. Rule editing
and everything else stays in the CLI and Web UI.

Not unit-tested through a live GUI (rumps drives a real NSStatusItem); the
client and worker concurrency logic are tested without AppKit. Quitting the tray
deactivates TUN mode (a machine-wide route table should not outlive the app
that turned it on) but never stops alled unless the user explicitly asks.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from alle.companion import CompanionClient, DaemonUnavailable

REFRESH_SECONDS = 5


class CoalescingWorker:
    """One latest-wins network worker for AppKit callbacks.

    ``submit`` never performs I/O and never queues an unbounded list: a newer
    pending status/action replaces an older pending one. Results carry their
    generation back to the main thread and stale completions are discarded.
    """

    def __init__(self, dispatch: Callable[[Callable[[], None]], None]):
        self._dispatch = dispatch
        self._condition = threading.Condition()
        self._pending: (
            tuple[int, Callable[[], Any], Callable[[bool, Any], None]] | None
        ) = None
        self._generation = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(
        self, work: Callable[[], Any], done: Callable[[bool, Any], None]
    ) -> None:
        with self._condition:
            if self._closed:
                return
            self._generation += 1
            self._pending = (self._generation, work, done)
            self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                pending = self._pending
                if pending is None:
                    continue
                generation, work, done = pending
                self._pending = None
            try:
                result = (True, work())
            except Exception as error:  # noqa: BLE001 — delivered to UI callback
                result = (False, error)
            with self._condition:
                current = generation == self._generation and not self._closed
            if current:
                self._dispatch(lambda done=done, result=result: done(*result))

    def finish(self, work, timeout=2.0):
        """Run final cleanup off-main and wait for at most ``timeout``."""
        finished = threading.Event()

        def run():
            try:
                work()
            finally:
                finished.set()

        threading.Thread(target=run, daemon=True).start()
        return finished.wait(timeout)

    def close(self):
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()


def _dispatch_main(callback):
    from Foundation import NSOperationQueue

    NSOperationQueue.mainQueue().addOperationWithBlock_(callback)


def _require_rumps():
    try:
        import rumps
    except ImportError as e:  # pragma: no cover - source-only prototype dependency
        raise SystemExit(
            "the menu-bar companion is a source-only prototype (macOS).\n"
            "For in-tree development, install rumps in the checkout environment;\n"
            "released alle installations use the CLI and Web UI."
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
            self.worker = CoalescingWorker(_dispatch_main)
            self._refresh(None)
            rumps.Timer(self._refresh, REFRESH_SECONDS).start()

        # -- rendering --
        def _refresh(self, _):
            self.worker.submit(client.tray_state, self._status_done)

        def _status_done(self, ok, value):
            if not ok:
                if isinstance(value, DaemonUnavailable):
                    self.title = "alle ○"
                    self.status_item.title = "Daemon not running — alle start"
                    self.channels_item.title = ""
                    self.tun_item.state = self.ks_item.state = False
                else:
                    self.status_item.title = f"Error: {value}"
                return
            self._render(value)

        def _render(self, st):
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
            def work():
                fn()
                return client.tray_state()

            def done(ok, value):
                if ok:
                    self._render(value)
                else:
                    _require_rumps().alert("alle", str(value))

            self.worker.submit(work, done)

        # -- actions (delegated to the client on the background worker) --
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

            def done(ok, value):
                if ok:
                    webbrowser.open(value)
                else:
                    _require_rumps().alert("alle", str(value))

            self.worker.submit(client.web_ui_login_url, done)

        def on_quit(self, _):
            # Deactivating tun is best-effort: a machine-wide route table must
            # not outlive the app that armed it. alled itself is left running.
            self.worker.finish(lambda: client.set_tun(False), timeout=2.0)
            self.worker.close()
            _require_rumps().quit_application()

    return AlleTray()


def main():
    build_app().run()


if __name__ == "__main__":  # pragma: no cover
    main()
