"""Manage the single pinned sing-box process: binary, config, lifecycle, stats.

alle runs exactly one sing-box instance. Each enabled channel becomes a local
``mixed`` (HTTP+SOCKS) inbound routed to its own WireGuard ``endpoint`` outbound.
Topology changes (add/remove) rewrite the config file and restart this one
process — sub-second, versus one process per channel.

The binary is the upstream release pinned in ``constants`` (GPL-3.0; we invoke it,
never link it — see THIRD_PARTY_NOTICES.md), downloaded once into
``~/.alle/bin/sing-box@<version>`` and checksum-verified. Any other sing-box on
PATH is ignored on purpose, so behaviour is identical on every machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from alle import paths
from alle.constants import CLASH_API_ADDRESS, SINGBOX_SHA256, SINGBOX_VERSION


class SingBoxError(RuntimeError):
    """Raised when the sing-box binary cannot be obtained or started."""


# ---- platform / binary -----------------------------------------------------


def host_platform() -> str:
    """This machine as ``<os>-<arch>`` (e.g. ``darwin-arm64``), or raise."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    key = f"{system}-{arch}" if arch else ""
    if key not in SINGBOX_SHA256:
        supported = ", ".join(sorted(SINGBOX_SHA256))
        raise SingBoxError(
            f"unsupported platform {system}/{machine}. alle ships sing-box for: {supported}."
        )
    return key


def _bin_path() -> Path:
    return paths.state_dir() / "bin" / f"sing-box@{SINGBOX_VERSION}"


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "alle/1"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (pinned https URL)
        dest.write_bytes(r.read())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_binary() -> Path:
    """Return the path to our pinned, checksum-verified sing-box.

    The on-disk binary is re-verified against the pinned SHA-256 on *every* call,
    not just right after download — so corruption or post-download tampering is
    caught, and a binary you pre-provisioned yourself at the expected path is
    accepted only if its bytes match.
    """
    key = host_platform()
    expected = SINGBOX_SHA256[key]
    target = _bin_path()
    if target.exists():
        if _sha256_file(target) == expected:
            return target
        print(
            f"alle: sing-box at {target} failed checksum verification — "
            "re-downloading the pinned build.",
            file=sys.stderr,
        )
        target.unlink()
    return _install(key, expected, target)


def _install(key: str, expected: str, target: Path) -> Path:
    asset = f"sing-box-{SINGBOX_VERSION}-{key}.tar.gz"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{SINGBOX_VERSION}/{asset}"
    # explicit, non-silent: tell the user exactly what is being fetched and verified
    print(
        f"alle: downloading sing-box {SINGBOX_VERSION} ({key}) from {url}\n"
        f"          will verify SHA-256 {expected} before use",
        file=sys.stderr,
    )
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset
        try:
            _download(url, archive)
        except OSError as e:
            raise SingBoxError(f"could not download sing-box from {url}: {e}") from e
        # the tarball is <dir>/sing-box (+ LICENSE); extract just the binary
        with tarfile.open(archive) as tf:
            member = next((m for m in tf.getmembers() if m.name.endswith("/sing-box")), None)
            if member is None:
                raise SingBoxError(f"{asset} did not contain a sing-box binary")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(member) as src:
                target.write_bytes(src.read())
    target.chmod(0o755)
    got = _sha256_file(target)
    if got != expected:
        target.unlink(missing_ok=True)
        raise SingBoxError(
            f"sing-box checksum mismatch (expected {expected}, got {got}); "
            "refusing to run an unverified binary."
        )
    print(f"alle: verified sing-box {SINGBOX_VERSION} ({key}).", file=sys.stderr)
    return target


# ---- process lifecycle -----------------------------------------------------


def _config_path() -> Path:
    return paths.state_dir() / "singbox.json"


def _pid_path() -> Path:
    return paths.state_dir() / "singbox.pid"


def _started_path() -> Path:
    return paths.state_dir() / "singbox.started"


def _log_path() -> Path:
    return paths.state_dir() / "singbox.log"


