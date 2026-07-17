"""alle command-line interface.

The data model is provider-centric: **each provider owns a list of channels**.
You add a provider once (``alle providers add <name>``), then add channels
under it (``alle channels add <name> --country …``). All of it lives in one
``~/.alle/state.json``; provider tokens live in ``credentials.yaml``.

The CLI only adapts terminal input/output to the shared application layer. A
detached applier daemon watches the state file, makes the single sing-box
process match it, and heartbeat-probes every enabled channel — so adding or
removing a channel is enough; there is no separate "apply" step. Two distinct
per-channel facts: ``channels enable``/``disable`` set *administrative intent*
(a disabled channel is not materialised at all — no WireGuard endpoint, no
keepalive, no probe — so it occupies no provider connection slot), while
"active" is *liveness*: an enabled channel is active only if its latest probe
succeeded.

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
import os
import sys
import threading
import time
from pathlib import Path

from alle import __version__, applog, credentials, daemon, output, routes, service
from alle.providers import (
    ProviderError,
    auth_fields,
    auth_help,
    display_name,
    kind,
    known,
    match,
    preview,
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
        if getattr(args, "token", None):
            sys.exit(
                f"{display_name(provider)} imports channels from a .conf; "
                "it has no token to set."
            )
        result = service.provider_add_config(provider)
        print(f"Added provider {result['display_name']}.")
        help_ = result["config_help"]
        if help_:
            print(f"  {help_}")
        return

    # A token provider that's already added: this is a credential *replacement*
    # (idempotent add), gated behind confirmation so it can't silently swap a
    # working token for a bad one.
    replacing = Store.load().has_provider(provider)
    if replacing and not _confirm_token_replace(provider, args):
        sys.exit("Aborted.")

    if not replacing:
        help_text, url = auth_help(provider)
        print(f"Add provider {display_name(provider)}.")
        if help_text:
            print(f"  How to obtain: {help_text}")
        if url:
            print(f"  {url}")

    creds = _collect_token_creds(provider, args)

    try:
        result = service.provider_add_or_update_token(provider, creds)
    except ProviderError as e:
        # Nothing was written (validation runs before the store), so on a replace
        # the previous credential is intact.
        sys.exit(f"credential rejected: {e}")

    if result.get("unchanged"):
        # Re-entered the same token — nothing changed, so don't claim a re-resolve.
        print(
            f"{result['display_name']} already has that token "
            f"({result['credential_preview']}) — nothing to do."
        )
        return

    if result["updated"]:
        print(
            f"Updated {result['display_name']} credential "
            f"(token {result['credential_preview']})."
        )
        for line in output.provider_token_refresh(result["channels"]):
            print(line)
        return

    if not result["functional"]:
        print(
            f"  Note: adding channels under {display_name(provider)} isn't implemented "
            "yet — credential stored for when it is."
        )
    print(
        f"Added provider {result['display_name']} (credential {result['credential_preview']})."
    )


def _confirm_token_replace(provider: str, args) -> bool:
    """Confirm replacing an already-added token provider's credential. ``--yes``
    (or ``--token`` in a script) skips the prompt; off a TTY without either we
    refuse rather than silently overwrite a working token."""
    creds = credentials.get(provider) or {}
    current = preview(provider, creds) or "none"
    print(f"{display_name(provider)} is already added (token {current}).")
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        sys.exit("Refusing to replace the token without --yes (not a terminal).")
    return input("Replace its token? [y/N] ").strip().lower() in ("y", "yes")


def _collect_token_creds(provider: str, args) -> dict:
    """Gather a token provider's credential fields, from ``--token`` (single-secret
    providers, for scripts) or interactive prompts."""
    fields = auth_fields(provider)
    token_flag = getattr(args, "token", None)
    if token_flag:
        secret_fields = [f for f in fields if f.secret]
        if len(secret_fields) != 1 or len(fields) != 1:
            sys.exit(
                f"--token can't be used with {display_name(provider)} "
                "(it needs more than one field); run without --token to be prompted."
            )
        return {fields[0].key: token_flag.strip()}
    creds = {}
    for field in fields:
        prompt = f"  {field.label}: "
        value = (_read_secret(prompt) if field.secret else input(prompt)).strip()
        if not value:
            sys.exit(f"{field.label} is required.")
        creds[field.key] = value
    return creds


def cmd_providers_ls(args):
    _print_or_json(service.provider_list(), output.providers_list, args.json)


def _print_provider_removals(result: dict) -> None:
    dry_run = result.get("dry_run", False)
    verb = "Would remove" if dry_run else "Removed"
    providers = result["providers"]
    for item in providers:
        print(
            f"{verb} {item['display_name']} and its "
            f"{item['channels_removed']} channel(s)."
        )
    if len(providers) > 1:
        print(f"{verb} {len(providers)} providers.")


def cmd_providers_rm(args):
    if args.all and args.providers:
        raise service.ServiceError("--all cannot be combined with provider names.")

    providers = (
        Store.load().provider_names()
        if args.all
        else [_resolve_provider(provider) for provider in args.providers]
    )
    if args.all and not providers:
        print("No providers added.")
        return
    if args.dry_run:
        _print_provider_removals(service.provider_remove_many(providers, dry_run=True))
        return

    plan = service.provider_remove_many(providers, dry_run=True)["providers"]
    if not args.yes:
        total_channels = sum(item["channels_removed"] for item in plan)
        names = ", ".join(item["display_name"] for item in plan)
        suffix = "s" if len(plan) != 1 else ""
        print(
            f"WARNING: removing provider{suffix} {names} will delete "
            f"{total_channels} channel(s) AND stored credential(s). You will need "
            "to re-add provider(s) to use them again."
        )
        if len(plan) == 1:
            expected = plan[0]["provider"]
            # Accept the key ("protonvpn") or the display name just shown ("Proton VPN").
            if match(input("Type the provider name to confirm: ")) != expected:
                sys.exit("Aborted.")
        elif input("Type yes to confirm: ").strip().lower() != "yes":
            sys.exit("Aborted.")
    _print_provider_removals(service.provider_remove_many(providers))


# ---- channels --------------------------------------------------------------


def cmd_channels_add(args):
    provider = _resolve_provider(args.provider)
    result = service.channel_add(
        provider,
        args.country,
        args.city,
        args.config,
        args.label or "",
        port=args.port or 0,
    )
    channel = result["channel"]
    labelled = f' labelled "{channel.label}"' if channel.label else ""
    if result.get("unchanged"):
        # A byte-identical re-import of an existing .conf changes nothing — say so
        # rather than reporting a misleading "Updated".
        print(
            f"Channel {channel.id}{labelled} already exists under "
            f"{result['display_name']} with identical settings — nothing to do "
            f"(on 127.0.0.1:{channel.port})."
        )
        return
    if result.get("imported_from"):
        verb = "Updated" if result.get("updated") else "Imported"
        source = f" from {result['imported_from']}"
    else:
        verb, source = "Added", ""
    print(
        f"{verb} channel {channel.id}{labelled} under {result['display_name']}{source} "
        f"on 127.0.0.1:{channel.port}."
    )
    print("Applying… (see: alle status)")


def cmd_channels_setlabel(args):
    result = service.channel_set_label(args.channel, args.label)
    ref = f"{result['provider']}/{result['channel']}"
    if result["cleared"]:
        print(f"Cleared the label on {ref} (shows as {result['channel']} again).")
    else:
        print(f'Labelled {ref} as "{result["label"]}".')


def cmd_channels_ls(args):
    """List configured channels grouped by provider — static config only, no
    connection status and independent of whether alle is up or down."""
    if sum(bool(flag) for flag in (args.json, args.ids, args.refs)) > 1:
        raise service.ServiceError("--json, --ids, and --refs are mutually exclusive.")
    data = service.channel_list()
    if args.ids:
        print("\n".join(channel["name"] for channel in data["channels"]))
        return
    if args.refs:
        print(
            "\n".join(
                f"{channel['provider']}/{channel['name']}"
                for channel in data["channels"]
            )
        )
        return
    _print_or_json(data, output.channels_list, args.json)


def _legacy_channel_provider(args) -> str | None:
    if not args.channel:
        return None
    if args.provider:
        if args.refs:
            raise service.ServiceError(
                "--provider cannot be combined with a positional provider "
                "when --channel is used."
            )
        return _resolve_provider(args.provider)
    if len(args.refs) != 1:
        raise service.ServiceError(
            "legacy --channel form requires exactly one provider: "
            "alle channels rm <provider> --channel <name>"
        )
    return _resolve_provider(args.refs[0])


def _channel_refs_from_args(args) -> list[str]:
    if not args.channel:
        return args.refs
    return [channel for group in args.channel for channel in group]


def _print_channel_removals(result: dict) -> None:
    dry_run = result.get("dry_run", False)
    verb = "Would remove" if dry_run else "Removed"
    channels = result["channels"]
    for item in channels:
        print(f"{verb} channel {item['channel']} from {item['display_name']}.")
    if len(channels) > 1:
        print(f"{verb} {len(channels)} channels.")


def cmd_channels_rm(args):
    provider = _legacy_channel_provider(args)
    if provider is None and args.provider:
        provider = _resolve_provider(args.provider)
    result = service.channel_remove_many(
        _channel_refs_from_args(args),
        provider=provider,
        dry_run=args.dry_run,
        all_=args.all,
    )
    _print_channel_removals(result)


def cmd_channels_set_enabled(args):
    enabled = args.channels_command == "enable"
    verb = "enable" if enabled else "disable"
    result = service.channel_set_enabled_many(
        args.refs,
        enabled,
        provider=_resolve_provider(args.provider) if args.provider else None,
        dry_run=args.dry_run,
        all_=args.all,
    )
    if result["dry_run"]:
        for item in result["channels"]:
            if item["changed"]:
                print(f"Would {verb} channel {item['ref']}.")
            else:
                print(f"Channel {item['ref']} is already {verb}d — nothing to do.")
        return
    for ref in result["wg_resolved"]:
        print(f"Resolved a server for {ref} (it had no WireGuard config yet).")
    for ref in result["changed"]:
        print(f"{verb.capitalize()}d channel {ref}.")
    for ref in result["already"]:
        print(f"Channel {ref} is already {verb}d — nothing to do.")
    if result["changed"]:
        print("Applying… (see: alle status)")


# ---- routes ------------------------------------------------------------------


def _rule_entries(args) -> list[dict]:
    out = []
    for value in args.domain or []:
        out.append({"type": None, "value": value})
    for value in args.cidr or []:
        out.append({"type": "ip_cidr", "value": value})
    if args.all:
        out.append({"type": "all", "value": ""})
    return out


def _print_ruleset_added(result: dict, verb: str = "Added") -> None:
    rs = result["ruleset"]
    print(
        f"{verb} ruleset {rs['id']} {rs['name']!r}: {rs['matcher_count']} matcher(s) → {rs['target']}."
    )
    for rule in rs["rules"]:
        if rule.get("shadowed_by"):
            print(
                f"  WARNING: {rule['id']} {rule['match']} is shadowed by "
                f"{routes.shadow_label(rule['shadowed_by'])} — it will never match."
            )


def cmd_routes_ruleset_create(args):
    _print_ruleset_added(
        service.routes_ruleset_create(args.name, args.target, _rule_entries(args))
    )


def cmd_routes_ruleset_add(args):
    _print_ruleset_added(
        service.routes_ruleset_add(args.ruleset, _rule_entries(args)), verb="Updated"
    )


def cmd_routes_ruleset_rm(args):
    result = service.routes_ruleset_remove(args.ruleset, dry_run=args.dry_run)
    rs = result["ruleset"]
    verb = "Would remove" if result["dry_run"] else "Removed"
    print(
        f"{verb} ruleset {rs['id']} {rs['name']!r}: {rs['matcher_count']} matcher(s)."
    )


def cmd_routes_ruleset_rename(args):
    rs = service.routes_ruleset_rename(args.ruleset, args.name)["ruleset"]
    print(f"Renamed ruleset {rs['id']} to {rs['name']!r}.")


def cmd_routes_ruleset_retarget(args):
    rs = service.routes_ruleset_retarget(args.ruleset, args.target)["ruleset"]
    print(f"Retargeted ruleset {rs['id']} {rs['name']!r} → {rs['target']}.")


def cmd_routes_ls(args):
    _print_or_json(
        service.routes_list(args.channel, flat=args.flat), output.routes_list, args.json
    )


def cmd_routes_rm(args):
    result = service.routes_remove(args.ids, dry_run=args.dry_run)
    verb = "Would remove" if result["dry_run"] else "Removed"
    for rule in result["rules"]:
        print(
            f"{verb} matcher {rule['id']}: {rule['match']} from ruleset {rule.get('ruleset')}."
        )


def cmd_routes_reorder(args):
    result = service.routes_reorder(args.ids, flat=args.flat)
    if args.json:
        print(output.json_text(result))
        return
    items = result["rules"] if args.flat else result["rulesets"]
    noun = "rule" if args.flat else "ruleset"
    n = len(items)
    if result["changed"]:
        print(
            f"Reordered {n} {noun}{'s' if n != 1 else ''}. Rules are evaluated top to bottom."
        )
    else:
        print(f"Routes already in that order ({n} {noun}{'s' if n != 1 else ''}).")
    for rule in result.get("rules", []):
        if rule.get("shadowed_by"):
            print(
                f"  WARNING: {rule['id']} is shadowed by "
                f"{routes.shadow_label(rule['shadowed_by'])} — it will never match."
            )


def cmd_routes_lan(args):
    enable = {"on": True, "off": False}.get(args.state)
    result = service.routes_lan_direct(enable)
    router = result["router"]
    if router["lan_direct"]:
        print(
            "LAN direct ON — built-in rules send private/link-local/multicast\n"
            "destinations (printers, NAS, router admin, LAN discovery) direct,\n"
            "ahead of all user rules — no catch-all can capture them."
        )
    else:
        print(
            "LAN direct off — LAN/local destinations follow your rules\n"
            "(a catch-all VPN rule will capture them; most VPN clients keep\n"
            "this protection on). Re-enable:  alle routes lan on"
        )
    if args.verbose:
        print("  Built-in ranges: " + ", ".join(result["cidrs"]))


def cmd_routes_killswitch(args):
    enable = {"on": True, "off": False}.get(args.state)
    result = service.routes_killswitch(enable)
    router = result["router"]
    state = (
        "ON — unmatched router traffic is blocked"
        if router["killswitch"]
        else "off — unmatched router traffic goes direct (no VPN)"
    )
    scope = (
        # With tun on, the whole system enters the same rule table, so the
        # unmatched->block boundary is genuinely system-wide.
        "Applies system-wide (TUN mode is on); per-channel ports are unaffected."
        if router.get("tun")
        else "Applies to the router entrypoint only; per-channel ports are unaffected."
    )
    print(f"Kill-switch {state}.\n  {scope}")


def cmd_tun(args):
    if args.state == "confirm":
        service.tun_trial_confirm()
        print("TUN trial confirmed — TUN mode stays on.")
        return
    if args.trial is not None:
        if args.state != "on":
            raise service.ServiceError("--trial only applies to: alle tun on")
        result = service.tun_trial_arm(args.trial)
        print(
            f"TUN mode ON — trial window: {result['trial']['seconds']}s.\n"
            "  Keep it:  alle tun confirm\n"
            "  No confirmation -> TUN mode reverts off automatically\n"
            "  (the safety net for remote/SSH sessions and first bring-up)."
        )
        return
    enable = {"on": True, "off": False}.get(args.state)
    result = service.tun_mode(enable)
    router = result["router"]
    if enable is None and result.get("trial"):
        import time

        left = max(0, int(result["trial"].get("deadline", 0)) - int(time.time()))
        print(
            f"TUN trial pending — reverts off in ~{left}s unless confirmed:\n"
            "  alle tun confirm"
        )
    if router["tun"]:
        unmatched = (
            "unmatched traffic is BLOCKED (kill-switch is system-wide)"
            if router["killswitch"]
            else "unmatched traffic goes direct — no kill-switch"
        )
        print(
            "TUN mode ON — all system traffic enters the routing rules;\n"
            f"  {unmatched}.\n"
            "  Enforcement lives in the sing-box process: if it crashes, the\n"
            "  system falls back to the physical route (fails open) for the\n"
            "  ~2s supervision window."
        )
    else:
        print(
            "TUN mode off — only traffic pointed at the proxy ports is routed\n"
            "(per-channel ports and the router entrypoint keep working).\n"
            "Enable:  alle tun on  (needs root)"
        )


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
    # An interactive speed test streams: the header prints up front, each
    # channel's row appears the moment its test completes, a progress line tracks
    # the channel under test, and a summary prints at the end. Everything else
    # (--json, non-TTY, plain probe) uses the single-shot path below.
    if args.speed and not args.json and sys.stderr.isatty():
        _run_streaming_test(args)
        return

    result = service.test(speed=args.speed, channel=args.channel)

    if args.json:
        print(output.json_text(result))
        return
    print(output.test_result(result))


def _run_streaming_test(args):
    """Live, per-channel speed-test output (TTY only). See :func:`cmd_test`."""
    # 0-or-1-element holder: service.test() fills it via on_begin once the
    # channel list is known (empty = nothing to stream, single-shot output).
    stream: list[_SpeedStream] = []

    def on_begin(chans):
        if chans:  # no header when there are no channels — test() returns the reason
            stream.append(_SpeedStream(output.test_stream_widths(chans)))
            stream[0].begin()

    def on_row(row):
        if stream:
            stream[0].row(row)

    def progress(row, phase):
        if stream:
            stream[0].progress(row, phase)

    result = service.test(
        speed=True,
        channel=args.channel,
        progress=progress,
        on_row=on_row,
        on_begin=on_begin,
    )

    if not stream:
        print(output.test_result(result))  # "No channels configured." etc.
        return
    # No trailing summary line: plain `alle test` prints none either, and a
    # failing channel is already visible in its own row (STATE carries the
    # probe's failure reason; its skipped transfers render as "-").
    stream[0].end()


class _SpeedStream:
    """Live renderer for a streamed speed test.

    Prints the table header to stdout up front, then one row to stdout as each
    channel finishes, with a single reusable, *animated* progress line on stderr
    tracking the channel/phase under test. The progress line is wiped before
    every stdout row so the table stays clean.

    A background thread cycles the braille spinner frames so the indicator
    actually moves during the multi-second transfers; a lock serializes its
    writes against the main thread's row writes, since both share one terminal
    cursor (without it the spinner could redraw mid-row and garble the table).
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, widths):
        self.widths = widths
        self.label = "probing channels…"  # read by the spinner thread each frame
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _spin(self):
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.is_set():
                break
            with self._lock:
                if self._stop.is_set():
                    break
                sys.stderr.write(f"\r  {frame} {self.label}\033[K")
                sys.stderr.flush()
            time.sleep(0.1)

    def begin(self):
        sys.stdout.write(output.test_stream_header(self.widths) + "\n")
        sys.stdout.flush()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def progress(self, row, phase):
        # Reassigned atomically (GIL); the spinner re-reads it next frame, so no
        # lock is needed just to update the label.
        self.label = f"{row['provider']}/{row['name']}  {phase}…"

    def row(self, row):
        with self._lock:  # hold the spinner still while we wipe + print the row
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
            sys.stdout.write(output.test_stream_row(row, self.widths) + "\n")
            sys.stdout.flush()
        self.label = "next…"

    def end(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
        with self._lock:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


# ---- export / import / validate ---------------------------------------------


def _read_bundle_file(path: str) -> str:
    try:
        return Path(path).expanduser().read_text()
    except OSError as e:
        raise service.ServiceError(f"could not read {path}: {e}") from e


def _write_secret_file(path: Path, text: str) -> None:
    """Write 0600 from the first byte; chmod too, since overwriting an
    existing file would otherwise keep its old (possibly wider) mode."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _print_bundle_summary(result: dict) -> None:
    if result["mode"] == "import":
        ch = result["channels"]
        for ref in ch["created"]:
            print(f"  + channel {ref}")
        for ref in ch["updated"]:
            print(f"  ~ channel {ref} updated")
        for provider in result["credentials"]["added"]:
            print(f"  + credential for {provider}")
        for provider in result["credentials"]["replaced"]:
            print(f"  ~ credential for {provider} REPLACED (was different)")
        for name in result["rulesets_added"]:
            print(f"  + ruleset {name!r} (appended at the lowest priority)")
        unchanged = len(ch["unchanged"])
        if unchanged:
            print(f"  = {unchanged} channel(s) already up to date")
        if not any(
            (
                ch["created"],
                ch["updated"],
                result["credentials"]["added"],
                result["credentials"]["replaced"],
                result["rulesets_added"],
            )
        ):
            print("  Nothing to change — the setup already matches the bundle.")
        created = bool(ch["created"])
    else:
        print(
            f"  Setup replaced: {len(result['providers'])} provider(s), "
            f"{len(result['channels'])} channel(s), "
            f"{len(result['rulesets'])} ruleset(s)."
        )
        removed = result["removed"]
        if removed["providers"] or removed["channels"]:
            print(
                f"  Removed: {len(removed['providers'])} provider(s), "
                f"{len(removed['channels'])} channel(s)."
            )
        for provider in result["credentials"]:
            print(f"  Credential for {provider} replaced from the bundle.")
        created = bool(result["channels"])
    for ref in result.get("wg_resolved", []):
        print(f"  ~ {ref}: fresh server resolved via the provider token")
    for ref in result.get("wg_fallback", []):
        print(
            f"  ! {ref}: could not resolve a fresh server — restored the "
            "bundle's snapshot (auto-reconnect refreshes it when possible)"
        )
    for note in result.get("notes", []):
        print(f"  note: {note}")
    if created:
        print(
            "Note: unless a channel declares an explicit `port:`, ports are "
            "allocated locally (exports never carry them) — point apps at the "
            "ports shown by: alle status"
        )


def cmd_export(args):
    result = service.setup_export()
    out = args.out or f"alle-backup-{time.strftime('%Y%m%d-%H%M%S')}.yaml"
    what = (
        f"{result['providers']} provider(s), {result['channels']} channel(s), "
        f"{result['rulesets']} ruleset(s)"
    )
    if out == "-":
        print(result["text"], end="")
        return
    _write_secret_file(Path(out).expanduser(), result["text"])
    print(f"Exported {what} -> {out}")
    print(
        "This file contains WireGuard private keys and provider tokens — "
        "keep it private."
    )


def cmd_import(args):
    text = _read_bundle_file(args.file)
    if args.replace:
        plan = service.setup_restore_plan(text)
        cur, new = plan["current"], plan["bundle"]
        print(
            "Import --replace REPLACES the entire setup:\n"
            f"  current: {cur['providers']} provider(s), "
            f"{cur['channels']} channel(s), {cur['rulesets']} ruleset(s)\n"
            f"  bundle:  {new['providers']} provider(s), {new['channels']} channel(s), "
            f"{new['rulesets']} ruleset(s)"
        )
        if not args.yes:
            if not sys.stdin.isatty():
                raise service.ServiceError(
                    "--replace is destructive — pass --yes to confirm without a prompt."
                )
            answer = input("Type 'yes' to replace the current setup: ")
            if answer.strip().lower() != "yes":
                print("Aborted — nothing was changed.")
                return
        result = service.setup_restore(text)
        print(f"Replaced the setup with {args.file}:")
    else:
        result = service.setup_import(text)
        print(f"Imported {args.file} (merged into the current setup):")
    _print_bundle_summary(result)


def cmd_gateway_init(args):
    result = service.gateway_init()
    print(
        "Gateway data plane declared (fail-closed): TUN on, kill switch on.\n"
        "Readiness turns healthy only once the interface, sing-box control, "
        "and a viable channel all hold."
    )
    del result  # summary is in the printed contract; details ride the log


def cmd_sync(args):
    result = service.setup_sync(_read_bundle_file(args.file))
    print(f"Synced {args.file} (managed desired state):")
    ch, rs = result["channels"], result["rulesets"]
    for ref in ch["created"]:
        print(f"  + channel {ref}")
    for ref in ch["updated"]:
        print(f"  ~ channel {ref} updated")
    for ref in ch["pruned"]:
        print(f"  - channel {ref} pruned (no longer in the bundle)")
    for ref, names in ch["kept_referenced"].items():
        print(
            f"  ! channel {ref} is no longer in the bundle but was KEPT — "
            f"routing rule(s) still reference it: {', '.join(names)}"
        )
    for provider in result["providers_pruned"]:
        print(f"  - provider {provider} pruned (with its credential)")
    for provider in result["credentials"]["added"]:
        print(f"  + credential for {provider}")
    for provider in result["credentials"]["replaced"]:
        print(f"  ~ credential for {provider} REPLACED (was different)")
    for name in rs["added"]:
        print(f"  + ruleset {name!r} (appended at the lowest priority)")
    for name in rs["updated"]:
        print(f"  ~ ruleset {name!r} updated in place")
    for name in rs["pruned"]:
        print(f"  - ruleset {name!r} pruned (no longer in the bundle)")
    for ref in result["wg_resolved"]:
        print(f"  ~ {ref}: fresh server resolved via the provider token")
    for ref in result["wg_fallback"]:
        print(
            f"  ! {ref}: could not resolve a fresh server — kept the "
            "bundle's snapshot (auto-reconnect refreshes it when possible)"
        )
    for note in result.get("notes", []):
        print(f"  note: {note}")
    unchanged = len(ch["unchanged"])
    if unchanged:
        print(f"  = {unchanged} channel(s) already up to date")
    if not any(
        (
            ch["created"],
            ch["updated"],
            ch["pruned"],
            result["providers_pruned"],
            result["credentials"]["added"],
            result["credentials"]["replaced"],
            rs["added"],
            rs["updated"],
            rs["pruned"],
        )
    ):
        print("  Nothing to change — the managed setup already matches the bundle.")


def cmd_validate(args):
    result = service.setup_validate(_read_bundle_file(args.file))
    print(
        f"{args.file} is a valid bundle: {result['providers']} provider(s), "
        f"{result['channels']} channel(s), {result['rulesets']} ruleset(s)."
    )
    for note in result["notes"]:
        print(f"  note: {note}")


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
    print(f"Web UI: {service.web_ui_url()}  (open it: alle ui)")


def cmd_stop(args):
    result = service.stop()
    if result["was_running"]:
        print("Alle stopped (channels kept in config).")
    else:
        print("Alle is already stopped.")


def cmd_ui(args):
    """Open the Web UI in the browser (or print the URL when headless)."""
    if not service.ensure_web_ui():  # start the daemon + wait for a verified serve
        sys.exit(
            "The Web UI isn't responding (or its port is occupied by another "
            "program — the sign-in link is only sent to a verified alle server). "
            "Try: alle restart"
        )
    login_url = service.web_ui_login_url()
    opened = False
    if not args.no_open and sys.stdout.isatty():
        import webbrowser

        try:
            opened = webbrowser.open(login_url)
        except Exception:  # noqa: BLE001 — headless / no browser: fall back to print
            opened = False
    if opened:
        print(f"Opening the alle dashboard: {service.web_ui_url()}")
    else:
        print("Open the alle dashboard in your browser (one-time sign-in link):")
        print(f"  {login_url}")
        port = service.web_ui_url().rsplit(":", 1)[1]
        print(
            "Remote/headless host? Tunnel the same port first — the sign-in "
            "link resolves to your local end:\n"
            f"  ssh -L {port}:127.0.0.1:{port} <user@host>\n"
            "  then open the link above"
        )


def cmd_restart(args):
    service.restart()
    print("Alle restarted. See: alle status")


def cmd_health(args):
    """Liveness probe with a strict exit code: 0 healthy, 1 not. Made for
    monitoring (container HEALTHCHECK, cron, scripts) — `alle status` is the
    human/diagnostic view."""
    result = service.health()
    if args.json:
        import json

        print(json.dumps(result))
    else:
        detail = f"daemon={'up' if result['daemon'] else 'down'} "
        detail += f"sing-box={'up' if result['singbox'] else 'down'} "
        detail += f"channels={result['channels']}"
        runtime_status = (result.get("runtime") or {}).get("singbox")
        if runtime_status and runtime_status != "ok":
            detail += f" ({runtime_status})"
        gateway = result.get("gateway")
        if gateway is not None:
            detail += " gateway=" + (
                "ready"
                if gateway["ok"]
                else "NOT-READY:" + ",".join(gateway["failing"])
            )
        print(("healthy: " if result["ok"] else "unhealthy: ") + detail)
    if not result["ok"]:
        sys.exit(1)


# ---- logs ------------------------------------------------------------------


def cmd_logs(args):
    if args.follow:
        applog.follow(args.lines)
    else:
        print(service.logs_tail(args.lines))


# ---- version ---------------------------------------------------------------


def cmd_version(args):
    if getattr(args, "singbox_path", False):
        from alle import singbox

        print(singbox.bin_path())
        return
    print(__version__)


# ---- daemon (login service) ------------------------------------------------


def cmd_daemon_install(args):
    result = service.daemon_install(linger=args.linger)
    verb = "Reinstalled" if result.get("reinstalled") else "Installed"
    print(f"{verb} the alle login service ({result['manager']}).")
    print(f"  Unit: {result['unit_path']}")
    print("  It auto-starts at login and is running now.")
    if result.get("linger"):
        print("  Lingering enabled: the daemon keeps running after you log out.")


def cmd_daemon_uninstall(args):
    result = service.daemon_uninstall()
    if result.get("removed"):
        print(f"Removed the alle login service ({result['manager']}).")
        print("  Your ~/.alle state (providers, channels, keys) is untouched.")
    else:
        print("No alle login service is installed.")


def cmd_daemon_status(args):
    _print_or_json(service.daemon_status(), output.daemon_status, args.json)


# ---- applier (hidden) ------------------------------------------------------


def cmd_applier(args):
    daemon.run_applier()


def cmd_run(args):
    """The daemon loop in the foreground — a container's PID 1 (or an
    interactive debug run). Identical to the hidden ``applier`` body except
    the operation log is also echoed to stderr, so `docker logs` (or the
    terminal) shows the same timeline as `alle logs`."""
    applog.echo_stderr = True
    # The foreground process IS the daemon: keep any work it triggers
    # (Web-UI-requested mutations calling ensure_running) from spawning a
    # second, detached applier around it. ALLE_APPLIER, not ALLE_SERVICE —
    # a plain foreground run has no supervisor to respawn it, so it must not
    # arm the supervised self-exit-on-upgrade.
    os.environ.setdefault("ALLE_APPLIER", "1")
    # Foreground = ownership: on SIGTERM/SIGINT this process tears down the
    # sing-box child and TUN (a container's stop grace period covers it),
    # unlike the supervised applier whose runtime hands over across respawns.
    daemon.run_applier(own_children=True)


# ---- privileged tun helper --------------------------------------------------


def cmd_helper_install(args):
    result = service.helper_install()
    verb = "Reinstalled" if result.get("reinstalled") else "Installed"
    print(f"{verb} the privileged tun helper (root LaunchDaemon).")
    print(f"  Plist: {result['plist']}")
    print(f"  Serves uid {result['serves_uid']} over {result['socket']}.")
    print(
        "  It runs as root, starts at boot, and is alive now. After this one\n"
        "  install, `alle tun on` needs no sudo — the helper owns sing-box\n"
        "  while tun mode is on."
    )


def cmd_helper_uninstall(args):
    result = service.helper_uninstall()
    if result.get("removed"):
        print("Removed the privileged tun helper (root LaunchDaemon).")
        print("  `alle tun on` now needs the sudo path again until reinstalled.")
    else:
        print("No privileged tun helper is installed.")


def cmd_helper_status(args):
    s = service.helper_status()
    if args.json:
        print(output.json_text(s))
    else:
        print(_helper_status_text(s))


def _helper_status_text(s: dict) -> str:
    if not s.get("supported"):
        return f"Privileged helper unsupported on {s.get('platform')}."
    if not s.get("installed"):
        return (
            "No privileged tun helper installed. tun on uses the sudo path.\n"
            "Install once:  sudo alle helper install"
        )
    state = "running" if s.get("reachable") else "installed but not answering"
    return (
        f"Privileged helper installed ({state}).\n"
        f"  Plist: {s['plist']}\n  Socket: {s['socket']}"
    )


# ---- helper-run (hidden): the LaunchDaemon body -----------------------------


def cmd_helper_run(args):
    from alle import helper

    sys.exit(helper.run_daemon())


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
    # Deliberately not __doc__: the module docstring is developer documentation;
    # --help gets one line and the command list speaks for itself.
    p = _HelpOnErrorParser(
        prog="alle",
        description="A universal VPN client that manages multiple VPN connections "
        "with rule-based routing.",
    )
    p.set_defaults(func=_show_help(p))
    # metavar keeps the usage line short and hides help-less commands (applier)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    # providers
    pr = sub.add_parser("providers", help="manage VPN providers")
    pr.set_defaults(func=_show_help(pr))
    pr_sub = pr.add_subparsers(dest="providers_command")
    pa = pr_sub.add_parser(
        "add",
        help="add a provider, or replace an added token provider's token",
    )
    pa.add_argument("provider", help=_provider_help())
    pa.add_argument(
        "--token",
        help="token value for scripts (single-secret token providers); "
        "skips the interactive prompt",
    )
    pa.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the confirmation when replacing an existing token",
    )
    pa.set_defaults(func=cmd_providers_add)
    pls = pr_sub.add_parser("ls", help="list the providers you've added")
    pls.add_argument("--json", action="store_true", help="print machine-readable JSON")
    pls.set_defaults(func=cmd_providers_ls)
    pd = pr_sub.add_parser(
        "rm", help="remove a provider AND all its channels + credential"
    )
    pd.add_argument("providers", nargs="*", help=_provider_help())
    pd.add_argument("--all", action="store_true", help="remove all added providers")
    pd.add_argument("--dry-run", action="store_true", help="show what would be removed")
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
    ca.add_argument(
        "--label",
        help='optional display label, e.g. "Video Streaming - US" '
        "(the channel id stays the handle commands use)",
    )
    ca.add_argument(
        "--port",
        type=int,
        help="explicit local proxy port for the channel (default: OS-assigned); "
        "declare one when something outside alle must know it ahead of time",
    )
    ca.set_defaults(func=cmd_channels_add)
    cls = ch_sub.add_parser(
        "ls", help="list configured channels by provider (no status)"
    )
    cls.add_argument("--json", action="store_true", help="print machine-readable JSON")
    cls.add_argument("--ids", action="store_true", help="print channel names only")
    cls.add_argument(
        "--refs", action="store_true", help="print provider-qualified channel refs"
    )
    cls.set_defaults(func=cmd_channels_ls)
    csl = ch_sub.add_parser("setlabel", help="set or clear a channel's display label")
    csl.add_argument("channel", help="channel id or provider/id ref (no globs)")
    csl.add_argument(
        "label", nargs="?", default="", help="the label; omit or pass '' to clear it"
    )
    csl.set_defaults(func=cmd_channels_setlabel)
    cr = ch_sub.add_parser("rm", help="remove one or more channels")
    cr.add_argument(
        "refs",
        nargs="*",
        help="channel name, glob, or provider/name ref to remove",
    )
    cr.add_argument("--provider", help="scope channel names/globs to this provider")
    cr.add_argument(
        "--channel",
        nargs="+",
        action="append",
        help="legacy form: alle channels rm <provider> --channel <name>",
    )
    cr.add_argument(
        "--all",
        action="store_true",
        help="remove every channel under --provider",
    )
    cr.add_argument("--dry-run", action="store_true", help="show what would be removed")
    cr.set_defaults(func=cmd_channels_rm)
    for toggle, blurb in (
        (
            "enable",
            "enable one or more disabled channels (materialise them again)",
        ),
        (
            "disable",
            "disable one or more channels — kept in config but not materialised: "
            "no connection to the provider, so its connection-cap slot is freed",
        ),
    ):
        ct = ch_sub.add_parser(toggle, help=blurb)
        ct.add_argument(
            "refs",
            nargs="*",
            help=f"channel name, glob, or provider/name ref to {toggle}",
        )
        ct.add_argument("--provider", help="scope channel names/globs to this provider")
        ct.add_argument(
            "--all",
            action="store_true",
            help=f"{toggle} every channel under --provider",
        )
        ct.add_argument("--dry-run", action="store_true", help="show what would change")
        ct.set_defaults(func=cmd_channels_set_enabled)

    # routes
    ro = sub.add_parser(
        "routes", help="rule-based routing through the router entrypoint"
    )
    ro.set_defaults(func=_show_help(ro))
    ro_sub = ro.add_subparsers(dest="routes_command")

    def _add_matcher_args(parser):
        parser.add_argument(
            "--domain",
            action="append",
            help="destination domain — matches the domain and all its subdomains",
        )
        parser.add_argument(
            "--cidr", action="append", help="destination IP or CIDR block"
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="match all traffic (catch-all — routes everything not matched earlier)",
        )

    rs = ro_sub.add_parser("ruleset", help="create and edit grouped routing rulesets")
    rs.set_defaults(func=_show_help(rs))
    rs_sub = rs.add_subparsers(dest="ruleset_command")
    rsc = rs_sub.add_parser("create", help="create a ruleset with one or more matchers")
    rsc.add_argument("name", help="display name for the ruleset")
    rsc.add_argument(
        "--via",
        dest="target",
        required=True,
        help="exit for matched traffic: <provider>/<channel>, 'direct', or 'block'",
    )
    _add_matcher_args(rsc)
    rsc.set_defaults(func=cmd_routes_ruleset_create)
    rsa = rs_sub.add_parser("add", help="add matchers to an existing ruleset")
    rsa.add_argument("ruleset", help="ruleset id shown by: alle routes ls")
    _add_matcher_args(rsa)
    rsa.set_defaults(func=cmd_routes_ruleset_add)
    rsrm = rs_sub.add_parser("rm", help="remove a whole ruleset by id")
    rsrm.add_argument("ruleset", help="ruleset id shown by: alle routes ls")
    rsrm.add_argument(
        "--dry-run", action="store_true", help="show what would be removed"
    )
    rsrm.set_defaults(func=cmd_routes_ruleset_rm)
    rsrn = rs_sub.add_parser("rename", help="rename a ruleset")
    rsrn.add_argument("ruleset", help="ruleset id shown by: alle routes ls")
    rsrn.add_argument("name", help="new display name")
    rsrn.set_defaults(func=cmd_routes_ruleset_rename)
    rst = rs_sub.add_parser("retarget", help="change a ruleset's exit target")
    rst.add_argument("ruleset", help="ruleset id shown by: alle routes ls")
    rst.add_argument(
        "target",
        help="exit for matched traffic: <provider>/<channel>, 'direct', or 'block'",
    )
    rst.set_defaults(func=cmd_routes_ruleset_retarget)
    rls = ro_sub.add_parser("ls", help="list rulesets in evaluation order")
    rls.add_argument(
        "--channel", help="only rules targeting this channel (name or provider/name)"
    )
    rls.add_argument("--flat", action="store_true", help="show the flat matcher rows")
    rls.add_argument("--json", action="store_true", help="print machine-readable JSON")
    rls.set_defaults(func=cmd_routes_ls)
    rrm = ro_sub.add_parser("rm", help="remove rules by id")
    rrm.add_argument("ids", nargs="+", help="rule id(s) shown by: alle routes ls")
    rrm.add_argument(
        "--dry-run", action="store_true", help="show what would be removed"
    )
    rrm.set_defaults(func=cmd_routes_rm)
    rre = ro_sub.add_parser(
        "reorder", help="replace ruleset evaluation order with the given id sequence"
    )
    rre.add_argument("ids", nargs="+", help="every ruleset id, in the new order")
    rre.add_argument(
        "--flat", action="store_true", help="reorder flat rule ids instead"
    )
    rre.add_argument("--json", action="store_true", help="print machine-readable JSON")
    rre.set_defaults(func=cmd_routes_reorder)
    rks = ro_sub.add_parser(
        "killswitch",
        help="block router traffic that matches no rule (instead of going direct)",
    )
    rks.add_argument(
        "state", nargs="?", choices=["on", "off"], help="omit to show the current state"
    )
    rks.set_defaults(func=cmd_routes_killswitch)
    rla = ro_sub.add_parser(
        "lan",
        help="built-in rules that send LAN/local destinations direct, ahead of "
        "all user rules (default: on)",
    )
    rla.add_argument(
        "state", nargs="?", choices=["on", "off"], help="omit to show the current state"
    )
    rla.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also list the built-in CIDR ranges",
    )
    rla.set_defaults(func=cmd_routes_lan)

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
    st = sub.add_parser("status", help="show system status (run state, router, Web UI)")
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
    sub.add_parser(
        "run",
        help="run the daemon in the foreground (containers/PID 1, debugging; "
        "logs also stream to stderr)",
    ).set_defaults(func=cmd_run)
    he = sub.add_parser(
        "health",
        help="cheap liveness check with a strict exit code (0 healthy, 1 not) "
        "— for monitoring and container HEALTHCHECKs",
    )
    he.add_argument("--json", action="store_true", help="print machine-readable JSON")
    he.set_defaults(func=cmd_health)
    tn = sub.add_parser(
        "tun",
        help="system-wide VPN mode: a TUN device routes ALL system traffic "
        "through the routing rules (needs root)",
    )
    tn.add_argument(
        "state",
        nargs="?",
        choices=["on", "off", "confirm"],
        help="omit to show the current state; 'confirm' keeps a --trial on",
    )
    tn.add_argument(
        "--trial",
        type=int,
        metavar="SECONDS",
        help="with 'on': auto-revert unless `alle tun confirm` runs within the "
        "window (5-3600s) — survives dropped SSH sessions",
    )
    tn.set_defaults(func=cmd_tun)
    ui = sub.add_parser("ui", help="open the Web UI dashboard in your browser")
    ui.add_argument(
        "--no-open",
        action="store_true",
        help="print the sign-in URL instead of opening",
    )
    ui.set_defaults(func=cmd_ui)
    te = sub.add_parser(
        "test",
        help="per-channel table: fresh probe (IP/latency) + traffic totals; "
        "--speed adds download/upload",
    )
    te.add_argument(
        "--speed", action="store_true", help="also run download/upload tests"
    )
    te.add_argument(
        "--channel",
        help="test only this channel (id or provider/id; default: every channel)",
    )
    te.add_argument("--json", action="store_true", help="print machine-readable JSON")
    te.set_defaults(func=cmd_test)

    # export / import / validate (the declarative setup bundle)
    ex = sub.add_parser(
        "export",
        help="write the whole setup as a declarative bundle (contains secrets)",
    )
    ex.add_argument(
        "--out",
        help="output file (default: alle-backup-<date>-<time>.yaml, written 0600; "
        "'-' for stdout)",
    )
    ex.set_defaults(func=cmd_export)
    im = sub.add_parser(
        "import",
        help="apply a bundle: merge into the current setup (default), or "
        "--replace to overwrite it entirely",
    )
    im.add_argument("file", help="bundle file from `alle export` (or hand-written)")
    im.add_argument(
        "--replace",
        action="store_true",
        help="REPLACE the entire setup with the bundle (destructive; confirms)",
    )
    im.add_argument(
        "--yes",
        action="store_true",
        help="with --replace: skip the confirmation prompt (required when not a TTY)",
    )
    im.set_defaults(func=cmd_import)
    gw = sub.add_parser(
        "gateway",
        help="container gateway profile (ALLE_GATEWAY=1): declare the "
        "fail-closed TUN + kill-switch data plane before readiness",
    )
    gw.set_defaults(func=_show_help(gw))
    gw_sub = gw.add_subparsers(dest="gateway_command")
    gwi = gw_sub.add_parser(
        "init",
        help="privilege-check and declare TUN + kill switch (the entrypoint "
        "runs this on every gateway-container start; fails loud when root "
        "mode, /dev/net/tun, or NET_ADMIN is missing)",
    )
    gwi.set_defaults(func=cmd_gateway_init)
    sy = sub.add_parser(
        "sync",
        help="converge on a bundle as the managed desired state: repeat syncs "
        "are idempotent; edits/removals touch only what sync itself created "
        "(the Docker entrypoint runs this on every boot)",
    )
    sy.add_argument("file", help="bundle file (the declared desired state)")
    sy.set_defaults(func=cmd_sync)
    va = sub.add_parser(
        "validate",
        help="check a bundle file against every rule without applying it "
        "(reports all problems with line numbers)",
    )
    va.add_argument("file", help="bundle file to validate")
    va.set_defaults(func=cmd_validate)
    lg = sub.add_parser("logs", help="show alle's operation log")
    lg.add_argument("-f", "--follow", action="store_true", help="stream new log lines")
    lg.add_argument(
        "-n", "--lines", type=int, default=200, help="lines to show (default 200)"
    )
    lg.set_defaults(func=cmd_logs)

    # daemon (login-service install) — advanced; most users never need it (the
    # runtime auto-starts on first use).
    dm = sub.add_parser(
        "daemon", help="install/remove alle as a login service (advanced)"
    )
    dm.set_defaults(func=_show_help(dm))
    dm_sub = dm.add_subparsers(dest="daemon_command")
    di = dm_sub.add_parser(
        "install", help="run the daemon at login (launchd/systemd --user)"
    )
    di.add_argument(
        "--linger",
        action="store_true",
        help="Linux only: keep running after logout (loginctl enable-linger)",
    )
    di.set_defaults(func=cmd_daemon_install)
    dm_sub.add_parser("uninstall", help="remove the login service").set_defaults(
        func=cmd_daemon_uninstall
    )
    ds = dm_sub.add_parser("status", help="show login-service + daemon status")
    ds.add_argument("--json", action="store_true", help="print machine-readable JSON")
    ds.set_defaults(func=cmd_daemon_status)

    ve = sub.add_parser("version", help="print alle's version")
    ve.add_argument(
        "--singbox-path",
        action="store_true",
        help="print the pinned sing-box binary path (e.g. for setcap on Linux)",
    )
    ve.set_defaults(func=cmd_version)

    sub.add_parser("applier").set_defaults(
        func=cmd_applier
    )  # internal: the daemon body

    # privileged tun helper (macOS): root LaunchDaemon that owns sing-box in
    # tun mode, installed once via sudo so `alle tun on` never needs sudo again.
    hp = sub.add_parser(
        "helper",
        help="install/remove the privileged tun helper (root LaunchDaemon, "
        "macOS; `sudo alle helper install` once, then `alle tun on` needs no "
        "sudo)",
    )
    hp.set_defaults(func=_show_help(hp))
    hp_sub = hp.add_subparsers(dest="helper_command")
    hp_sub.add_parser(
        "install", help="install the root helper (run under sudo)"
    ).set_defaults(func=cmd_helper_install)
    hp_sub.add_parser(
        "uninstall", help="remove the root helper (run under sudo)"
    ).set_defaults(func=cmd_helper_uninstall)
    hs = hp_sub.add_parser("status", help="show whether the helper is installed")
    hs.add_argument("--json", action="store_true", help="print machine-readable JSON")
    hs.set_defaults(func=cmd_helper_status)

    sub.add_parser("helper-run").set_defaults(
        func=cmd_helper_run
    )  # internal: the helper daemon body

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
