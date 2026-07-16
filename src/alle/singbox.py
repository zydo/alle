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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from alle import applog, fsio, paths, proc
from alle.constants import SINGBOX_SHA256, SINGBOX_VERSION


class SingBoxError(RuntimeError):
    """Raised when the sing-box binary cannot be obtained or started."""


class SingBoxRuntimeError(SingBoxError):
    """The process failed at runtime (e.g. exited immediately after start) —
    distinct from environmental failures like an unobtainable binary, because
    the message carries the process's own log tail (port conflicts etc.)."""


class ConfigRejectedError(SingBoxError):
    """sing-box refused the generated config (``sing-box check`` failed).

    Deterministic: the same state compiles to the same config, so retrying on
    a timer cannot help — only a state change (or an alle upgrade) can."""


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


def bin_path() -> Path:
    """Where the pinned sing-box lives (whether or not it's downloaded yet).
    Public: the CLI prints it (``alle version --singbox-path``) and the TUN
    privilege gate inspects its file capabilities.

    ``ALLE_SINGBOX`` points at a pre-provisioned binary instead (the container
    image bakes one outside the state volume; air-gapped hosts stage their
    own). The trust model is unchanged: the bytes are still verified against
    the pinned SHA-256 on every start, and an override that fails is an error
    — never a fallback download over (or beside) a path the user chose.
    """
    override = os.environ.get("ALLE_SINGBOX")
    if override:
        return Path(override)
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
    target = bin_path()
    if os.environ.get("ALLE_SINGBOX"):
        # An explicit override is a statement, not a cache: verify it, but on
        # any failure raise instead of downloading — silently replacing (or
        # shadowing) a path the operator chose would defeat the point.
        if not target.exists():
            raise SingBoxError(f"ALLE_SINGBOX={target} does not exist")
        got = _sha256_file(target)
        if got != expected:
            raise SingBoxError(
                f"ALLE_SINGBOX={target} is not the pinned sing-box "
                f"{SINGBOX_VERSION} ({key}): expected SHA-256 {expected}, "
                f"got {got}"
            )
        return target
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
                os.chmod(staged, 0o755)  # noqa: S2612
                os.replace(staged, target)
            finally:
                if os.path.exists(staged):
                    os.unlink(staged)
    print(f"alle: verified sing-box {SINGBOX_VERSION} ({key}).", file=sys.stderr)
    return target


# ---- Clash API endpoint ------------------------------------------------------


def _clash_api_path() -> Path:
    return paths.state_dir() / "clash_api.json"


def _valid_clash_api(cfg) -> dict | None:
    """The validated endpoint dict, or None if ``cfg`` is missing/malformed."""
    if not isinstance(cfg, dict):
        return None
    address, secret_ = cfg.get("address"), cfg.get("secret")
    if isinstance(address, str) and address and isinstance(secret_, str) and secret_:
        return {"address": address, "secret": secret_}
    return None


def clash_api() -> dict:
    """The local Clash API endpoint: ``{"address": "127.0.0.1:<port>", "secret"}``.

    Generated once, kept ``0600``, and shared by the config builder (which tells
    sing-box to require the secret) and the stats client. The secret gates an
    API that exposes every connection's destination and can close connections —
    without it, any local process or user could read or disturb the tunnels.
    The port is allocated from the OS instead of hard-coded so two users (or
    two ``ALLE_HOME``\\ s) on one machine don't fight over the same port.

    Locking and durable publishing live in :func:`alle.fsio.generated_endpoint`
    — concurrent callers agree on one endpoint, and a reader never sees a
    half-written file.
    """

    def generate() -> dict:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        return {"address": f"127.0.0.1:{port}", "secret": secrets.token_hex(16)}

    return fsio.generated_endpoint(_clash_api_path(), _valid_clash_api, generate)


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


