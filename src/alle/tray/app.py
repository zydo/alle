"""Minimal PySide6 tray/menu-bar companion.

This is intentionally small: it is one client of alle's shared service layer, not
the host of the application. Long-lived runtime work remains in the alled daemon.
"""

from __future__ import annotations

import sys

from alle import service


def _load_qt():
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError as e:  # pragma: no cover - exercised by CLI smoke, not GUI tests
        raise SystemExit(
            "PySide6 is not installed. Install the optional tray extra: alle[tray]"
        ) from e
    return QtCore, QtGui, QtWidgets


def _icon(QtCore, QtGui):
    icon = QtGui.QIcon.fromTheme("network-vpn")
    if not icon.isNull():
        return icon

    pixmap = QtGui.QPixmap(22, 22)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setBrush(QtGui.QColor("#2563eb"))
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.drawEllipse(3, 3, 16, 16)
    painter.end()
    return QtGui.QIcon(pixmap)


class TrayController:
    def __init__(self, app, QtCore, QtGui, QtWidgets):
        self.app = app
        self.QtCore = QtCore
        self.QtWidgets = QtWidgets
        self.tray = QtWidgets.QSystemTrayIcon(_icon(QtCore, QtGui), app)
        self.menu = QtWidgets.QMenu()
        self.status_action = self.menu.addAction("Status: unknown")
        self.status_action.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Start alle", self.start)
        self.menu.addAction("Stop alle", self.stop)
        self.menu.addAction("Restart alle", self.restart)
        self.menu.addSeparator()
        self.menu.addAction("Quit companion", app.quit)
        self.tray.setContextMenu(self.menu)

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)

    def show(self):
        self.tray.show()
        self.refresh()

    def refresh(self):
        try:
            snapshot = service.status_snapshot()
            state = "running" if snapshot["running"] else "stopped"
            channels = snapshot["channel_count"]
            label = f"Status: {state} ({channels} channel(s))"
        except Exception as e:  # noqa: BLE001 - a tray label should survive service errors
            label = f"Status: error ({e})"
        self.status_action.setText(label)
        self.tray.setToolTip(label.replace("Status: ", "alle: "))

    def start(self):
        self._run(service.start, "alle start requested")

    def stop(self):
        self._run(service.stop, "alle stop requested")

    def restart(self):
        self._run(service.restart, "alle restart requested")

    def _run(self, func, message: str):
        try:
            func()
            self.tray.showMessage("alle", message)
        except Exception as e:  # noqa: BLE001 - show user-facing tray errors
            self.tray.showMessage("alle", str(e))
        self.refresh()


def main(argv: list[str] | None = None) -> int:
    QtCore, QtGui, QtWidgets = _load_qt()
    app = QtWidgets.QApplication(argv or sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray is available in this desktop session.", file=sys.stderr)
        return 1

    controller = TrayController(app, QtCore, QtGui, QtWidgets)
    controller.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