class Runner:
    """Owns the one sing-box daemon, addressed by a pidfile so it survives a
    CLI process exit while channels keep running."""

    def __init__(self) -> None:
        self.config_path = _config_path()
        self.log_path = _log_path()

    def running_pid(self) -> int | None:
        pf = _pid_path()
        if not pf.exists():
            return None
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            return None

    def is_running(self) -> bool:
        return self.running_pid() is not None

    def config_exists(self) -> bool:
        return self.config_path.exists()

    def started_at(self) -> int | None:
        """Epoch when the current sing-box process started (shared by all channels)."""
        try:
            return int(_started_path().read_text().strip())
        except (OSError, ValueError):
            return None

    def live_refs(self) -> set[tuple[str, str]]:
        """``(provider, id)`` of every channel present in the live config."""
        from alle.state import tag_to_ref

        try:
            cfg = json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return set()
        refs = set()
        for inb in cfg.get("inbounds") or []:
            ref = tag_to_ref(inb.get("tag", ""))
            if ref:
                refs.add(ref)
        return refs

    def apply(self, config: dict) -> bool:
        """Write the config and (re)start sing-box to match it.

        Restarts only when the config actually changed (so a no-op reconcile never
        blips live tunnels) or the process isn't running. Returns True if the live
        process was started or restarted as a result.

        The config file is written read-only (``0400``) after each update so it is
        not edited out from under the running process by accident — alle
        relaxes the mode only when it rewrites the file itself.

        sing-box is kept running even with no inbounds (an idle daemon held alive
        by the Clash API controller), so ``alle start`` with zero channels still
        reports an active daemon. It is only stopped explicitly by ``alle stop``.
        """
        new = json.dumps(config, indent=2)
        old = self.config_path.read_text() if self.config_path.exists() else None
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_protected(new)
        if new == old and self.is_running():
            return False
        if self.is_running():
            # Apply in place via SIGHUP: sing-box re-reads the whole config and
            # rebuilds without re-exec (PID and start time preserved, no bind-race
            # during a stop→start gap). In-flight connections on changed outbounds
            # may drop — there is no zero-interruption full-config swap — but it is
            # the least disruptive way to add/remove a channel. Guarded by
            # ``sing-box check`` so a bad config never takes down a working process;
            # fall back to a full restart if the reload couldn't be signalled.
            if self.check() and self.reload():
                return True
        self.restart()
        return True

    def _write_protected(self, text: str) -> None:
        """Write the config, then mark it read-only so casual edits are refused."""
        if self.config_path.exists():
            os.chmod(self.config_path, 0o600)  # allow our own overwrite
        self.config_path.write_text(text)
        os.chmod(self.config_path, 0o400)

    def start(self) -> None:
        if self.is_running():
            return
        binary = ensure_binary()
        with open(self.log_path, "ab") as lf:
            proc = subprocess.Popen(
                [str(binary), "run", "-c", str(self.config_path)],
                stdout=lf,
                stderr=lf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        _pid_path().write_text(str(proc.pid))
        _started_path().write_text(str(int(time.time())))

    def stop(self) -> None:
        pid = self.running_pid()
        if pid is None:
            _pid_path().unlink(missing_ok=True)
            _started_path().unlink(missing_ok=True)
            return
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            if self.running_pid() is None:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _pid_path().unlink(missing_ok=True)
        _started_path().unlink(missing_ok=True)

    def restart(self) -> None:
        self.stop()
        self.start()

    def check(self) -> bool:
        """Validate the on-disk config with ``sing-box check``. True if it passes."""
        try:
            binary = ensure_binary()
        except SingBoxError:
            return False
        proc = subprocess.run(
            [str(binary), "check", "-c", str(self.config_path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(
                f"alle: sing-box rejected the config:\n{proc.stderr.strip()}",
                file=sys.stderr,
            )
            return False
        return True

    def reload(self) -> bool:
        """Tell a running sing-box to reload its config in place (SIGHUP)."""
        pid = self.running_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGHUP)
            return True
        except OSError:
            return False

    def logs(self, tail: int = 80) -> str:
        if not self.log_path.exists():
            return "(no logs)"
        lines = self.log_path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-tail:]).strip() or "(no logs)"

    # ---- traffic stats via the Clash API -----------------------------------
    def traffic(self) -> dict[str, tuple[int, int]]:
        """Map outbound tag -> (upload_bytes, download_bytes), best effort.

        Sums currently-tracked connections per outbound chain. Returns {} if the
        Clash API is unreachable (e.g. sing-box still starting).
        """
        url = f"http://{CLASH_API_ADDRESS}/connections"
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 (loopback)
                data = json.load(r)
        except (OSError, ValueError):
            return {}
        out: dict[str, list[int]] = {}
        for c in data.get("connections") or []:
            chain = c.get("chains") or []
            if not chain:
                continue
            tag = chain[0]  # outermost outbound the connection exits through
            acc = out.setdefault(tag, [0, 0])
            acc[0] += int(c.get("upload") or 0)
            acc[1] += int(c.get("download") or 0)
        return {tag: (u, d) for tag, (u, d) in out.items()}