class ApplyOutcome(Enum):
    """What :meth:`Runner.apply` actually achieved — a delivered signal is not
    proof of a healthy applied generation, so the boolean it replaced could not
    distinguish "nothing to do" from "config refused"."""

    UNCHANGED = "unchanged"  # config identical and the process is healthy
    APPLIED = "applied"  # new config live, process + control API verified
    REJECTED = "rejected"  # `sing-box check` refused it; last-known-good kept
    RUNTIME_FAILED = "runtime_failed"  # passed check but failed at runtime


@dataclass(frozen=True)
class ApplyResult:
    outcome: ApplyOutcome
    detail: str = ""  # rejection/runtime error text (log tail, check stderr)


class Runner:
    """Owns the one sing-box daemon, addressed by a pidfile so it survives a
    CLI process exit while channels keep running."""

    def __init__(self, local_only: bool = False) -> None:
        self.config_path = _config_path()
        self.log_path = _log_path()
        self._check_error = ""  # stderr of the last failed `sing-box check`
        # local_only skips all helper delegation: used by the privileged helper
        # itself, which IS the privileged actor and would otherwise recurse
        # (Runner → helper.request → helper's Runner → helper.request …).
        self.local_only = local_only

    def _ownership(self) -> tuple[str | None, int | None]:
        """Who owns the live sing-box, and its pid: ``("local", pid)`` for a
        user-owned process, ``("helper", pid)`` for one the privileged helper
        is running as root, or ``(None, None)``.

        Local is checked FIRST because it is the common case (explicit-proxy
        mode, tun off): the pidfile read is cheap and local, and it avoids a
        helper round-trip on every running_pid/probe/status_snapshot call. The
        helper is consulted only when no local sing-box is live — which is
        exactly when tun mode is on and the process is root-owned (the user
        pidfile read returns None then, because os.kill on a root pid gets
        EPERM, which proc.verify reads as "not ours").
        """
        local = proc.read_pidfile(_pid_path(), ("sing-box",))
        if local is not None:
            return ("local", local)
        if self.local_only:
            return (None, None)
        h = self._helper_owned_pid()
        return ("helper", h) if h else (None, None)

    def running_pid(self) -> int | None:
        return self._ownership()[1]

    def _helper_owned_pid(self) -> int | None:
        """The pid of a sing-box the privileged helper is running, or None.

        None covers every non-tun situation: no helper installed, helper
        installed but idle (tun off), the helper unreachable, or local_only
        mode (the helper itself using a Runner — it must not ask itself).
        When it returns a pid, the live sing-box is root-owned and *only* the
        helper can signal it — callers must delegate start/stop/reload.
        """
        if self.local_only:
            return None
        from alle import helper

        st = helper.request("status")
        if st.get("ok") and st.get("running"):
            pid = st.get("pid")
            if isinstance(pid, int):
                return pid
        return None

    def is_running(self) -> bool:
        return self.running_pid() is not None

    def apply(self, config: dict) -> ApplyResult:
        """Write the config and (re)start sing-box to match it.

        Restarts only when the config actually changed (so a no-op reconcile
        never blips live tunnels) or the process isn't running. ``APPLIED``
        means the new generation is *verified* live — process up and control
        API answering — not merely that a signal was delivered (the pinned
        1.13.x closes the old instance and exits when the replacement fails to
        start on reload).

        The config file is written read-only (``0400``) after each update so it is
        not edited out from under the running process by accident — alle
        relaxes the mode only when it rewrites the file itself.

        ``sing-box check`` guards every path, cold starts included (a bad config
        used to reach the cold start and die silently right after fork): a
        failed check restores the last-known-good config, leaves a
        still-running process (on the old config) undisturbed, and — when the
        process is *not* running — restarts it on that last-known-good
        generation rather than leaving every tunnel down; the rejection is
        still reported. sing-box is kept running even with no inbounds (an idle
        daemon held alive by the Clash API controller), so ``alle start`` with
        zero channels still reports an active daemon. It is only stopped
        explicitly by ``alle stop``.
        """
        new = json.dumps(config, indent=2)
        old = self.config_path.read_text() if self.config_path.exists() else None
        if new == old and self.is_running():
            return ApplyResult(ApplyOutcome.UNCHANGED)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_protected(new)
        if not self.check():
            if old is not None:
                self._write_protected(old)
                if not self.is_running():
                    try:
                        self.restart()  # keep the previous generation alive
                    except SingBoxError:
                        pass  # reported as rejected; supervision keeps retrying
            return ApplyResult(ApplyOutcome.REJECTED, self._check_error)
        # Privileged-helper delegation: a tun inbound needs root, which only
        # the helper can provide; and once the helper owns sing-box only it
        # can signal the process. So the apply path forks on whether this
        # config wants the helper AND the helper is installed. Everything
        # else — config write, `check`, the Clash-API health probe — is
        # uid-agnostic and stays local either way.
        from alle import helper as helper_mod

        has_tun = any(i.get("type") == "tun" for i in config.get("inbounds", []))
        want_helper = has_tun and not self.local_only and helper_mod.reachable()
        if want_helper:
            return self._apply_via_helper(helper_mod)
        # Turning tun off while the helper owns a root sing-box: stop it via
        # the helper (the user can't signal it), then proceed locally.
        if self._ownership()[0] == "helper":
            helper_mod.request("stop")
        if self.is_running():
            if self.reload() and self._verify_healthy():  # noqa: S1066
                return ApplyResult(ApplyOutcome.APPLIED)  # reloaded in place
            # SIGHUP undeliverable, or the process/control API did not come
            # back after the reload — fall back to a full restart
        try:
            self.restart()
        except SingBoxRuntimeError as e:
            return ApplyResult(ApplyOutcome.RUNTIME_FAILED, str(e))
        if not self._verify_healthy():
            return ApplyResult(
                ApplyOutcome.RUNTIME_FAILED,
                "sing-box started but its control API did not become reachable",
            )
        return ApplyResult(ApplyOutcome.APPLIED)

    def _apply_via_helper(self, helper_mod) -> ApplyResult:
        """Apply the already-written, already-checked config through the
        privileged helper (tun mode on).

        The helper is the only thing that can run sing-box as root, so a local
        user sing-box — if one is still up from before tun was enabled — must
        be stopped first (two sing-boxes would fight over the same ports).
        Then reload if the helper already owns a live process, else start.
        """
        # Clear any user-owned sing-box directly via the pidfile; the helper
        # path must not go through the helper-aware stop() (which would delegate
        # for a process the helper doesn't own).
        if proc.read_pidfile(_pid_path(), ("sing-box",)) is not None:
            self._stop_local()
        if self._ownership()[0] == "helper":
            helper_mod.request("reload")
            if self._verify_healthy():
                return ApplyResult(ApplyOutcome.APPLIED)
            helper_mod.request("stop")  # reload didn't take — cold start
        r = helper_mod.request("start")
        if not r.get("ok"):
            return ApplyResult(
                ApplyOutcome.RUNTIME_FAILED, r.get("error", "helper start failed")
            )
        if not self._verify_healthy():
            return ApplyResult(
                ApplyOutcome.RUNTIME_FAILED,
                "the helper started sing-box but its control API is unreachable",
            )
        return ApplyResult(ApplyOutcome.APPLIED)

    def _verify_healthy(self, deadline: float = 3.0) -> bool:
        """The process is alive *and* its control API answers.

        Polled briefly because a reload/start needs a moment to bring the Clash
        API listener back up; a dead process short-circuits immediately.
        """
        t0 = time.monotonic()
        while True:
            if not self.is_running():
                return False
            if self._control_alive():
                return True
            if time.monotonic() - t0 >= deadline:
                return False
            time.sleep(0.1)

    def _control_alive(self) -> bool:
        api = clash_api()
        req = urllib.request.Request(
            f"http://{api['address']}/version",  # noqa: S5332
            headers={"Authorization": f"Bearer {api['secret']}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=1):  # noqa: S310 (loopback)
                return True
        except (OSError, ValueError):
            return False

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
        # Identity is captured while the child is still ours and unreaped, so
        # the recorded start time provably belongs to this sing-box.
        proc.write_pidfile(_pid_path(), child.pid)
        _started_path().write_text(str(int(time.time())))
        # A config can pass `check` yet still die at startup (a port stolen by
        # another process binds only at run time). Catch an immediate exit so
        # the failure is loud instead of a dead PID behind a fresh pidfile.
        time.sleep(0.3)
        if child.poll() is not None:
            _pid_path().unlink(missing_ok=True)
            _started_path().unlink(missing_ok=True)
            raise SingBoxRuntimeError(
                f"sing-box exited immediately (code {child.returncode}); "
                f"last log lines:\n{self.logs(10)}"
            )

    def stop(self) -> None:
        # Only the helper can stop a root sing-box it owns; a user SIGTERM
        # would get EPERM. Delegate when it owns the process.
        from alle import helper as helper_mod

        owner, _pid = self._ownership()
        if owner == "helper":
            helper_mod.request("stop")
            return
        self._stop_local()

    def _stop_local(self) -> None:
        """Stop a user-owned sing-box by pidfile (no helper involvement)."""
        pid = proc.read_pidfile(_pid_path(), ("sing-box",))
        if pid is None:
            _pid_path().unlink(missing_ok=True)
            _started_path().unlink(missing_ok=True)
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:  # exited between the liveness check and the signal
            pass
        for _ in range(40):
            if proc.read_pidfile(_pid_path(), ("sing-box",)) is None:
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
            # kept for apply() to attach to the REJECTED outcome — "reported"
            # must include *why* sing-box refused the generation
            self._check_error = result.stderr.strip() or "sing-box rejected the config"
            print(
                f"alle: sing-box rejected the config:\n{result.stderr.strip()}",
                file=sys.stderr,
            )
            return False
        self._check_error = ""
        return True

    def reload(self) -> bool:
        """Tell a running sing-box to reload its config in place (SIGHUP).

        Delegates to the helper when it owns the process (a user SIGHUP would
        get EPERM on a root sing-box)."""
        from alle import helper as helper_mod

        owner, pid = self._ownership()
        if owner == "helper":
            return bool(helper_mod.request("reload").get("reloaded", False))
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
    def connections(self) -> list[dict] | None:
        """Raw active connections from the Clash API.

        Each entry carries a stable ``id`` plus cumulative ``upload``/``download``
        byte counters for that connection's lifetime and a ``chains`` list naming
        the outbound(s) it exits through. This is the raw feed the metrics
        accumulator turns into durable per-channel totals.

        Returns ``None`` — never ``[]`` — when the Clash API is unreachable
        (sing-box still starting) or the payload is malformed: "couldn't
        sample" and "no live connections" must stay distinguishable, or a
        failed sample would clear the accumulator's watermarks and the next
        good one would re-bank whole lifetime counters.
        """
        api = clash_api()
        req = urllib.request.Request(
            f"http://{api['address']}/connections",  # noqa: S5332
            headers={"Authorization": f"Bearer {api['secret']}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as r:  # noqa: S310 (loopback)
                data = json.load(r)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        conns = data.get("connections")
        if conns is None:
            return []  # API healthy, zero connections
        if not isinstance(conns, list):
            return None
        return [c for c in conns if isinstance(c, dict)]

    def generation(self) -> str | None:
        """Identity of the running sing-box instance (verified pid + kernel
        start time), or ``None`` if it isn't provably running.

        The metrics accumulator keys its counter watermarks on this: Clash
        connection counters reset with the process, so a sample from a new
        generation must re-baseline instead of being read as counter deltas.
        """
        owner, _pid = self._ownership()
        if owner == "helper":
            from alle import helper as helper_mod

            st = helper_mod.request("status")
            if st.get("ok") and st.get("running"):
                gen = st.get("generation")
                if gen:
                    return gen
        try:
            text = _pid_path().read_text()
        except OSError:
            return None
        rec = proc.parse_record(text)
        if rec is None or not proc.verify(rec, ("sing-box",)):
            return None
        return f"{rec['pid']}/{rec.get('start') or ''}"
