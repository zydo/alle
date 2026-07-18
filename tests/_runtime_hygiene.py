"""Ownership and cleanup for processes started by the pytest suite.

This is deliberately test infrastructure, not a production cleanup command.
Only directories carrying our exact marker below a private pytest base are
eligible, and only PIDs whose recorded kernel start time still matches are
ever signalled.  Ambiguity preserves the directory and process.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from alle import proc

MARKER = ".alle-pytest-session.json"
HOME_MARKER = ".alle-pytest-home.json"
REGISTRY = ".alle-pytest-processes.json"
FORMAT = 1
LARGE_BINARY = 8 << 20
SESSION_BUDGET = 128 << 20

# Tests deliberately monkeypatch process/filesystem primitives through modules
# that import ``os``.  Module objects are shared, so teardown must retain the
# real primitives captured before any test can replace them.
_OS_KILL = os.kill
_OS_REPLACE = os.replace
_OS_WAITPID = os.waitpid
_START_TIME_OF = proc.start_time_of
_SUBPROCESS_RUN = subprocess.run


def _atomic_json(path: Path, value: object) -> None:
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    staged.write_text(json.dumps(value, sort_keys=True))
    os.chmod(staged, 0o600)
    _OS_REPLACE(staged, path)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _exact_record(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    pid, start = value.get("pid"), value.get("start")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(start, str)
        or not start
    ):
        return None
    return {"pid": pid, "start": start}


def record_is_live(value: object) -> bool:
    """Require exact PID + kernel start time; legacy/ambiguous means false."""
    record = _exact_record(value)
    return bool(record and _START_TIME_OF(record["pid"]) == record["start"])


def _reap_if_child(pid: int) -> None:
    try:
        _OS_WAITPID(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def terminate_exact(value: object, *, timeout: float = 5.0) -> bool:
    """Stop only the exact process identity in ``value``.

    Returns true when the process is gone.  A malformed or stale record is
    already non-live and needs no signal.
    """
    record = _exact_record(value)
    if record is None or not record_is_live(record):
        return True
    pid = record["pid"]
    if pid == os.getpid():
        return False
    try:
        _OS_KILL(pid, signal.SIGTERM)
    except OSError:
        return not record_is_live(record)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not record_is_live(record):
            _reap_if_child(pid)
            return True
        time.sleep(0.05)
    if record_is_live(record):
        try:
            _OS_KILL(pid, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and record_is_live(record):
        time.sleep(0.05)
    _reap_if_child(pid)
    return not record_is_live(record)


def _pidfile_record(path: Path) -> dict | None:
    value = _read_json(path)
    return _exact_record(value)


def _registry_entries(root: Path) -> list[dict]:
    value = _read_json(root / REGISTRY)
    if not isinstance(value, list):
        return []
    entries = []
    for item in value:
        if not isinstance(item, dict):
            continue
        record = _exact_record(item.get("record"))
        home = item.get("home")
        kind = item.get("kind")
        if record and isinstance(home, str) and kind in {"applier", "singbox"}:
            entries.append({"record": record, "home": home, "kind": kind})
    return entries


def _scan_pidfiles(root: Path) -> list[dict]:
    entries: list[dict] = []
    for kind, filename in (("applier", "applier.pid"), ("singbox", "singbox.pid")):
        for path in root.glob(f"home-*/state/{filename}"):
            if not _inside(path, root):
                continue
            record = _pidfile_record(path)
            if record:
                entries.append(
                    {"record": record, "home": str(path.parent), "kind": kind}
                )
    return entries


def _process_entries(root: Path) -> list[dict]:
    marker = _owned_session(root, root.parent)
    token = marker.get("token") if marker else None
    unique: dict[tuple[int, str], dict] = {}
    for entry in [*_registry_entries(root), *_scan_pidfiles(root)]:
        home = Path(entry["home"])
        if not _inside(home, root):
            continue
        record = entry["record"]
        command = proc.command_of(record["pid"]) if record_is_live(record) else None
        if command is not None:
            if not isinstance(token, str) or not _process_has_test_identity(
                record["pid"], token, home
            ):
                continue
            if entry["kind"] == "applier" and not any(
                marker in command for marker in ("-m alle applier", "alle applier")
            ):
                continue
            config = str(home / "singbox.json")
            if entry["kind"] == "singbox" and not (
                "sing-box" in command and config in command
            ):
                continue
        unique[(record["pid"], record["start"])] = entry
    return list(unique.values())


def _process_has_test_identity(pid: int, token: str, home: Path) -> bool:
    """Prove the process inherited this session token and exact test home."""
    expected = {
        "ALLE_TEST_SESSION": token,
        "ALLE_TEST_HOME": str(home),
        "ALLE_HOME": str(home),
    }
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        raw = b""
    if raw:
        environment = {}
        for item in raw.split(b"\0"):
            key, separator, value = item.partition(b"=")
            if separator:
                environment[key.decode(errors="replace")] = value.decode(
                    errors="replace"
                )
        return all(environment.get(key) == value for key, value in expected.items())
    try:
        result = _SUBPROCESS_RUN(
            [proc.PS, "eww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Never return or log this text: BSD ps appends the whole environment.
    text = result.stdout
    return all(f" {key}={value}" in text for key, value in expected.items())


def _owned_session(root: Path, base: Path) -> dict | None:
    if (
        root.is_symlink()
        or not root.is_dir()
        or root.parent.resolve() != base.resolve()
    ):
        return None
    value = _read_json(root / MARKER)
    if not isinstance(value, dict):
        return None
    if (
        value.get("format") != FORMAT
        or value.get("root") != str(root.resolve())
        or not isinstance(value.get("token"), str)
        or not value["token"]
    ):
        return None
    return value


def _stop_session_processes(root: Path) -> list[dict]:
    entries = _process_entries(root)
    marker = _owned_session(root, root.parent)
    owner = _exact_record(marker.get("owner")) if marker else None
    if owner is not None:
        entries = [entry for entry in entries if entry["record"] != owner]
    # The applier deliberately leaves sing-box alive for adoption, so stop it
    # first and the data plane second.
    entries.sort(key=lambda item: 0 if item["kind"] == "applier" else 1)
    for entry in entries:
        terminate_exact(entry["record"])
    return [entry for entry in entries if record_is_live(entry["record"])]


def recover_stale_sessions(base: Path) -> list[Path]:
    """Recover dead marked sessions; preserve live or ambiguous ones."""
    recovered: list[Path] = []
    if not base.exists() or base.is_symlink():
        return recovered
    for root in sorted(base.iterdir()):
        marker = _owned_session(root, base)
        if marker is None:
            continue
        owner = _exact_record(marker.get("owner"))
        if owner is None or record_is_live(owner):
            continue
        if _stop_session_processes(root):
            continue
        shutil.rmtree(root)
        recovered.append(root)
    return recovered


class RuntimeSession:
    """One pytest invocation's private homes and exact process registry."""

    def __init__(self, base: Path | None = None):
        self.base = base or Path(tempfile.gettempdir()) / "alle-pytest-sessions"
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.base, 0o700)
        recover_stale_sessions(self.base)
        self.root = self.base / f"session-{os.getpid()}-{uuid.uuid4().hex}"
        self.root.mkdir(mode=0o700)
        self.token = uuid.uuid4().hex
        owner = proc.record(os.getpid())
        if _exact_record(owner) is None:
            raise RuntimeError("pytest process has no exact start-time identity")
        _atomic_json(
            self.root / MARKER,
            {
                "format": FORMAT,
                "root": str(self.root.resolve()),
                "owner": owner,
                "token": self.token,
            },
        )
        _atomic_json(self.root / REGISTRY, [])

    def new_home(self) -> Path:
        parent = Path(tempfile.mkdtemp(prefix="home-", dir=self.root))
        os.chmod(parent, 0o700)
        _atomic_json(
            parent / HOME_MARKER,
            {
                "format": FORMAT,
                "session": str(self.root.resolve()),
                "token": self.token,
            },
        )
        return parent / "state"

    def _assert_home(self, home: Path) -> Path:
        parent = home.parent
        marker = _read_json(parent / HOME_MARKER)
        if (
            home.name != "state"
            or not _inside(home, self.root)
            or not isinstance(marker, dict)
            or marker.get("format") != FORMAT
            or marker.get("session") != str(self.root.resolve())
            or marker.get("token") != self.token
        ):
            raise RuntimeError(f"refusing cleanup outside this pytest session: {home}")
        return parent

    def refresh_registry(self) -> list[dict]:
        entries = [
            entry
            for entry in _process_entries(self.root)
            if record_is_live(entry["record"])
        ]
        _atomic_json(self.root / REGISTRY, entries)
        return entries

    def cleanup_home(self, home: Path) -> None:
        parent = self._assert_home(home)
        for _ in range(3):
            live = [
                entry for entry in self.refresh_registry() if entry["home"] == str(home)
            ]
            if not live:
                break
            live.sort(key=lambda item: 0 if item["kind"] == "applier" else 1)
            for entry in live:
                if not terminate_exact(entry["record"]):
                    raise RuntimeError(
                        f"test {entry['kind']} pid "
                        f"{entry['record']['pid']} survived cleanup"
                    )
        else:
            raise RuntimeError(f"test processes kept appearing during cleanup: {home}")
        for name in (
            "applier.pid",
            "applier.info.json",
            "singbox.pid",
            "singbox.started",
            "helper.sock",
        ):
            (home / name).unlink(missing_ok=True)
        if home.exists():
            for socket_path in home.glob("*.sock"):
                socket_path.unlink(missing_ok=True)
            large = [
                path
                for path in home.rglob("sing-box@*")
                if path.is_file() and path.stat().st_size >= LARGE_BINARY
            ]
            if large:
                raise AssertionError(
                    "test home downloaded a private sing-box instead of using the "
                    f"session binary: {large}"
                )
        shutil.rmtree(parent)
        self.refresh_registry()

    def close(self) -> None:
        for marker in sorted(self.root.glob(f"home-*/{HOME_MARKER}")):
            self.cleanup_home(marker.parent / "state")
        live = [
            entry
            for entry in _process_entries(self.root)
            if record_is_live(entry["record"])
        ]
        if live:
            raise AssertionError(f"live alle test processes at suite end: {live}")
        binaries = [
            path
            for path in self.root.rglob("sing-box@*")
            if path.is_file() and path.stat().st_size >= LARGE_BINARY
        ]
        if len(binaries) > 1:
            raise AssertionError(f"duplicate large sing-box test binaries: {binaries}")
        allocated = sum(
            path.stat().st_size for path in self.root.rglob("*") if path.is_file()
        )
        if allocated > SESSION_BUDGET:
            raise AssertionError(
                f"pytest runtime root used {allocated} bytes (budget {SESSION_BUDGET})"
            )
        shutil.rmtree(self.root)
        try:
            self.base.rmdir()
        except OSError:
            pass


