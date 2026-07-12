"""The privileged TUN helper — a root daemon that owns sing-box while tun is on.

Why it exists
-------------
Creating the utun device and rewriting the system route table needs root
(macOS has no ``setcap`` equivalent). The v1 stopgap is ``sudo … alle tun on``
every time; this helper is the steady state: installed **once** by
``sudo alle helper install`` as a system LaunchDaemon, it runs as root for the
machine's life, and the user-level alle delegates sing-box's lifecycle to it
whenever tun mode is active. After install, no password is ever asked again.

Why it is this small
--------------------
The helper is deliberately dumb — a hard-scope privilege holder, not a second
alle. It does **not** parse ``state.json``, run :mod:`alle.engine`, or touch
credentials. The user daemon generates the config (as it always has) and writes
it to the fixed path ``$ALLE_HOME/singbox.json``; the helper merely ``exec``\\ s
the pinned sing-box binary against that file and supervises it. The only thing
that must be root is the sing-box process holding the tun — everything
config-shaped stays in user space. See ``docs/security.md``.

Trust + auth
------------
The helper listens on a unix socket (root-owned dir, so no pre-create race) and
authenticates each connection by the peer's effective uid against the single
installing user recorded at install time (``ALLE_HELPER_ALLOWED_UID``). No
shared secret: the kernel vouches for the peer. A non-installing user is
refused before any command runs. The protocol carries **no paths from the
client** — the binary, config, and log paths are all fixed at install time, so
the helper can never be talked into ``exec``\\ ing an arbitrary binary as root.

Linux does not use this helper: ``setcap cap_net_admin`` on the pinned binary
is the one-time-install equivalent there (no root process at all).
"""

from __future__ import annotations

import json
import os
import socket
import struct
from typing import NoReturn

from alle import applog, singbox

# /var/run is root-owned (not world-writable like /tmp), so a non-root process
# cannot pre-create the socket path and spoof the helper before it binds.
HELPER_SOCKET_DEFAULT = "/var/run/alle.helper.sock"
HELPER_LABEL = "com.github.zydo.alle.helper"
# Bumped when the protocol/behaviour changes; the client can refuse to delegate
# to an older helper it cannot trust.
PROTOCOL_VERSION = 1

# macOS LOCAL_PEERCRED: retrieve the connected peer's effective uid. SOL_LOCAL
# is 0 and LOCAL_PEERCRED is 0x001 on Darwin; the returned xucred begins
# {u32 cr_version; uid_t cr_uid; ...}, so uid is the second 32-bit word.
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_XUCRED_SIZE = 76  # version(4) + uid(4) + ngroups(4) + groups[16](64)


def _peer_uid(conn: socket.socket) -> int | None:
    """The effective uid of the peer on a connected AF_UNIX socket, or None."""
    try:
        data = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
    except OSError:
        return None
    try:
        _version, uid = struct.unpack_from("II", data, 0)
    except struct.error:
        return None
    return int(uid)


def _ok(**kw) -> dict:
    kw["ok"] = True
    return kw


def _err(msg: str, **kw) -> dict:
    kw["ok"] = False
    kw["error"] = msg
    return kw


def _runner() -> singbox.Runner:
    # local_only: the helper IS the privileged actor, so its Runner must manage
    # sing-box directly via the pidfile — never by calling helper.request on
    # itself (that would recurse: Runner.running_pid → _helper_owned_pid →
    # helper.request → the helper's own Runner …).
    return singbox.Runner(local_only=True)


def _handle(req: dict, allowed_uid: int) -> dict:
    """One request → one response. Authorized by the caller (peer uid checked
    in the accept loop before this runs)."""
    cmd = req.get("cmd")
    runner = _runner()
    if cmd == "ping":
        return _ok(version=PROTOCOL_VERSION, allowed_uid=allowed_uid)
    if cmd == "status":
        pid = runner.running_pid()
        if pid is None:
            return _ok(running=False)
        return _ok(running=True, pid=pid, generation=runner.generation())
    if cmd == "start":
        try:
            runner.start()
        except singbox.SingBoxError as e:
            return _err(str(e))
        pid = runner.running_pid()
        return _ok(pid=pid, generation=runner.generation())
    if cmd == "stop":
        runner.stop()
        return _ok()
    if cmd == "reload":
        return _ok(reloaded=runner.reload())
    return _err(f"unknown command {cmd!r}")


