"""alle command-line interface.

The data model is provider-centric: **each provider owns a list of channels**.
You add a provider once (``alle providers add <name>``), then add channels
under it (``alle channels add <name> --country …``). All of it lives in one
``~/.alle/state.json``; provider tokens live in ``credentials.yaml``.

The CLI only adapts terminal input/output to the shared application layer. A
detached applier daemon watches the state file, makes the single sing-box
process match it, and heartbeat-probes every channel — so adding or removing a
channel is enough; there is no separate "apply" step. WireGuard is
connectionless, so there is no enable/disable: a channel is "active" only if its
latest probe succeeded.

Read commands accept ``--json`` for **shell / cross-language scripting** (jq,
monitoring hooks, CI): it is a direct serialization of the ``alle.service`` return
value, not a scrape of the human text. It is deliberately *not* the programmatic
interface for alle's own components — the Web UI, desktop companion, and any typed
client use ``alle.service`` (and later the ``alled`` control API) directly rather
than shelling out to the CLI and parsing its output.
"""

from __future__ import annotations

import argparse
import getpass
import itertools
import sys
import threading
import time

from alle import __version__, applog, daemon, output, service
from alle.providers import (
    ProviderError,
    auth_fields,
    auth_help,
    display_name,
    kind,
    known,
    match,
)
from alle.state import Store


# ---- secret entry ----------------------------------------------------------


def _read_secret_chars(read_char, echo) -> str:
    """Assemble a secret from single characters, masking echo and handling paste."""
    buf: list[str] = []
    while True:
        ch = read_char()
        if ch in ("\r", "\n", ""):  # Enter or EOF ends input
            break
        if ch == "\x03":  # Ctrl-C
            raise KeyboardInterrupt
        if ch in ("\x7f", "\x08"):  # backspace
            if buf:
                buf.pop()
                echo("\b \b")
            continue
        if ch == "\x1b":  # escape sequence (bracketed paste, arrow keys, ...)
            nxt = read_char()
            if nxt == "[":  # CSI: consume through its final byte
                while True:
                    c = read_char()
                    if c == "" or c.isalpha() or c == "~":
                        break
            elif nxt == "O":  # SS3 (F1–F4, arrows in application mode): one final byte
                read_char()
            continue
        buf.append(ch)
        echo("*")
    return "".join(buf)


def _read_secret(prompt: str) -> str:
    """Read a secret from the terminal, echoing one ``*`` per character.

    Gives visible feedback (unlike getpass) and reads in cbreak mode so a paste
    arrives intact. Falls back to getpass when stdin isn't a real TTY.
    """
    try:
        import termios
        import tty
    except ImportError:  # non-Unix
        return getpass.getpass(prompt)
    if not sys.stdin.isatty():
        return getpass.getpass(prompt)

    def echo(s):
        sys.stdout.write(s)
        sys.stdout.flush()

    sys.stdout.write(prompt)
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return _read_secret_chars(lambda: sys.stdin.read(1), echo)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()


# ---- helpers ---------------------------------------------------------------


def _resolve_provider(name: str) -> str:
    return service.resolve_provider(name)


def _print_or_json(data: dict, render, json_: bool) -> None:
    print(output.json_text(data) if json_ else render(data))


# ---- providers -------------------------------------------------------------


def cmd_providers_add(args):
    provider = _resolve_provider(args.provider)

    if kind(provider) == "config":
        result = service.provider_add_config(provider)
        print(f"Added provider {result['display_name']}.")
        help_ = result["config_help"]
        if help_:
            print(f"  {help_}")
        return

    # token provider — prompt, (validate if functional), store credential
    help_text, url = auth_help(provider)
    print(f"Add provider {display_name(provider)}.")
    if help_text:
        print(f"  How to obtain: {help_text}")
    if url:
        print(f"  {url}")
    creds = {}
    for field in auth_fields(provider):
        prompt = f"  {field.label}: "
        value = (_read_secret(prompt) if field.secret else input(prompt)).strip()
        if not value:
            sys.exit(f"{field.label} is required.")
        creds[field.key] = value

    try:
        result = service.provider_add_token(provider, creds)
    except ProviderError as e:
        sys.exit(f"credential rejected: {e}")

    if not result["functional"]:
        print(
            f"  Note: adding channels under {display_name(provider)} isn't implemented "
            "yet — credential stored for when it is."
        )
    print(
        f"Added provider {result['display_name']} (credential {result['credential_preview']})."
    )


def cmd_providers_ls(args):
    _print_or_json(service.provider_list(), output.providers_list, args.json)


