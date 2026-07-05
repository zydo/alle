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
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from alle import applog, paths, proc
from alle.constants import SINGBOX_SHA256, SINGBOX_VERSION


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
            member = next(
                (m for m in tf.getmembers() if m.name.endswith("/sing-box")), None
            )
            if member is None:
                raise SingBoxError(f"{asset} did not contain a sing-box binary")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                raise SingBoxError(f"{asset} sing-box archive member is not a file")
            # Stage next to the target and rename only after the checksum
            # passes: the verified path never holds a partial or wrong binary,
            # even mid-crash or with a concurrent ensure_binary() reading it.
            fd, staged = tempfile.mkstemp(dir=str(target.parent), prefix=".sing-box-")
            try:
                with os.fdopen(fd, "wb") as out, src:
                    out.write(src.read())
                got = _sha256_file(Path(staged))
                if got != expected:
                    raise SingBoxError(
                        f"sing-box checksum mismatch (expected {expected}, got {got}); "
                        "refusing to run an unverified binary."
                    )
                os.chmod(staged, 0o755)
                os.replace(staged, target)
            finally:
                if os.path.exists(staged):
                    os.unlink(staged)
    print(f"alle: verified sing-box {SINGBOX_VERSION} ({key}).", file=sys.stderr)
    return target


# ---- Clash API endpoint ------------------------------------------------------


def _clash_api_path() -> Path:
    return paths.state_dir() / "clash_api.json"


def clash_api() -> dict:
    """The local Clash API endpoint: ``{"address": "127.0.0.1:<port>", "secret"}``.

    Generated once, kept ``0600``, and shared by the config builder (which tells
    sing-box to require the secret) and the stats client. The secret gates an
    API that exposes every connection's destination and can close connections —
    without it, any local process or user could read or disturb the tunnels.
    The port is allocated from the OS instead of hard-coded so two users (or
    two ``ALLE_HOME``\\ s) on one machine don't fight over the same port.
    """
    p = _clash_api_path()
    while True:
        try:
            cfg = json.loads(p.read_text())
            address, secret_ = cfg.get("address"), cfg.get("secret")
            if address and secret_:
                return {"address": address, "secret": secret_}
            p.unlink(missing_ok=True)  # incomplete — regenerate
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            p.unlink(missing_ok=True)  # corrupt, but fully regenerable — rebuild
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        fresh = {"address": f"127.0.0.1:{port}", "secret": secrets.token_hex(16)}
        try:
            # O_EXCL: if two processes race to generate, exactly one wins and
            # the loser re-reads the winner's file on the next pass.
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            json.dump(fresh, f, indent=2)
            f.write("\n")
        return fresh