class RuntimeHandle:
    """Explicit capability for a test that is allowed to spawn the applier."""

    def __init__(self, session: RuntimeSession, home: Path, ensure_running):
        self.session = session
        self.home = home
        self._ensure_running = ensure_running
        self.enabled = False
        self.blocked_calls = 0
        self.cleaned = False

    def dispatch(self) -> None:
        if not self.enabled:
            self.blocked_calls += 1
            return None
        result = self._ensure_running()
        self.session.refresh_registry()
        return result

    def start(self) -> None:
        if not self.enabled:
            raise RuntimeError("request background_runtime before starting an applier")
        self.dispatch()
        self.wait_for("applier")

    def wait_for(self, kind: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            entries = self.session.refresh_registry()
            for entry in entries:
                if (
                    entry["kind"] == kind
                    and entry["home"] == str(self.home)
                    and record_is_live(entry["record"])
                ):
                    return entry
            time.sleep(0.05)
        raise AssertionError(
            f"{kind} did not start for test home <test-home>\n"
            f"{self._timeout_diagnostics()}"
        )

    def _sanitize(self, text: str) -> str:
        replacements = (
            (self.session.token, "<session-token>"),
            (str(self.home), "<test-home>"),
            (str(self.session.root), "<test-session>"),
        )
        for value, replacement in replacements:
            text = text.replace(value, replacement)
        return "".join(
            char if char.isprintable() or char == "\t" else "?" for char in text
        )

    def _pid_diagnostic(self, kind: str) -> str:
        path = self.home / f"{kind}.pid"
        if not path.exists():
            return f"{kind}.pid=absent"
        record = _pidfile_record(path)
        if record is None:
            return f"{kind}.pid=invalid"
        if not record_is_live(record):
            return f"{kind}.pid={record['pid']} live=false"
        command = proc.command_of(record["pid"])
        if kind == "applier":
            shape = command is not None and any(
                marker in command for marker in ("-m alle applier", "alle applier")
            )
        else:
            shape = command is not None and (
                "sing-box" in command and str(self.home / "singbox.json") in command
            )
        identity = _process_has_test_identity(
            record["pid"], self.session.token, self.home
        )
        return (
            f"{kind}.pid={record['pid']} live=true "
            f"command_shape={shape} session_identity={identity}"
        )

    def _log_diagnostic(self, name: str) -> str:
        path = self.home / name
        try:
            lines = path.read_text(errors="replace").splitlines()[-10:]
        except OSError:
            return f"{name}=absent"
        excerpt = self._sanitize("\n".join(lines))[-2000:]
        return f"{name}=\n{excerpt or '(empty)'}"

    def _timeout_diagnostics(self) -> str:
        parts = [self._pid_diagnostic(kind) for kind in ("applier", "singbox")]
        info = _read_json(self.home / "applier.info.json")
        if isinstance(info, dict):
            status = info.get("singbox")
            detail = info.get("detail")
            if isinstance(status, str):
                summary = f"applier.info.singbox={self._sanitize(status)}"
                if isinstance(detail, str) and detail:
                    summary += f" detail={self._sanitize(detail)[:500]}"
                parts.append(summary)
        else:
            parts.append("applier.info=absent")
        parts.extend(
            self._log_diagnostic(name)
            for name in ("alle.log", "applier.log", "singbox.log")
        )
        return "\n".join(parts)

    def stop(self) -> None:
        if self.cleaned:
            return
        self.session.cleanup_home(self.home)
        self.cleaned = True