def cmd_providers_rm(args):
    provider = _resolve_provider(args.provider)
    store = Store.load()
    if not store.has_provider(provider):
        sys.exit(f"{display_name(provider)} is not added.")
    n = len(store.provider_channels(provider))
    if not args.yes:
        print(
            f"WARNING: removing {display_name(provider)} will delete its "
            f"{n} channel(s) AND its stored credential. You will need to re-add the "
            "provider (and re-enter the token) to use it again."
        )
        # Accept the key ("protonvpn") or the display name just shown ("Proton VPN").
        if match(input("Type the provider name to confirm: ")) != provider:
            sys.exit("Aborted.")
    result = service.provider_remove(provider)
    print(
        f"Removed {result['display_name']} and its {result['channels_removed']} channel(s)."
    )


# ---- channels --------------------------------------------------------------


def cmd_channels_add(args):
    provider = _resolve_provider(args.provider)
    result = service.channel_add(provider, args.country, args.city, args.config)
    channel = result["channel"]
    if result.get("imported_from"):
        verb = "Updated" if result.get("updated") else "Imported"
        source = f" from {result['imported_from']}"
    else:
        verb, source = "Added", ""
    print(
        f"{verb} channel {channel.id} under {result['display_name']}{source} "
        f"on 127.0.0.1:{channel.port}."
    )
    print("Applying… (see: alle status)")


def cmd_channels_ls(args):
    """List configured channels grouped by provider — static config only, no
    connection status and independent of whether alle is up or down."""
    _print_or_json(service.channel_list(), output.channels_list, args.json)


def cmd_channels_rm(args):
    provider = _resolve_provider(args.provider)
    result = service.channel_remove(provider, args.channel)
    print(f"Removed channel {result['channel']} from {result['display_name']}.")


# ---- locations -------------------------------------------------------------


def cmd_locations(args):
    provider = _resolve_provider(args.provider)
    _print_or_json(
        service.locations_list(provider, args.country, args.refresh),
        output.locations,
        args.json,
    )


# ---- status / test ---------------------------------------------------------


def cmd_status(args):
    _print_or_json(service.status_snapshot(), output.status, args.json)


def cmd_test(args):
    spinner = _Spinner("testing…") if args.speed and not args.json else None

    def progress(row, phase):
        if spinner is not None:
            spinner.label = f"{row['provider']}/{row['name']}  {phase}…"

    if spinner is not None:
        with spinner:
            result = service.test(
                speed=args.speed, channel=args.channel, progress=progress
            )
    else:
        result = service.test(speed=args.speed, channel=args.channel)

    if args.json:
        print(output.json_text(result))
        return
    print(output.test_result(result))


def cmd_metrics(args):
    _print_or_json(service.metrics_snapshot(args.channel), output.metrics, args.json)


