# CLI reference

Complete reference for the `alle` command-line interface — the primary, complete
surface for managing providers, channels, and the local runtime.

From a checkout, prefix everything with `uv run` (e.g. `uv run alle status`). An
installed build (`uv tool install alle-proxy` / `pipx install alle-proxy` —
see the README's Install section) exposes `alle` directly. The examples below
omit the prefix.

## Contents

- [CLI reference](#cli-reference)
  - [Contents](#contents)
  - [Conventions](#conventions)
  - [Concepts](#concepts)
  - [`alle providers`](#alle-providers)
    - [`alle providers add <provider>`](#alle-providers-add-provider)
    - [`alle providers ls [--json]`](#alle-providers-ls---json)
    - [`alle providers rm <provider>... [--all] [--dry-run] [-y|--yes]`](#alle-providers-rm-provider---all---dry-run--y--yes)
  - [`alle channels`](#alle-channels)
    - [`alle channels add <provider> …`](#alle-channels-add-provider-)
    - [`alle channels ls [--json|--ids|--refs]`](#alle-channels-ls---json--ids--refs)
    - [`alle channels setlabel <channel> [label]`](#alle-channels-setlabel-channel-label)
    - [`alle channels rm <channel>...`](#alle-channels-rm-channel)
    - [`alle channels enable/disable <channel>...`](#alle-channels-enabledisable-channel)
  - [`alle routes`](#alle-routes)
    - [`alle routes ruleset create <name> --via <target> --<matcher>...`](#alle-routes-ruleset-create-name---via-target---matcher)
    - [`alle routes ruleset add <ruleset> --<matcher>...`](#alle-routes-ruleset-add-ruleset---matcher)
    - [`alle routes ruleset rm <ruleset> [--dry-run]`](#alle-routes-ruleset-rm-ruleset---dry-run)
    - [`alle routes ruleset rename <ruleset> <name>` / `retarget <ruleset> <target>`](#alle-routes-ruleset-rename-ruleset-name--retarget-ruleset-target)
    - [`alle routes ls [--channel <ref>] [--flat] [--json]`](#alle-routes-ls---channel-ref---flat---json)
    - [`alle routes rm <id>...`](#alle-routes-rm-id)
    - [`alle routes reorder <ruleset-id>... [--flat] [--json]`](#alle-routes-reorder-ruleset-id---flat---json)
    - [`alle routes killswitch [on|off]`](#alle-routes-killswitch-onoff)
    - [`alle routes lan [on|off]`](#alle-routes-lan-onoff)
  - [`alle locations`](#alle-locations)
  - [`alle status`](#alle-status)
  - [`alle start` / `stop` / `restart`](#alle-start--stop--restart)
  - [`alle upgrade [--check]`](#alle-upgrade---check)
  - [`alle run`](#alle-run)
  - [`alle health [--json]`](#alle-health---json)
  - [`alle tun [on|off]`](#alle-tun-onoff)
  - [`alle test`](#alle-test)
  - [`alle export [--out <file>]`](#alle-export---out-file)
  - [`alle import <file> [--replace] [--yes]`](#alle-import-file---replace---yes)
  - [`alle sync <file>`](#alle-sync-file)
  - [`alle validate <file>`](#alle-validate-file)
  - [`alle logs`](#alle-logs)
  - [`alle ui`](#alle-ui)
  - [`alle daemon`](#alle-daemon)
    - [`alle daemon install [--linger]`](#alle-daemon-install---linger)
    - [`alle daemon uninstall`](#alle-daemon-uninstall)
    - [`alle daemon status [--json]`](#alle-daemon-status---json)
  - [`alle helper`](#alle-helper)
  - [`alle version`](#alle-version)
  - [Output conventions](#output-conventions)
  - [Exit codes](#exit-codes)
  - [Environment variables](#environment-variables)
  - [Files](#files)

## Conventions

- **Help** — run `alle`, `alle <group>`, or any command with `-h/--help` to see usage.
  A group or command invoked with no action prints its help instead of erroring.
- **`--json`** — read commands (`providers ls`, `channels ls`, `routes ls`,
  `locations`, `status`, `test`) accept `--json` for a stable,
  machine-readable projection of the same data. This is the scripting/cross-language interface (pipe to `jq`, etc.).
  It is **not** the programmatic API for `alle`'s own components — those call the core
  (`alle.service`) directly rather than shelling out. Human table output is for
  terminals and may change; `--json` shape is stable.
- **No separate apply step** — adding or removing channels or routing rules writes
  `~/.alle/state.json`; `alle`'s background runtime reconciles sing-box and probes
  channels automatically. `start`/`stop`/`restart` are the user-facing controls.
- **Provider names** — commands take the lowercase key (`nordvpn`, `protonvpn`); the
  brand name (`NordVPN`, `Proton VPN`) is what's shown in output. Both the key and the
  brand (any case) are accepted where a provider is expected.

## Concepts

- **Provider** — a VPN service added to `alle`. Two archetypes:
  - **Token/API** (e.g. `nordvpn`): you provide a credential once; the provider's API
    derives WireGuard keys and resolves servers by location.
  - **Config/portal** (e.g. `protonvpn`): no API — you download a WireGuard `.conf`
    from the provider portal and import it. No credential.
- **Channel** — one VPN location/server under a provider, exposed locally as an
  HTTP+SOCKS proxy on `127.0.0.1:<port>`. Ports are auto-assigned by the OS and
  then stored in `state.json` so they stay stable across restarts and re-imports.
  A channel is identified by its auto-generated **id**, globally unique when
  provider-qualified (e.g. `nordvpn/united_states_1`) — the permanent handle
  every command, routing rule, and metric uses, and what the `ID` column shows.
  A channel may also carry an optional **label**, a friendly display name
  (`alle channels setlabel`) that is presentation only and never a handle.
- **Router entrypoint** — one additional, always-on HTTP+SOCKS proxy that
  dispatches each connection by routing rule to a channel, `direct`, or `block`.
  With no rules it is a transparent pass-through (everything goes direct, no
  VPN). Its port is assigned once and treated as a contract — it never changes
  across restarts. Channel ports remain fully usable alongside it.
- **Runtime** — the background process managed by `alle start`/`stop`/`restart` that
  reconciles `state.json` into one sing-box process and heartbeat-probes each channel.
  It auto-starts on the first mutation or `alle start` and runs for the session;
  [`alle daemon install`](#alle-daemon) optionally promotes it to a supervised
  login service.

WireGuard is connectionless, so there is no connect/disconnect. A channel exists in
config; its health is whatever the most recent background probe found.

---

## `alle providers`

Manage VPN providers.

### `alle providers add <provider>`

Add a provider — **or replace an already-added token provider's token**.

- **Token providers** (`nordvpn`): prompts for the credential (input hidden, shown as
  `*`), validates it against the provider API, and stores it in `credentials.yaml`.
- **Config providers** (`protonvpn`): just registers the provider — no credential —
  and prints how to import a `.conf`.

**Replacing a token (idempotent add).** Running `add` for a token provider that is
already configured is the supported way to rotate a bad/expired token — you don't
remove and re-add (which would delete every channel). It shows the current masked
token, confirms (`Replace its token? [y/N]`), validates the new one, and only then
replaces it:

- **Validation-first:** if the new token is rejected, the **old token is kept** and
  the provider error is printed — a bad paste can't lock you out.
- **Same token is a no-op:** re-entering the token already stored changes nothing —
  it reports `already has that token — nothing to do` and does **not** re-resolve
  channels.
- **Channels re-resolve:** after a successful replace with a *different* token, every
  one of that provider's channels is re-resolved with the new credential in one pass
  (a fresh server per channel). A channel that can't be re-resolved right now keeps
  its current server and refreshes on the next reconnect; the summary lists both sets.
- **The token is never displayed** — only a masked preview (`nGx4****a91k`) is ever
  shown, in any command, JSON, log, or the Web UI.

`--token <value>` supplies the token non-interactively (for scripts; single-secret
token providers only) and `-y`/`--yes` skips the replace confirmation. Off a
terminal, a replace refuses without `--yes` rather than silently overwriting.

```bash
alle providers add nordvpn                       # prompts for an access token
alle providers add protonvpn                     # registers; import channels with --config
alle providers add nordvpn                       # already added → confirm + replace token
alle providers add nordvpn --token "$TOK" --yes  # scriptable token rotation
```

### `alle providers ls [--json]`

List added providers as a table: `PROVIDER`, `TYPE` (`token`/`config`), and `DETAIL`
(a masked credential for token providers, or the number of imported `.conf` files for
config providers).

```text
PROVIDER    TYPE    DETAIL
----------  ------  -------------
NordVPN     token   ******8fb7
Proton VPN  config  2 .conf files
```

### `alle providers rm <provider>... [--all] [--dry-run] [-y|--yes]`

Remove one or more providers **and all their channels and stored credentials**.
Prompts for confirmation unless `-y` is given.

**Refused while any of the provider's channels is targeted by a routing rule** —
the error lists every referencing rule and the exact `alle routes rm …` to run
first (see [`alle routes`](#alle-routes)). There is no force/cascade: routing
config only changes when you change it.

```bash
alle providers rm protonvpn -y
alle providers rm nordvpn protonvpn -y
alle providers rm --all --dry-run
```

- `--dry-run` prints what would be removed without changing state.
- `--all` removes every added provider; combine with `--dry-run` first when in doubt.

---

## `alle channels`

Manage channels under a provider. The two ways to add a channel are **mutually
exclusive**, one per provider archetype.

### `alle channels add <provider> …`

**Token/API providers** — locate a server by country (and optionally city):

```bash
alle channels add nordvpn --country "United States"
alle channels add nordvpn --country "United States" --city "Seattle"
```

- `--country` is required; `--city` is optional (omit = any city in the country).
- Each add resolves a fresh recommended server, so repeating the same location creates
  a distinct channel: `united_states_1`, `united_states_2`, …
- See selectable locations with [`alle locations`](#alle-locations).

**Config providers** — import a WireGuard `.conf`:

```bash
alle channels add protonvpn --config ~/Downloads/wg-US-CA-842.conf
```

- `--config` cannot be combined with `--country`/`--city`.
- The channel **id is the file name** (`wg-US-CA-842.conf` → `wg_us_ca_842`), no
  numeric suffix. Re-importing the same file is an **update in place** (keys may have
  rotated) — it keeps the id and local port stable and does not create a duplicate.
  Re-importing a **byte-identical** `.conf` (same keys, server, and location)
  changes nothing and says so (`… already exists … nothing to do`) instead of
  reporting a misleading update.
- Country/city are parsed best-effort from the file name's ISO codes (ProtonVPN's
  `wg-<CC>-<SUB>-<n>` convention, e.g. `US`/`CA` → United States / California). Only
  the country code is reliable; a missing/unknown subdivision shows as `(Unknown)`.
  `alle` never geo-locates the endpoint to guess.

Both forms accept `--label "<text>"` to give the channel a friendly display name
(see [`alle channels setlabel`](#alle-channels-setlabel-channel-label)):

```bash
alle channels add nordvpn --country "United States" --label "Streaming - US"
```

Both forms also accept `--port <n>` to **declare** the channel's local proxy
port instead of taking an OS-assigned one — for when something outside alle
(a firewall rule, a compose file publishing the port) must know it ahead of
time. A declared port that another channel (or the router entrypoint) already
holds is refused loudly; nothing is ever silently moved. Without `--port`,
allocation is OS-assigned exactly as before. (`ALLE_PORT_BASE=<n>` switches
the *allocator* to sequential-from-`n` — an opt-in used by the container
image; see `docs/docker.md`.)

### `alle channels ls [--json|--ids|--refs]`

List configured channels (static config only — no live status). Columns: `LABEL`,
`ID`, `PORT`, `COUNTRY`, `CITY`, `STATUS`. `LABEL` is the friendly display name
(falls back to the id when unset); `ID` is the globally-unique,
provider-qualified handle (`nordvpn/japan_1`) — the same ref every command
accepts, which is why no separate provider column is needed. `STATUS` is the
administrative `enabled` / `disabled` state (see
[`alle channels disable`](#alle-channels-enabledisable-channel)), distinct from
probe liveness — this table stays the same whether alle is up or down.

```text
LABEL           ID                        PORT    COUNTRY        CITY        STATUS
--------------  ------------------------  ------  -------------  ----------  --------
Streaming - US  nordvpn/japan_1           :53124  Japan          (Any City)  enabled
seattle_1       nordvpn/seattle_1         :53125  United States  Seattle     disabled
wg_us_ca_842    protonvpn/wg_us_ca_842    :53126  United States  California  enabled
```

`--json` carries the same fact as a boolean `enabled` per channel.

For scripting, print just channel ids or provider-qualified refs (labels are
never used as identifiers):

```bash
alle channels ls --ids
alle channels ls --refs
```

### `alle channels setlabel <channel> [label]`

Set (or, with no `label`, clear) a channel's display label. The label is
presentation only — commands, routing rules, and metrics always use the id, so
relabelling is safe and cascades nowhere. Labels may duplicate and are never
accepted as a channel ref.

```bash
alle channels setlabel united_states_seattle_1 "Streaming - US West"
alle channels setlabel nordvpn/japan_1 "Test runner"   # qualified ref works too
alle channels setlabel japan_1                          # omit to clear → shows the id again
```

`<channel>` is a channel id or `provider/id` ref (no globs — a label targets one
channel). Every channel table (`channels ls`, `test`) shows
the same `LABEL` + `ID` columns — `LABEL` is the label or the id when unset, `ID`
is the provider-qualified ref (`nordvpn/japan_1`); `--json` on those carries the
bare `name` (id), `provider`, and `label` separately.

### `alle channels rm <channel>...`

Remove one or more channels (also drops stored metrics).

```bash
alle channels rm japan_1 united_states_seattle_1
alle channels rm protonvpn/wg_us_ca_842
alle channels rm 'united_states_*' --dry-run
alle channels rm 'united_states_*'
alle channels rm --provider nordvpn --all
```

- Plain channel names are resolved across providers. If the same name exists under
  multiple providers, use a provider-qualified ref like `nordvpn/japan_1`.
- Glob patterns (`*`, `?`, `[abc]`) match channel names. Quote patterns in shells so
  the shell does not expand them before `alle` sees them.
- `--provider <provider>` scopes names, globs, and `--all` to one provider.
- `--dry-run` prints exactly what would be removed without changing state.
- Compatibility form: `alle channels rm <provider> --channel <name>` still works.
- **A channel targeted by a routing rule cannot be removed.** The refusal lists
  *all* referencing rules in one pass with the exact fix
  (`alle routes rm r1 r2 …`); `--dry-run` reports the same conflict. Check ahead
  with `alle routes ls --channel <name>`. There is no `--force` — removing rules
  is always its own explicit step, so a channel removal can never silently
  reroute traffic.

### `alle channels enable/disable <channel>...`

Set a channel's **administrative state** without removing it. A **disabled**
channel is kept in the config but not materialised at all: no local proxy
port, no WireGuard endpoint, no handshake or keepalive toward the provider —
**it occupies no connection slot** on plans that cap simultaneous connections
(NordVPN and Proton VPN allow ~10). Hold more channels than the cap and enable
only the ones you want live:

```bash
alle channels disable japan_1                    # free its provider slot
alle channels disable 'united_states_*'          # globs and batches, like rm
alle channels disable --provider nordvpn --all
alle channels enable nordvpn/japan_1             # dial it again
alle channels disable japan_1 --dry-run          # plan without changing state
```

- Same ref grammar as `channels rm`: bare ids, `provider/id` refs, globs,
  `--provider` scoping, `--all`, `--dry-run`. Channels already in the requested
  state are reported as no-ops.
- Disabled ≠ unhealthy. Enable/disable is *intent*; "active" is *liveness*
  (the latest probe). A disabled channel shows `Disabled` in `status` and a
  skipped `Disabled` row in `test` — listed everywhere, probed nowhere.
- **A channel targeted by a routing rule cannot be disabled** — the same
  restrict-only refusal as `rm`, listing every referencing rule. Rules can't
  target a disabled channel either; enable it first.
- Disabling is purely **local**: alle stops dialling the server. It does not
  deregister the device/peer from the provider account.
- Enabling is instant when the channel has its WireGuard params (the usual
  case). A channel imported *disabled* from a bundle without a `wg` snapshot
  resolves its server via the provider API at enable time — the one case where
  `enable` needs the network, and it fails cleanly (channel stays disabled) if
  the API is unreachable.
- The daemon reconciles on every toggle: exactly that one endpoint is added or
  removed from the live sing-box config; other channels never blip.

---

## `alle routes`

Rule-based routing through the router entrypoint. Rules are evaluated **top to
bottom — first match wins**; use `alle routes reorder` or drag-reorder in the
Web UI dashboard to change evaluation order. Traffic that matches no rule goes
**direct (no VPN)** unless the [kill-switch](#alle-routes-killswitch-onoff) is on.

Before any user rule, a **built-in LAN block** (on by default — see
[`alle routes lan`](#alle-routes-lan-onoff)) sends private, link-local, and
multicast destinations direct, so a catch-all VPN rule never cuts off printers,
NAS boxes, router admin pages, or LAN discovery.

**Domain matchers always cover subdomains.** A domain you add
(`netflix.com`, `api.openai.com`) matches that domain *and all of its
subdomains*, dot-boundary (`example.co.uk` never matches `otherexample.co.uk`).
There is deliberately no exact-only domain matcher — one semantic keeps rules
predictable. `routes ls` flags any rule that can never match because an
earlier rule (or the built-in LAN block, when on) already covers it.

**DNS depends on the mode — mind DNS leakage.**

- *Explicit-proxy mode (the default):* alle routes by destination
  (IP/CIDR/domain), it does not intercept the system resolver. An app that
  resolves a hostname *locally* and then connects by IP can leak which host it
  is contacting through the local DNS query, and a hostname resolved to a
  sinkhole address by another system-wide TUN VPN on the same host can defeat
  a domain rule. To keep name resolution away from the host resolver, point
  apps at the proxy with **remote DNS** — `socks5h://` (not `socks5://`) or an
  HTTP proxy that resolves remotely — so sing-box resolves the destination,
  not the host.
- *[TUN mode](#alle-tun-onoff):* alle **owns the resolver** — plain DNS from
  every app is hijacked and answered by sing-box, so the `socks5h://` advice
  becomes moot and local-resolver leakage disappears. See the tun section for
  where the upstream query goes (a public resolver, dialed direct).

The entrypoint's port is shown by `alle status` and by `routes ls`; it is
allocated on the first daemon start and then never changes.

### `alle routes ruleset create <name> --via <target> --<matcher>...`

Create a named ruleset: a contiguous, ordered block of matchers that all share
one exit target. `<target>` is where matched traffic exits:

- `<provider>/<channel>` — a channel (must exist; see `alle channels ls --refs`),
- `direct` — straight to the network, no VPN,
- `block` — refuse the connection.

Matcher flags may be repeated; creation is atomic, so a multi-domain ruleset
lands in one transaction and one daemon reconcile:

| Matcher        | Matches                                                    |
| -------------- | ---------------------------------------------------------- |
| `--domain <d>` | `d` and all of its subdomains (dot-boundary)               |
| `--cidr <net>` | destination IP in `net` (a bare IP means that one address) |
| `--all`        | everything — the catch-all for "VPN by default"            |

```bash
alle routes ruleset create Streaming --via nordvpn/united_states_1 --domain netflix.com --domain hulu.com
alle routes ruleset create LocalDirect --via direct --cidr 192.168.0.0/16
alle routes ruleset create BlockTrackers --via block --domain tracker.example.com
alle routes ruleset create DefaultVPN --via nordvpn/japan_1 --all
```

Notes:

- **Ruleset order is priority**: first matching ruleset wins. Matchers inside a
  ruleset are unordered because they all exit the same way.
- Domain rules work for HTTPS on any port (destinations are sniffed via SNI when
  the client sends an IP or uses SOCKS without a hostname).
- `--cidr` matches IP-literal destinations; domain destinations are **not**
  resolved for CIDR matching.
- If an earlier ruleset already covers a matcher (e.g. `--domain google.com`
  before `--domain api.google.com`), `routes ls` marks the later matcher as
  **shadowed** and it will never match.

### `alle routes ruleset add <ruleset> --<matcher>...`

Add matcher(s) to an existing ruleset block. The new matchers inherit that
ruleset's priority immediately; lower-priority duplicates are left in place and
shown as shadowed rather than silently deleted.

### `alle routes ruleset rm <ruleset> [--dry-run]`

Remove a whole ruleset block. (A ruleset whose matchers are all removed via
`routes rm` simply has no rows left, so it no longer appears.)

### `alle routes ruleset rename <ruleset> <name>` / `retarget <ruleset> <target>`

Rename a ruleset or change its exit target. Renaming is presentation-only and
does not trigger a sing-box reconcile; retargeting does.

### `alle routes ls [--channel <ref>] [--flat] [--json]`

List rulesets in evaluation order. The header line shows the entrypoint address
and its unmatched behavior. `--channel <name|provider/name>` filters to flat
matcher rows targeting one channel — useful before removing it. `--flat` shows
the raw matcher rows (`ID`, `RULESET`, `MATCH`, `TARGET`, `NOTE`) for debugging
against sing-box logs.

```text
Router entrypoint 127.0.0.1:54585 — 3 rule(s), LAN bypasses VPN, unmatched → direct
rs1  Streaming → nordvpn/united_states_1 (2 matcher(s))
  r1  domain_suffix netflix.com
  r2  domain_suffix hulu.com
rs2  Direct exceptions → direct (1 matcher(s))
  r3  ip_cidr 192.168.0.0/16
```

### `alle routes rm <id>...`

Remove matcher rows by id (`--dry-run` to preview). Unknown ids are reported all
at once and nothing is removed.

```bash
alle routes rm r2
alle routes rm r1 r3 --dry-run
```

### `alle routes reorder <ruleset-id>... [--flat] [--json]`

Replace the ruleset-block evaluation order with a full list of ruleset ids. Pass
every existing ruleset id exactly once; ids stay stable and only their order
changes. `--flat` is a debug escape hatch that reorders raw rule ids, but refuses
any permutation that would split a ruleset block.

```bash
alle routes reorder rs3 rs1 rs2
```

### `alle routes killswitch [on|off]`

Block router traffic that matches no rule, instead of letting it go direct.
Run without an argument to show the current state.

```bash
alle routes killswitch on
alle routes killswitch
```

- Applies to the router entrypoint **and**, when [TUN mode](#alle-tun-onoff) is
  on, to all system traffic — per-channel ports are always unaffected.
  (Commercial VPN apps use "kill switch" for a system-wide block; alle's is
  system-wide exactly when TUN mode is on.)
- `alle status` and `routes ls` show `unmatched → block — kill-switch ON` while
  active, so it's always visible why unmatched traffic fails.

### `alle routes lan [on|off]`

Toggle the built-in default-direct rules for LAN/local traffic (default: **on**,
the recommended state). Run without an argument to show the current state;
`-v`/`--verbose` also lists the covered ranges.

```bash
alle routes lan
alle routes lan off
alle routes lan on -v
```

While on, destinations in these ranges go direct **ahead of every user rule**,
so even an `--all` catch-all cannot capture them:

| Ranges                                          | What they are                             |
| ----------------------------------------------- | ----------------------------------------- |
| `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` | IPv4 private networks                     |
| `169.254.0.0/16`, `127.0.0.0/8`                 | IPv4 link-local, loopback                 |
| `224.0.0.0/4`, `255.255.255.255/32`             | IPv4 multicast (mDNS/SSDP), broadcast     |
| `::1/128`, `fe80::/10`, `fc00::/7`, `ff00::/8`  | IPv6 loopback, link-local, ULA, multicast |

Notes:

- The built-in rules are fixed — they cannot be edited or removed individually;
  this toggle is the whole surface. They never appear in `alle routes ls`.
- Applies to the router entrypoint **and**, when [TUN mode](#alle-tun-onoff) is
  on, to all system traffic; per-channel ports are unaffected.
- DNS is deliberately **not** excluded from the tunnel: sending plain DNS direct
  by default would leak browsing activity, so DNS traffic stays subject to your
  rules.
- `alle status` and `routes ls` append `— LAN direct off` to the router line
  while the protection is disabled.

---

## `alle locations`

`alle locations <provider> [--country "<country>"] [--refresh] [--json]`

List a token provider's selectable countries (and their cities). With `--country`,
list just that country's cities. `--refresh` forces a re-fetch of the cached list.
Config providers have no locations API and print guidance instead.

```bash
alle locations nordvpn
alle locations nordvpn --country "United States"
```

---

## `alle status`

`alle status [--json]`

The system-level summary: whether `alle` is running, per-provider channel
counts, the router entrypoint's address and posture, and the Web UI and REST
API endpoints —
deliberately **no per-channel table**. Channel detail lives in one place,
[`alle test`](#alle-test) (fresh probes + traffic totals); rendering the same
rows here from cached probes would just duplicate that table behind a probe-age
column. Daemon problems (version skew after an upgrade, a crash-looping or
config-rejected sing-box) surface as warning lines.

```text
Alle - Active
  Channels  NordVPN: 6 channels (4 enabled), Proton VPN: 1 channel  (details: alle channels ls)
  Router    127.0.0.1:54585 — 2 rule(s), LAN bypasses VPN, unmatched → direct  (details: alle routes ls)
  Web UI    http://alle-cb9cd104.localhost:58601  (open it: alle ui)
  REST API  http://alle-cb9cd104.localhost:58601/api/v1 — shares the Web UI listener; Bearer auth required
```

A provider's count shows the enabled split (`6 channels (4 enabled)`) only
when some of its channels are [disabled](#alle-channels-enabledisable-channel).

The router line always states the entrypoint's full posture — `pass-through
(no rules)`, or the rule count plus the two priority boundaries: the built-in
LAN block (`LAN bypasses VPN` / `LAN follows rules`) and unmatched handling
(`unmatched → direct`, or `… → block — kill-switch ON`) — so it is never
ambiguous whether traffic is inside a VPN. `--json` still carries the full
per-channel state (the Web UI and scripts read it), plus additive `web_ui` and
`rest_api` endpoint fields. Neither output includes the Bearer secret; only the
human rendering is a summary.

---

## `alle start` / `stop` / `restart`

- **`alle start`** — start the runtime (background reconciler + sing-box). Runs idle if
  no channels are configured yet. The first interactive start offers — exactly
  once, ever — to register the login service (`alle daemon install`); answer
  `n` and it never asks again. `--yes` installs the service without asking,
  `--no-service` declines without asking (both suited to scripts). The offer
  is skipped entirely on non-TTY runs, in containers, under a supervisor, or
  when a unit already exists — and a skipped offer is not a spent one.
- **`alle stop`** — stop the runtime. Channels stay in config; only the processes stop.
- **`alle restart`** — stop then start. Also clears any `Reconnect failed` flags so
  dead channels are retried from scratch.

```bash
alle start
alle restart
alle stop
```

---

## `alle upgrade [--check]`

Upgrade alle through the tool that installed it — alle never replaces its own
files. The install channel (uv tool, pipx, or pip) is detected and the upgrade
delegated to it; the daemon then restarts on the new version (or, when a
service unit owns it, self-exits for the supervisor to respawn).

```bash
alle upgrade --check   # ask PyPI for the latest version; changes nothing
alle upgrade           # delegate to uv tool/pipx/pip, then restart the daemon
```

- `--check` contacts PyPI **only when you run it** — alle never checks for
  updates in the background. The Web UI's version badge does the same check on
  click.
- A **git checkout** refuses (upgrade it with git), a **container** refuses
  (the image is immutable — pull a new tag and recreate the container), and an
  undetectable channel refuses rather than guess.
- `--json` prints the machine-readable result.

---

## `alle run`

The same daemon loop, in the **foreground**: the process does not detach, and
every operation-log line is also written to stderr. This is what a container
runs as PID 1 (`docker logs` gets the timeline; a restart policy replaces the
login service — see `docs/docker.md`), and it doubles as an interactive way
to watch the daemon work. `Ctrl-C`/SIGTERM stop it cleanly. Everything else —
locking, pidfile, the Web UI thread — is identical to the background daemon,
so a foreground run and `alle start` exclude each other like two daemons
always have.

---

## `alle health [--json]`

A **cheap liveness probe with a strict exit code**: `0` when the daemon is
running and sing-box is up, `1` otherwise. Built for machines — container
`HEALTHCHECK`s, cron, monitoring — where [`alle status`](#alle-status) is the
human/diagnostic view. No probes and no network I/O: two pidfile checks and a
state read.

```bash
alle health           # healthy: daemon=up sing-box=up channels=3
alle health --json    # {"ok": true, "daemon": true, "singbox": true, ...}
```

---

## `alle tun [on|off]`

System-wide VPN mode: sing-box creates a TUN device and takes over the
system's default route, so **all** system traffic — every app, raw sockets,
UDP — enters the same routing rules the router entrypoint uses. Run without an
argument to show the current state.

```bash
alle tun on
alle tun
alle tun off
```

- **One rule table, two doors.** The tun joins the router entrypoint's
  compiled rules (built-in LAN-direct block, your rulesets, unmatched
  handling) — nothing is duplicated, and per-channel proxy ports plus the
  router port keep working unchanged. With tun on, `alle routes killswitch on`
  is genuinely system-wide.
- **Privilege: one-time install, then no sudo.** Creating the TUN device is
  privileged, so `alle tun on` refuses unless one of these holds (the helper
  is the intended steady state; the others are fallbacks):
  - **macOS — privileged helper (recommended).** Install it **once**:

    ```bash
    sudo alle helper install
    ```

    This registers a root LaunchDaemon that owns sing-box while tun mode is on.
    After this single install, `alle tun on` (and the Web UI / companion
    toggle) works as your normal user with **no password, ever** — it
    survives reboots (launchd starts the helper at boot). Remove it with
    `sudo alle helper uninstall`; check it with `alle helper status`. See
    `docs/security.md` for the helper's hard scope.

  - **Linux — setcap (no root, recommended).** Grant the pinned sing-box
    binary the capability once; then `alle tun on` works as your normal user
    with an unprivileged daemon — no root process anywhere:

    ```bash
    sudo setcap cap_net_admin,cap_net_raw+ep "$(alle version --singbox-path)"
    ```

    No restart step is needed: enabling tun re-execs sing-box automatically
    (file capabilities are acquired at exec time, so a plain reload would not
    pick them up), and the privilege gate checks the *running* process's
    capabilities, not just the binary's. Re-run the grant after any sing-box
    version bump (the pinned path changes). Verified in the Tier 2 sandbox.

  - **Any platform — sudo one-off (fallback).** If you haven't installed the
    helper (macOS) or setcap (Linux), sing-box must run as root for the
    toggle. Stop any user-level daemon first, then run under sudo against your
    normal state directory:

    ```bash
    alle stop
    sudo ALLE_HOME="$HOME/.alle" alle tun on
    ```

    The rest of the alle CLI is **not** sudo-only — only this one-off tun
    enable needs it, and the helper/setcap paths exist to avoid even that.

- **DNS is owned by alle in TUN mode.** Plain DNS from any app is hijacked
  and answered by sing-box; the upstream is `1.1.1.1` over UDP, dialed
  **direct** — never a LAN resolver, and (in v1) never a channel: with
  multiple channels there is no single "the tunnel" to prefer, so resolution
  goes direct even under the kill-switch (rule matching and endpoint dialing
  need it). Apps doing their own encrypted DNS (DoH/DoT) are ordinary traffic,
  subject to your rules like anything else.
- **No IPv6 while on the VPN — blocked, not leaked (a provider restriction).**
  The supported providers' WireGuard configs are **IPv4-only** (NordVPN and
  Proton VPN ship no IPv6 tunnel addressing), so IPv6 *cannot* be carried
  through the tunnel — that is their restriction, not alle's. The honest
  options are to leak IPv6 around the VPN or to block it; alle blocks it:
  with tun on, the v6 default route is captured into the tun and all IPv6 is
  rejected (`curl -6` fails with connection reset; your home IPv6 never
  appears on IP-check sites). LAN-direct still passes local IPv6
  (link-local/ULA) when enabled, DNS is `ipv4_only` so apps rarely even try
  v6, and IPv6 returns to normal the moment tun is off. If a future provider
  ships IPv6 WireGuard endpoints, real IPv6-over-VPN becomes possible.
- **Kill-switch + tun blocks alle's own provider calls.** Only sing-box's own
  sockets bypass the tun; the alle daemon's provider API traffic (NordVPN
  server re-resolution on reconnect, `alle locations --refresh`) is ordinary
  direct egress, so with both tun **and** the kill-switch on it is blocked
  like any other unmatched traffic (verified live in the sandbox). Channels
  keep flowing — WireGuard runs inside sing-box — but automatic reconnect
  cannot fetch a replacement server until a rule allows it or the kill-switch
  is lifted.
- **Crash behavior (honest asterisk).** Enforcement lives in the sing-box
  process. If it crashes, the utun and its routes vanish and the kernel falls
  back to the physical route — traffic fails **open** for the ~2s supervision
  window until sing-box restarts. A firewall-anchored always-on kill-switch is
  future hardening.
- **Trying it safely: `alle tun on --trial <seconds>`.** Arms a detached
  watchdog *before* activation (the `iptables-apply` pattern): unless you run
  `alle tun confirm` within the window, TUN mode reverts off automatically —
  even if your SSH session died with the network. `alle tun` shows the
  pending trial; a plain `alle tun on`/`off` supersedes it.
- **Recovery.** `alle tun off` (reconciles the tun away); if the CLI is
  broken, `alle stop` — a cleanly killed sing-box always restores the routes.
  Do **not** `pkill sing-box`: the daemon restarts it with the same tun config
  within ~2s. Full ordered runbook (read it before the first activation):
  [docs/tun-runbook.md](tun-runbook.md).
- `alle status` shows a `TUN` line while active.

---

## `alle test`

`alle test [--channel <id>] [--speed] [--fail] [--json]`

**The** per-channel table: probe channels **now** (rather than waiting for the
next background cycle) and print each channel's fresh connectivity plus its
cumulative traffic: `LABEL`, `ID`, `PORT`, `COUNTRY`, `CITY`, `STATE`,
`LATENCY` (probe latency), `IP` (exit IP), `SENT`, `RECV` (durable totals since
counters began). With `--channel`, test just one channel by id. `STATE` is
`Healthy`, or the failure reason (`Stopped` while the runtime is down,
otherwise the probe error — e.g. `Timeout`, `Failed`); a failing channel marks
its own row, it never aborts the rest of the run. A
[disabled](#alle-channels-enabledisable-channel) channel is listed too —
skipped with `STATE` `Disabled`, not probed (it has no inbound to probe) and
never counted as failed. Every channel table shares the same `LABEL` + `ID`
lead — `LABEL` is the display name (the id when no label is set), `ID` is the
provider-qualified ref commands take.

```text
LABEL         ID                      PORT    COUNTRY        CITY        STATE     LATENCY  IP             SENT      RECV
------------  ----------------------  ------  -------------  ----------  --------  -------  -------------  --------  --------
Test runner   nordvpn/japan_1         :53124  Japan          (Any City)  Healthy   398.0ms  93.118.43.151  104.0 MB  396.7 MB
seattle_1     nordvpn/seattle_1       :53125  United States  Seattle     Disabled  -        -              12.5 MB   88.0 MB
wg_us_ca_842  protonvpn/wg_us_ca_842  :53126  United States  California  Timeout   -        -              28.1 MB   141.8 MB
```

Add `--fail` for monitoring use: exit code 1 when any probed channel is
unhealthy — or when nothing was probed at all (a monitor that watched nothing
must not report success). Without it, `alle test` is informational and always
exits 0. The daemon-liveness counterpart is [`alle health`](#alle-health---json).

Add `--speed` to run the slower download/upload test after the fresh
connectivity probe; it appends `DOWNLOAD` and `UPLOAD` columns. Speed tests run
only for channels that were healthy in that same probe; an unhealthy or
disabled channel keeps its row (reason in `STATE`, `-` in the speed columns)
while the others proceed.

In an interactive terminal `alle test --speed` **streams**: the table header
prints up front and each channel's row appears the moment its own test completes
(with a live progress indicator on the channel under test), instead of waiting
for the whole batch to finish. Piped / non-TTY output and `--json` still produce
the complete table / a single JSON object at the end.

```bash
alle test
alle test --channel wg_us_ca_842
alle test --speed
alle test --speed --json
```

**Interpretation:** speed-test `LATENCY` is a min round trip through the tunnel and
bundles TCP/TLS setup, so it reads higher than raw ping — the *ordering* across channels
is what's meaningful. `UPLOAD` is bounded by your machine's shared uplink, so channels
tend to converge there.

**Traffic accuracy note:** `SENT`/`RECV` are sampled from live connections
every couple of seconds. Transfers that open and finish entirely between two
samples are under-counted, and up to one sample interval is skipped when
sing-box or the daemon (re)starts (counters are re-baselined so nothing is
ever counted twice) — both inherent to the data source, not bugs. Use them for
cumulative usage trends, not exact accounting. (`--json` also carries a
`traffic_updated_at` epoch per row — when traffic last flowed.)

---

## `alle export [--out <file>]`

Write the entire setup — providers (with their credentials), channels of both
archetypes, rulesets, and the router toggles — as one declarative YAML
**bundle**, for backup, moving to another machine, or replaying after a
reinstall. Defaults to `alle-backup-<date>-<time>.yaml` in the current
directory, written `0600`; `--out -` prints to stdout instead (for scripts).

```bash
alle export                          # -> alle-backup-20260709-143022.yaml (0600)
alle export --out ~/setup.yaml
alle export --out - | wc -l          # stdout for scripts
```

- **The file is a secret.** It contains WireGuard private keys and provider
  tokens — everything needed to recreate the setup. Keep it private.
- **Runtime state never travels in a bundle**: no probe results, speed/latency
  history, traffic totals, reconnect bookkeeping, rule/ruleset ids — and **no
  ports** (see `import --replace` below).

The bundle is a **declarative startup config**, not a state dump — `export` is
a convenience; the same file can be hand-written from scratch. Token-provider
channels may omit the `wg` snapshot entirely (`{country: Sweden}` is enough):
their WireGuard params are derived state, resolved via the provider token at
apply time. Config-provider channels (Proton `.conf`) must include `wg` — it
*is* their configuration. Matchers in hand-written rulesets can be bare
strings (`netflix.com`, `10.8.0.0/16`, `all`), inferred exactly like
`alle routes ruleset create --domain`.

**Guides** — writing a setup from scratch, with per-provider how-to for
filling the YAML: [declarative-config.md](declarative-config.md). Format
reference, apply semantics, and caveats (including running one setup on two
machines): [bundle.md](bundle.md).

---

## `alle import <file> [--replace] [--yes]`

Apply a bundle. Two modes, one command:

- **Default (merge)** — layer the bundle onto the current setup; nothing is
  removed:
  - Providers and credentials are added; a credential that differs from the
    stored one is replaced (called out explicitly in the summary — an old
    backup can overwrite a newer token).
  - Channels upsert by `(provider, channel id)` — an existing channel is
    updated in place (its local port kept), a new id is created (fresh port).
  - **Token channels resolve a fresh server via the provider token** when they
    are new to this machine (or their location changed); an existing channel
    with the same location keeps its live params (no API call, no churn); the
    bundle's `wg` snapshot is used only when fresh resolution fails (offline,
    API down) — reported in the summary, refreshed later by auto-reconnect.
    Config channels are applied exactly as written.
  - **Rulesets always append at the bottom of the priority order.** Under
    first-match-wins an appended block can never hijack existing routing; use
    `alle routes ls` (shadow lint) and `alle routes reorder` afterwards.
  - `killswitch` / `lan_direct`, when present in the bundle, are applied.

```bash
alle import alle-backup-20260709-143022.yaml                  # merge
alle import alle-backup-20260709-143022.yaml --replace --yes  # replace, non-interactive
```

- **`--replace`** — **overwrite the whole setup** instead of merging.
  Providers, channels, credentials, rulesets, and toggles not in the bundle are
  removed. Destructive: it prompts for confirmation, and requires `--yes` when
  not running on a TTY. The bundle must be self-contained (a ruleset target
  must reference a channel defined in the bundle). Ports are local allocations,
  so they don't survive a replace — a channel whose `(provider, id)` already
  exists keeps its current port, but new identities get fresh ports and the
  router entrypoint port is untouched; repoint apps at the ports from
  `alle status` after a cross-machine replace. Runtime state resets (fresh
  probes, no carried-over history).

The whole file is validated first — every WireGuard field, matcher, and target
reference — and rejected as a whole with a per-entry error list (path +
reason) on any problem. An import never half-applies. Full semantics and
caveats: [bundle.md](bundle.md).

---

## `alle sync <file>`

Converge on a bundle as the **managed desired state** — the startup-sync
apply mode. This is what the Docker entrypoint runs on every container start;
it works the same on a host for a version-controlled, repeatedly-applied
setup file.

Same upsert rules as a merge import (ports, labels, fresh token resolution,
the `enabled` tri-state — an *unstated* `enabled` still preserves an ad-hoc
`alle channels disable`), plus **provenance**: everything sync creates is
marked as owned by the bundle, and each sync updates/prunes only that owned
state:

- **Idempotent** — syncing the same bundle again changes nothing (state stays
  byte-identical; rulesets never duplicate, unlike repeated `import`).
- **Edits update in place** — a changed managed ruleset is rewritten at its
  existing priority position; a changed channel updates in place.
- **Removals prune** — a channel/ruleset/provider dropped from the bundle is
  removed (a dropped provider's credential too). Channels and rulesets
  created *outside* sync (CLI, Web UI, `alle import`) are never pruned or
  adopted. A managed channel that a hand-made rule still references is kept
  and reported instead of breaking the rule.

`alle import` keeps its append/merge semantics; use `sync` when the file is
the single source of truth, `import` when layering a backup or template onto
an existing setup.

```bash
alle sync my-setup.yaml     # converge: idempotent, prunes what the file dropped
```

---

## `alle validate <file>`

Check a bundle **without applying it** — a pre-import dry run. Reports **every**
problem in one pass (never stopping at the first), each with the **line number**
and reason, and exits non-zero if any are found.

```bash
alle validate my-setup.yaml
alle validate my-setup.yaml && alle import my-setup.yaml   # gate an import
```

It runs the self-contained (`--replace`-style) checks:

- `kind: alle-bundle` and a `bundle_version` this alle understands.
- Providers are among the supported set (`nordvpn`, `protonvpn`).
- Token providers (NordVPN) carry a non-empty token; channel ids are unique
  within a provider; **country is required and checked against the provider's
  real country list, and city — if given — against that country's cities**
  (fetched or cached; skipped with a note if the provider API is unreachable).
- `wg` is optional for token providers (derived on apply) but **required for
  config providers** (Proton `.conf`); when present, its WireGuard fields are
  checked for presence and validity.
- If a `router` block is present, `killswitch` and `lan_direct` must be set
  explicitly; ruleset names are non-empty, each `target` is `direct`, `block`,
  or a `<provider>/<channel>` defined in the bundle, and every matcher type is
  one of the supported kinds (`domain_suffix`, `ip_cidr`, `all`; the legacy
  `domain` type is accepted and read as `domain_suffix`).

```text
$ alle validate broken.yaml
bundle rejected (3 problems) — nothing was changed:
  line 7   providers.nordvpn.credential — a non-empty token is required for NordVPN
  line 10  providers.nordvpn.channels.us_1.country — 'Atlantis' is not a known NordVPN country
  line 14  router.lan_direct — must be set explicitly to true or false
```

---

## `alle logs`

`alle logs [-f|--follow] [-n|--lines N]`

Show `alle`'s operation log (default last 200 lines). `-f` streams new lines.

```bash
alle logs -n 50
alle logs -f
```

---

## `alle ui`

Open the Web UI dashboard in your browser. Ensures the daemon (which serves the
UI) is running, then opens a one-time sign-in link.

```bash
alle ui
alle ui --no-open    # print the sign-in URL instead of opening a browser
```

The UI has a **Dashboard**, a **Bundle** page, and a **Logs** page. The
Dashboard shows the router entrypoint address, a channels table (Location,
Port, Latency, IP, and Sent / Received / Down Speed / Up Speed), and the
router rules. Measured columns stay blank until you run a per-row or
all-channel **Probe** or **Speed Test** (spinner while running). Adding a
channel opens a provider-guided wizard: an icon-only provider row plus an
always-present "+" to add NordVPN or Proton VPN;
token providers (NordVPN) pick a country and city from a searchable list, Proton
VPN uploads a WireGuard `.conf`. Router rules can be added, deleted, and
drag-reordered (first match wins), with an **Allow Non-VPN Traffic** toggle
(Unmatched row) and a fixed **Priority 0 / LAN** row keeping local traffic
direct. Channels a routing rule still targets can't be removed — the
UI shows the exact rules to clear first, same as `alle channels rm`. A **Bundle**
page downloads the setup as a bundle (`alle export`) and uploads one to **merge**
or **replace** the whole setup (replace confirms) — same as `alle import` /
`alle import --replace` — plus a **Validate** button to dry-run-check a file. A
**Logs** page polls the local log tail. Lifecycle (start/stop/restart) is via the CLI;
the masthead links to the project on GitHub.

- The server binds to `127.0.0.1` only and is never exposed to the network (there
  is no `--bind` option, by design). The browser URL uses a per-installation
  `alle-<random>.localhost` hostname (browsers resolve it to loopback
  themselves) so the session cookie is scoped to alle alone, never shared with
  other local web apps. Reach it remotely over an SSH tunnel on the **same**
  port: `ssh -L <port>:127.0.0.1:<port> user@host`, then open the `alle ui`
  sign-in link locally (the `<port>` is shown by `alle status` / `alle start`).
  SSH provides encryption and access control; do not expose or reverse-proxy
  the alle Web UI port directly.
- Auth: `alle ui` mints a single-use login token (exchanged for an `HttpOnly`
  session cookie); the persistent secret never appears in a URL, and `alle ui`
  only sends the sign-in link after the listener proves (via an HMAC health
  challenge) that it really is alle. Manual sign-in: paste the `secret` from
  `~/.alle/control_api.json`. Sessions idle out after 30 minutes without an
  open tab (12 h absolute cap); the masthead's **Sign out** revokes every
  session. The full threat model: [docs/security.md](security.md).

---

## `alle daemon`

Manage whether alle's background daemon runs as a **user-level login service**.
Advanced and optional — without it the runtime auto-starts on first use and runs
for the session. Installing the service makes it start at login and be
supervised (auto-restarted on crash, and on an in-place upgrade). No `sudo`: it
is a per-user service, never system-wide.

- **macOS** — a LaunchAgent (`~/Library/LaunchAgents/com.github.zydo.alle.plist`),
  managed with `launchctl`.
- **Linux** — a `systemd --user` unit (`~/.config/systemd/user/alle.service`),
  managed with `systemctl --user`.

Both auto-start at login and run for the login session. (A macOS LaunchAgent
cannot survive logout — that needs a root service, out of scope. On Linux,
`--linger` keeps it running after logout.)

### `alle daemon install [--linger]`

Register and start the login service. Pre-fetches the pinned sing-box binary so
the service starts ready. Idempotent — re-running refreshes the unit (e.g. after
a format change). `--linger` (Linux only) enables `loginctl enable-linger` so the
daemon keeps running after you log out.

A failed install rolls back: the previous unit (or no unit) is restored, and a
manually started daemon that was stopped for the handoff is brought back — you
never end up with less than you started with. If `--linger` itself fails, the
error says so explicitly; the service is installed and running at that point,
only logout survival is missing.

```bash
alle daemon install
alle daemon install --linger      # Linux: survive logout
```

Homebrew users don't need this — `brew services start alle` owns registration
there.

### `alle daemon uninstall`

Remove the login service. Your `~/.alle` state (providers, channels, keys) is
left untouched.

### `alle daemon status [--json]`

Show whether the login service is installed/active and whether the daemon is
running, with its version.

```text
Login service: active (launchd).
  Unit: /Users/you/Library/LaunchAgents/com.github.zydo.alle.plist
Daemon: running, version 0.1.3.
```

**Upgrades:** the service unit execs a stable shim, so `uv tool upgrade
alle-proxy` (or `pipx upgrade`) never needs to touch it, and a supervised daemon
notices the new version and restarts itself onto it within ~30s. For an
unsupervised daemon, `alle status` prints a one-line warning when the running
daemon is older than the CLI (`run alle restart to pick up the upgrade`).

---

## `alle helper`

`alle helper install` · `alle helper uninstall` · `alle helper status`

The privileged TUN helper — macOS only (Linux uses `setcap`, no helper). It is
the one-time grant that makes [`alle tun on`](#alle-tun-onoff) need no sudo:
install once, and the helper (a root LaunchDaemon) owns sing-box while tun mode
is on, so `tun on`/`off` and the Web UI toggle run as your normal user with no
password, across reboots.

```bash
sudo alle helper install     # one-time; then `alle tun on` needs no sudo
alle helper status           # is it installed and answering?
sudo alle helper uninstall   # remove it (tun on then needs the sudo fallback)
```

- `install`/`uninstall` need root (they write `/Library/LaunchDaemons/`); run
  them under `sudo`. `status` does not. The helper serves the user behind
  `sudo` (`SUDO_UID`) over a unix socket, authenticated by peer uid.
- One helper serves one `ALLE_HOME` — the one active at install time
  (`alle helper status` reports it as `serves_home`). Commands from a
  different home are refused; to move the helper to another home, rerun
  `sudo alle helper install` from that home.
- The helper is deliberately minimal — it only launches/stops/reloads sing-box
  against the fixed config path; it never parses state or sees credentials. See
  `docs/security.md` for the trust model.
- The signed, GUI-installed variant (SMAppService inside the `.app`) is deferred
  to Phase 8; this launchd helper is the no-signing steady state.

---

## `alle version`

Print the installed package version.

```bash
alle version
```

---

## Output conventions

- **Tables** share one style: a header row, a dashed separator, then rows — columns
  left-aligned and joined by two spaces. Channel tables all lead with the same
  `LABEL` and `ID` columns; `ID` is the globally-unique `provider/id` ref.
- **Location placeholders** (always under a column titled `CITY`, even when the value
  is a state/region):
  - `(Any City)` — a token-provider channel pinned to a country but no specific city.
  - `(Unknown)` — a config channel whose country/city couldn't be parsed from the file
    name.
- **`--json`** mirrors the structured data behind the human tables and keeps the raw
  lowercase provider key (plus a display name where relevant).

## Exit codes

- `0` — success.
- `1` — a user-correctable error (bad input, unknown provider/channel, rejected
  credential, missing config file, etc.); the message explains what to fix.
- `2` — argument/usage error (argparse); help is printed.
- `130` — interrupted (Ctrl-C).

## Environment variables

All optional; **every one unset = the behavior documented everywhere else in
this reference.** They exist for deployment profiles (the Docker image sets
several — see `docs/docker.md`) and for hermetic testing:

| Variable               | Effect                                                                                                                                                                                                                                                                                                         |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ALLE_HOME`            | State directory (default `~/.alle`).                                                                                                                                                                                                                                                                           |
| `ALLE_LISTEN`          | Bind address for channel + router proxy inbounds (default `127.0.0.1`). The container image sets `0.0.0.0`; invalid values are logged and ignored, never widened.                                                                                                                                              |
| `ALLE_PORT_BASE`       | Allocate new ports sequentially from this number instead of the OS ephemeral pool — deterministic, publishable ports. Declared `--port`/bundle `port:` values still win.                                                                                                                                       |
| `ALLE_SINGBOX`         | Path to a pre-provisioned sing-box binary. Still verified against the pinned SHA-256 on every start; a mismatch is a hard error, never a re-download.                                                                                                                                                          |
| `ALLE_CONTAINER`       | Marks the process as containerized (guardrails + hint text only — never changes binds, ports, or lifecycle by itself).                                                                                                                                                                                         |
| `ALLE_API_LISTEN`      | Bind address (`host[:port]`) for the control API/Web UI server (default: loopback on the minted `control_api.json` port). Non-loopback values expose the Bearer-authenticated REST API — see `docs/api.md`; the browser cookie path stays loopback-only. Invalid values are logged and ignored, never widened. |
| `ALLE_API_SECRET`      | Replaces the minted API secret with this value (min 16 chars) — for handing the same credential to compose siblings. Set for the daemon *and* local CLI use.                                                                                                                                                   |
| `ALLE_API_SECRET_FILE` | Same, read from a file (compose/k8s secrets). Exactly one of the two; setting both, an unreadable file, or a weak value makes the API refuse to start.                                                                                                                                                         |
| `ALLE_BUNDLE`          | Read by the container entrypoint only: the bundle path applied at boot (default `/etc/alle/bundle.yaml`).                                                                                                                                                                                                      |

(`ALLE_SERVICE` / `ALLE_APPLIER` are internal markers set by the login service,
the image, and `alle run` — not user knobs.)

## Files

Everything lives under `~/.alle/` unless `$ALLE_HOME` is set (handy for hermetic
testing: `ALLE_HOME=/tmp/alle-test alle status`):

- `state.json` — providers, channels, WireGuard params, ports, probe + reconnect
  state, and the router section (entrypoint port, kill-switch, routing rules)
  (`0600`).
- `credentials.yaml` — token-provider credentials (`0600`).
- `metrics.db` — SQLite store of per-channel cumulative traffic counters.
- `providers/*.json` — cached provider location lists.
- `singbox.json` — generated sing-box config (`0400`, read-only).
- `clash_api.json` — generated address + secret for the internal stats API (`0600`).
- `control_api.json` — generated address + secret for the Web UI control server
  (`0600`); the contract port the dashboard is served on.
- `bin/sing-box@<version>` — pinned, checksum-verified sing-box binary.
- `alle.log`, plus `*.pid` and `applier.info.json` (daemon pid + version, read by
  `alle status` for the skew warning) / runtime files while running.