class HelperConfigError(RuntimeError):
    """The helper's plist-provided environment is unusable (missing/bad uid).

    A configuration error worth a hard exit rather than a silent open socket;
    launchd's KeepAlive will retry, and the log carries the reason.
    """


def _allowed_uid_from_env() -> int:
    allowed = os.environ.get("ALLE_HELPER_ALLOWED_UID")
    if not allowed:
        raise HelperConfigError("ALLE_HELPER_ALLOWED_UID is unset — refusing to start")
    try:
        return int(allowed)
    except ValueError:
        raise HelperConfigError(f"bad ALLE_HELPER_ALLOWED_UID {allowed!r}") from None


def run_daemon() -> int:
    """The LaunchDaemon entry point. Binds the socket and serves forever.

    Reads its configuration from environment variables set by the install
    plist (``ALLE_HELPER_SOCKET``, ``ALLE_HELPER_ALLOWED_UID``, and the usual
    ``ALLE_HOME`` so the alle path machinery points at the installing user's
    state). Returns an exit code only on a fatal configuration error — the
    healthy path never returns (the serve loop runs for the daemon's life).
    """
    try:
        allowed_uid = _allowed_uid_from_env()
    except HelperConfigError as e:
        applog.log(f"alle-helper: {e}")
        return 2
    socket_path = os.environ.get("ALLE_HELPER_SOCKET", HELPER_SOCKET_DEFAULT)
    _serve_forever(socket_path, allowed_uid)


def _serve_forever(socket_path: str, allowed_uid: int) -> NoReturn:
    """Bind the socket and handle requests until the process is killed."""
    # Clean any stale socket (a previous helper that crashed mid-bind). Root
    # owns /var/run so this unlink cannot hit a file we did not create.
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(socket_path)
    # 0660 owned by the installing user: only that user can connect; the
    # peer-uid check is the real auth, this just keeps other locals off the
    # socket so they get EACCES instead of reaching the request loop.
    os.chmod(socket_path, 0o660)
    os.chown(socket_path, allowed_uid, os.getgid())
    srv.listen(8)
    applog.log(f"alle-helper: listening on {socket_path} (serving uid {allowed_uid})")

    while True:
        try:
            conn, _ = srv.accept()
        except OSError as e:
            applog.log(f"alle-helper: accept failed: {e}")
            continue
        try:
            conn.settimeout(10.0)  # a stuck/abusive client can't wedge the loop
            uid = _peer_uid(conn)
            if uid is None:
                _send(conn, _err("could not determine peer uid"))
                continue
            if uid != 0 and uid != allowed_uid:
                # uid 0 (root) is always allowed so alle run under sudo, and a
                # future root LaunchDaemon, can still drive the helper.
                _send(conn, _err("not authorized"))
                continue
            req = _recv(conn)
            if req is None:
                continue
            _send(conn, _handle(req, allowed_uid))
        except Exception as e:  # noqa: BLE001 — a bad request must not kill the daemon
            try:
                _send(conn, _err(f"helper error: {e}"))
            except OSError:
                pass
            applog.log(f"alle-helper: request error: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _send(conn: socket.socket, obj: dict) -> None:
    conn.sendall((json.dumps(obj) + "\n").encode())


def _recv(conn: socket.socket) -> dict | None:
    """One newline-terminated JSON request. None on EOF/empty/malformed."""
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = conn.recv(65536)
        except OSError:
            return None
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:  # a command is one small object; cap abuse
            return None
    if not buf:
        return None
    try:
        obj = json.loads(buf.decode().strip())
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# ---- client (user side) ------------------------------------------------------


def request(cmd: str, **fields) -> dict:
    """Send one command to the helper and return its response dict.

    ``{"ok": False, "error": ...}`` when the helper is unreachable (not
    installed, not running, or the socket absent) — callers treat that as
    "no helper; fall back to the local path." Never raises on a missing
    helper: the delegation gate depends on a clean downgrade.
    """
    path = os.environ.get("ALLE_HELPER_SOCKET", HELPER_SOCKET_DEFAULT)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5.0)
            s.connect(path)
            s.sendall((json.dumps({"cmd": cmd, **fields}) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return {"ok": False, "error": "helper closed the connection"}
            return json.loads(buf.decode().strip())
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"helper unreachable: {e}"}


def ping() -> dict:
    """Is the helper alive and which uid does it serve? Cheaper than status."""
    return request("ping")


def reachable() -> bool:
    """True iff a helper is installed, running, and answers ping."""
    return bool(ping().get("ok"))