class _Spinner:
    """A tiny stderr spinner for long, one-at-a-time work (the speed test).

    Animates only on a real TTY; when stderr is redirected (pipe, CI) it stays
    silent so captured output isn't polluted. ``label`` may be updated live to
    reflect the current phase.
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str):
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if sys.stderr.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def _spin(self):
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.is_set():
                break
            sys.stderr.write(f"\r  {frame} {self.label}\033[K")
            sys.stderr.flush()
            time.sleep(0.1)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()
        if sys.stderr.isatty():
            sys.stderr.write("\r\033[K")  # wipe the spinner line
            sys.stderr.flush()


# ---- start / stop / restart ------------------------------------------------


def cmd_start(args):
    result = service.start()
    if result["has_channels"]:
        print("Alle started; channels are being applied and probed. See: alle status")
    else:
        print(
            "Alle started (sing-box running idle — no channels yet). "
            "Add one: alle channels add <provider> --country …"
        )


def cmd_stop(args):
    result = service.stop()
    if result["was_running"]:
        print("Alle stopped (channels kept in config).")
    else:
        print("Alle is already stopped.")


def cmd_restart(args):
    service.restart()
    print("Alle restarted. See: alle status")


# ---- logs ------------------------------------------------------------------


def cmd_logs(args):
    if args.follow:
        applog.follow(args.lines)
    else:
        print(service.logs_tail(args.lines))


# ---- version ---------------------------------------------------------------


def cmd_version(args):
    print(__version__)


# ---- applier (hidden) ------------------------------------------------------


def cmd_applier(args):
    daemon.run_applier()


# ---- parser ----------------------------------------------------------------


class _HelpOnErrorParser(argparse.ArgumentParser):
    """ArgumentParser that prints help instead of a terse error when arguments
    are missing or invalid. ``add_subparsers``/``add_parser`` propagate this class
    to every level, so e.g. ``alle providers add`` (missing ``provider``)
    shows the same help as ``alle providers add -h``."""

    def error(self, message):
        self.print_help()
        self.exit(2)


def _show_help(parser: argparse.ArgumentParser):
    """A `func` that prints a parser's help — used when a (sub)command is given
    with no action, so the user sees usage instead of an argparse error."""

    def run(_args):
        parser.print_help()

    return run


def _provider_help() -> str:
    """Enumerate the supported providers for argument help."""
    return "supported provider; one of: " + ", ".join(known())


def build_parser() -> argparse.ArgumentParser:
    p = _HelpOnErrorParser(prog="alle", description=__doc__)
    p.set_defaults(func=_show_help(p))
    sub = p.add_subparsers(dest="command")

    # providers
    pr = sub.add_parser("providers", help="manage VPN providers")
    pr.set_defaults(func=_show_help(pr))
    pr_sub = pr.add_subparsers(dest="providers_command")
    pa = pr_sub.add_parser("add", help="add a provider (prompts for a token if needed)")
    pa.add_argument("provider", help=_provider_help())
    pa.set_defaults(func=cmd_providers_add)
    pls = pr_sub.add_parser("ls", help="list the providers you've added")
    pls.add_argument("--json", action="store_true", help="print machine-readable JSON")
    pls.set_defaults(func=cmd_providers_ls)
    pd = pr_sub.add_parser(
        "rm", help="remove a provider AND all its channels + credential"
    )
    pd.add_argument("provider", help=_provider_help())
    pd.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    pd.set_defaults(func=cmd_providers_rm)

    # channels
    ch = sub.add_parser("channels", help="manage channels under a provider")
    ch.set_defaults(func=_show_help(ch))
    ch_sub = ch.add_subparsers(dest="channels_command")
    ca = ch_sub.add_parser("add", help="add a channel under a provider")
    ca.add_argument("provider", help=_provider_help())
    ca.add_argument("--country", help="country — API providers only (e.g. nordvpn)")
    ca.add_argument("--city", help="city — API providers only (omit = any city)")
    ca.add_argument(
        "--config",
        help="path to a WireGuard .conf — config providers only (e.g. protonvpn); "
        "mutually exclusive with --country/--city",
    )
    ca.set_defaults(func=cmd_channels_add)
    cls = ch_sub.add_parser(
        "ls", help="list configured channels by provider (no status)"
    )
    cls.add_argument("--json", action="store_true", help="print machine-readable JSON")
    cls.set_defaults(func=cmd_channels_ls)
    cr = ch_sub.add_parser("rm", help="remove a channel from a provider")
    cr.add_argument("provider", help=_provider_help())
    cr.add_argument("--channel", required=True, help="channel name to remove")
    cr.set_defaults(func=cmd_channels_rm)

    # locations
    lo = sub.add_parser(
        "locations", help="list a provider's available countries/cities"
    )
    lo.add_argument("provider", help=_provider_help())
    lo.add_argument("--country", help="show cities for this country")
    lo.add_argument(
        "--refresh", action="store_true", help="force-refresh the location list"
    )
    lo.add_argument("--json", action="store_true", help="print machine-readable JSON")
    lo.set_defaults(func=cmd_locations)

    # top-level verbs
    st = sub.add_parser("status", help="show alle + per-channel status")
    st.add_argument("--json", action="store_true", help="print machine-readable JSON")
    st.set_defaults(func=cmd_status)
    sub.add_parser(
        "start", help="start sing-box (idle if no channels) + apply + probe"
    ).set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop sing-box (channels kept in config)").set_defaults(
        func=cmd_stop
    )
    sub.add_parser(
        "restart", help="stop then start (reload after upgrades/config)"
    ).set_defaults(func=cmd_restart)
    te = sub.add_parser(
        "test", help="probe channels now; optionally speed-test healthy ones"
    )
    te.add_argument(
        "--speed", action="store_true", help="also run download/upload tests"
    )
    te.add_argument("--channel", help="test only this channel (default: every channel)")
    te.add_argument("--json", action="store_true", help="print machine-readable JSON")
    te.set_defaults(func=cmd_test)
    me = sub.add_parser("metrics", help="show per-channel cumulative traffic totals")
    me.add_argument(
        "channel", nargs="?", help="filter to one channel by name (default: all)"
    )
    me.add_argument("--json", action="store_true", help="print machine-readable JSON")
    me.set_defaults(func=cmd_metrics)
    lg = sub.add_parser("logs", help="show alle's operation log")
    lg.add_argument("-f", "--follow", action="store_true", help="stream new log lines")
    lg.add_argument(
        "-n", "--lines", type=int, default=200, help="lines to show (default 200)"
    )
    lg.set_defaults(func=cmd_logs)
    sub.add_parser("version", help="print alle's version").set_defaults(
        func=cmd_version
    )

    sub.add_parser("applier").set_defaults(
        func=cmd_applier
    )  # internal: the daemon body

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except service.ServiceError as e:
        sys.exit(str(e))
    except (ProviderError, RuntimeError) as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