def forget_clash_api() -> None:
    """Drop the generated Clash API endpoint so the next :func:`clash_api` call
    allocates a fresh port + secret (used when another process stole the port)."""
    _clash_api_path().unlink(missing_ok=True)


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
        try:
            pid = int(pf.read_text().strip())
        except (OSError, ValueError):
            return None
        # PID-recycling guard: believe the pidfile only if the process behind
        # the number really is a sing-box (see alle.proc), so a stale file can
        # neither report a dead daemon as running nor let stop() kill a stranger.
        return pid if proc.alive_matching(pid, ("sing-box",)) else None

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

        ``sing-box check`` guards every path, cold starts included (a bad config
        used to reach the cold start and die silently right after fork): a failed
        check restores the last-known-good config and leaves whatever process
        state exists — running on the old config, or stopped — undisturbed.
        sing-box is kept running even with no inbounds (an idle daemon held alive
        by the Clash API controller), so ``alle start`` with zero channels still
        reports an active daemon. It is only stopped explicitly by ``alle stop``.
        """
        new = json.dumps(config, indent=2)
        old = self.config_path.read_text() if self.config_path.exists() else None
        if new == old and self.is_running():
            return False  # no change and the daemon is already up — leave it alone
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_protected(new)
        if not self.check():
            # sing-box rejected the new config: roll back to the last-known-good
            # file so a still-running process (on the old config) is undisturbed
            # and a stopped one isn't started on a config known to be bad.
            if old is not None:
                self._write_protected(old)
            return False
        if self.is_running():
            if self.reload():
                return True  # reloaded in place (PID + start time preserved)
            # config is valid but SIGHUP could not be delivered — full restart
        self.restart()
        return True

    def _write_protected(self, text: str) -> None:
        """Write the config, then mark it read-only so casual edits are refused.

        Created 0600 from the first byte — the config carries every channel's
        WireGuard private key, so it must never exist under the default
        (usually world-readable) umask mode, even briefly.
        """
        if self.config_path.exists():
            os.chmod(self.config_path, 0o600)  # allow our own overwrite
        fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(self.config_path, 0o400)

    def start(self) -> None:
        if self.is_running():
            return
        binary = ensure_binary()
        applog.rotate_if_needed(self.log_path, applog.MAX_LOG_BYTES)
        with open(self.log_path, "ab") as lf:
            child = subprocess.Popen(
                [str(binary), "run", "-c", str(self.config_path)],
                stdout=lf,
                stderr=lf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        _pid_path().write_text(str(child.pid))
        _started_path().write_text(str(int(time.time())))
        # A config can pass `check` yet still die at startup (a port stolen by
        # another process binds only at run time). Catch an immediate exit so
        # the failure is loud instead of a dead PID behind a fresh pidfile.
        time.sleep(0.3)
        if child.poll() is not None:
            _pid_path().unlink(missing_ok=True)
            _started_path().unlink(missing_ok=True)
            raise SingBoxError(
                f"sing-box exited immediately (code {child.returncode}); "
                f"last log lines:\n{self.logs(10)}"
            )

    def stop(self) -> None:
        pid = self.running_pid()
        if pid is None:
            _pid_path().unlink(missing_ok=True)
            _started_path().unlink(missing_ok=True)
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:  # exited between the liveness check and the signal
            pass
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
        """Validate the on-disk config with ``sing-box check``. True if it passes.

        Raises :class:`SingBoxError` when the binary itself cannot be obtained —
        that is an environmental (retryable) failure, not a config problem, and
        must not be mistaken for "config rejected" (which triggers a rollback).
        """
        binary = ensure_binary()
        result = subprocess.run(
            [str(binary), "check", "-c", str(self.config_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"alle: sing-box rejected the config:\n{result.stderr.strip()}",
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
    def connections(self) -> list[dict]:
        """Raw active connections from the Clash API, best effort.

        Each entry carries a stable ``id`` plus cumulative ``upload``/``download``
        byte counters for that connection's lifetime and a ``chains`` list naming
        the outbound(s) it exits through. Returns ``[]`` if the Clash API is
        unreachable (e.g. sing-box still starting). This is the raw feed the
        metrics accumulator turns into durable per-channel totals.
        """
        api = clash_api()
        req = urllib.request.Request(
            f"http://{api['address']}/connections",
            headers={"Authorization": f"Bearer {api['secret']}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as r:  # noqa: S310 (loopback)
                data = json.load(r)
        except (OSError, ValueError):
            return []
        return data.get("connections") or []

    def traffic(self) -> dict[str, tuple[int, int]]:
        """Map outbound tag -> (upload_bytes, download_bytes), best effort.

        Sums currently-tracked connections per outbound chain. Returns {} if the
        Clash API is unreachable (e.g. sing-box still starting).
        """
        out: dict[str, list[int]] = {}
        for c in self.connections():
            chain = c.get("chains") or []
            if not chain:
                continue
            tag = chain[0]  # outermost outbound the connection exits through
            acc = out.setdefault(tag, [0, 0])
            acc[0] += int(c.get("upload") or 0)
            acc[1] += int(c.get("download") or 0)
        return {tag: (u, d) for tag, (u, d) in out.items()}
