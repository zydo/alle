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
    - [`alle providers rm <provider>... [-y|--yes]`](#alle-providers-rm-provider--y--yes)
  - [`alle channels`](#alle-channels)
    - [`alle channels add <provider> …`](#alle-channels-add-provider-)
    - [`alle channels ls [--json|--ids|--refs]`](#alle-channels-ls---json--ids--refs)
    - [`alle channels setlabel <channel> [label]`](#alle-channels-setlabel-channel-label)
    - [`alle channels rm <channel>...`](#alle-channels-rm-channel)
  - [`alle routes`](#alle-routes)
    - [`alle routes add <target> --<matcher>`](#alle-routes-add-target---matcher)
    - [`alle routes ls [--channel <ref>] [--json]`](#alle-routes-ls---channel-ref---json)
    - [`alle routes rm <id>...`](#alle-routes-rm-id)
    - [`alle routes killswitch [on|off]`](#alle-routes-killswitch-onoff)
  - [`alle locations`](#alle-locations)
  - [`alle status`](#alle-status)
  - [`alle start` / `stop` / `restart`](#alle-start--stop--restart)
  - [`alle test`](#alle-test)
  - [`alle metrics`](#alle-metrics)
  - [`alle logs`](#alle-logs)
  - [`alle daemon`](#alle-daemon)
  - [`alle version`](#alle-version)
  - [Output conventions](#output-conventions)
  - [Exit codes](#exit-codes)
  - [Files](#files)

## Conventions

- **Help** — run `alle`, `alle <group>`, or any command with `-h/--help` to see usage.
  A group or command invoked with no action prints its help instead of erroring.
- **`--json`** — read commands (`providers ls`, `channels ls`, `routes ls`,
  `locations`, `status`, `test`, `metrics`) accept `--json` for a stable,
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

Add a provider.

- **Token providers** (`nordvpn`): prompts for the credential (input hidden, shown as
  `*`), validates it against the provider API, and stores it in `credentials.yaml`.
- **Config providers** (`protonvpn`): just registers the provider — no credential —
  and prints how to import a `.conf`.

```bash
alle providers add nordvpn        # prompts for an access token
alle providers add protonvpn      # registers; import channels with --config
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

### `alle providers rm <provider>... [-y|--yes]`

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
- Country/city are parsed best-effort from the file name's ISO codes (ProtonVPN's
  `wg-<CC>-<SUB>-<n>` convention, e.g. `US`/`CA` → United States / California). Only
  the country code is reliable; a missing/unknown subdivision shows as `(Unknown)`.
  `alle` never geo-locates the endpoint to guess.

Both forms accept `--label "<text>"` to give the channel a friendly display name
(see [`alle channels setlabel`](#alle-channels-setlabel-channel-label)):

```bash
alle channels add nordvpn --country "United States" --label "Streaming - US"
```

### `alle channels ls [--json|--ids|--refs]`

List configured channels (static config only — no live status). Columns: `LABEL`,
`ID`, `PORT`, `COUNTRY`, `CITY`. `LABEL` is the friendly display name (falls back
to the id when unset); `ID` is the globally-unique, provider-qualified handle
(`nordvpn/japan_1`) — the same ref every command accepts, which is why no separate
provider column is needed.

```text
LABEL           ID                        PORT    COUNTRY        CITY
--------------  ------------------------  ------  -------------  ----------
Streaming - US  nordvpn/japan_1           :53124  Japan          (Any City)
seattle_1       nordvpn/seattle_1         :53125  United States  Seattle
wg_us_ca_842    protonvpn/wg_us_ca_842    :53126  United States  California
```

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
channel). Every channel table (`channels ls`, `status`, `test`, `metrics`) shows
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

---

## `alle routes`

Rule-based routing through the router entrypoint. Rules are evaluated **in the
order they were added — first match wins**; there is no reordering command yet
(delete and re-add to change order). Traffic that matches no rule goes
**direct (no VPN)** unless the [kill-switch](#alle-routes-killswitch-onoff) is on.

The entrypoint's port is shown by `alle status` and by `routes add`/`routes ls`;
it is allocated on the first daemon start and then never changes.

### `alle routes add <target> --<matcher>`

Append one rule. `<target>` is where matched traffic exits:

- `<provider>/<channel>` — a channel (must exist; see `alle channels ls --refs`),
- `direct` — straight to the network, no VPN,
- `block` — refuse the connection.

Exactly one matcher per rule:

| Matcher               | Matches                                                    |
| --------------------- | ---------------------------------------------------------- |
| `--domain <d>`        | exactly `d`                                                |
| `--domain-suffix <d>` | `d` and all its subdomains (dot-boundary)                  |
| `--cidr <net>`        | destination IP in `net` (a bare IP means that one address) |
| `--all`               | everything — the catch-all for "VPN by default"            |

```bash
alle routes add nordvpn/united_states_1 --domain-suffix netflix.com
alle routes add direct --cidr 192.168.0.0/16
alle routes add block --domain tracker.example.com
alle routes add nordvpn/japan_1 --all
```

Notes:

- Domain rules work for HTTPS on any port (destinations are sniffed via SNI when
  the client sends an IP or uses SOCKS without a hostname).
- `--cidr` matches IP-literal destinations; domain destinations are **not**
  resolved for CIDR matching.
- If an earlier rule already covers the new one (e.g. `--domain-suffix
  google.com` before `--domain api.google.com`), `routes add` warns that the new
  rule is **shadowed** and will never match.

### `alle routes ls [--channel <ref>] [--json]`

List rules in evaluation order: `ID`, `MATCH`, `TARGET`, and a `NOTE` marking
shadowed rules. The header line shows the entrypoint address and its unmatched
behavior. `--channel <name|provider/name>` filters to rules targeting one
channel — useful before removing it.

```text
Router entrypoint 127.0.0.1:54585 — 3 rule(s), unmatched → direct
ID  MATCH                        TARGET                   NOTE
--  ---------------------------  -----------------------  ----------------------------
r1  domain_suffix netflix.com    nordvpn/united_states_1
r2  domain api.netflix.com       direct                   shadowed by r1 — never matches
r3  ip_cidr 192.168.0.0/16       direct
```

### `alle routes rm <id>...`

Remove rules by id (`--dry-run` to preview). Unknown ids are reported all at
once and nothing is removed.

```bash
alle routes rm r2
alle routes rm r1 r3 --dry-run
```

### `alle routes killswitch [on|off]`

Block router traffic that matches no rule, instead of letting it go direct.
Run without an argument to show the current state.

```bash
alle routes killswitch on
alle routes killswitch
```

- Applies to the **router entrypoint only** — per-channel ports are unaffected.
  (Commercial VPN apps use "kill switch" for a system-wide block; alle's becomes
  system-wide only once your system points at the router.)
- `alle status` and `routes ls` show `unmatched → block — kill-switch ON` while
  active, so it's always visible why unmatched traffic fails.

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

Show whether `alle` is running, the router entrypoint's address and mode, plus a
per-channel health table from the latest probes: `LABEL`, `ID`, `PORT`,
`COUNTRY`, `CITY`, `STATE`, `AGO` (probe age), `LATENCY` (latency), `IP` (exit
IP). Every channel table shares the same `LABEL` + `ID` lead — `LABEL` is the
display name (the id when no label is set), `ID` is the provider-qualified ref
commands take.

```text
Alle - Active
  Router  127.0.0.1:54585 — 2 rule(s), unmatched → direct
LABEL           ID                      PORT    COUNTRY        CITY        STATE   AGO      LATENCY  IP
--------------  ----------------------  ------  -------------  ----------  ------  -------  -------  ---------------
Test runner     nordvpn/japan_1         :53124  Japan          (Any City)  Active  19s ago  398ms    93.118.43.151
wg_us_ca_842    protonvpn/wg_us_ca_842  :53126  United States  California  Active  19s ago  87ms     185.98.169.31
```

The router line always states the entrypoint's behavior — `pass-through (no
rules)`, `N rule(s), unmatched → direct`, or `… → block — kill-switch ON` — so
it is never ambiguous whether unmatched traffic is inside a VPN (it is not).

`STATE` is `Active`, `Pending`, a probe error, or a reconnect state
(`Reconnecting (N)` / `Reconnect failed`) when auto-reconnect is at work.

---

## `alle start` / `stop` / `restart`

- **`alle start`** — start the runtime (background reconciler + sing-box). Runs idle if
  no channels are configured yet.
- **`alle stop`** — stop the runtime. Channels stay in config; only the processes stop.
- **`alle restart`** — stop then start. Also clears any `Reconnect failed` flags so
  dead channels are retried from scratch.

```bash
alle start
alle restart
alle stop
```

---

## `alle test`

`alle test [--channel <id>] [--speed] [--json]`

Probe channels **now** (rather than waiting for the next background cycle) and print a
quick connectivity table: `LABEL`, `ID`, `PORT`, `COUNTRY`, `CITY`, `STATE`,
`LATENCY` (probe latency), and `IP` (exit IP). With `--channel`, test just one channel by id.
`STATE` is `Healthy`, or the failure reason (`Stopped` while the runtime is down, otherwise
the probe error) — the same convention as [`alle status`](#alle-status).

```text
LABEL           ID                      PORT    COUNTRY        CITY        STATE     LATENCY  IP
--------------  ----------------------  ------  -------------  ----------  --------  -------  ---------------
Test runner     nordvpn/japan_1         :53124  Japan          (Any City)  Healthy   398.0ms  93.118.43.151
wg_us_ca_842    protonvpn/wg_us_ca_842  :53126  United States  California  Timeout   -        -
```

Add `--speed` to run the slower download/upload test after the fresh connectivity
probe. Speed tests run only for channels that were healthy in that same probe;
unhealthy channels are shown with skipped speed columns.

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

---

## `alle metrics`

`alle metrics [<channel>] [--json]`

Per-channel **cumulative** traffic totals (sent/received/total) since counters began,
plus when traffic was last seen. Columns: `LABEL`, `ID`, `PORT`, `COUNTRY`,
`CITY`, `SENT`, `RECV`, `TOTAL`, `SEEN`. Optionally filter to one channel by id.

```text
LABEL           ID                      PORT    COUNTRY        CITY        SENT      RECV      TOTAL     SEEN
--------------  ----------------------  ------  -------------  ----------  --------  --------  --------  -------
Test runner     nordvpn/japan_1         :53124  Japan          (Any City)  104.0 MB  396.7 MB  500.7 MB  3s ago
wg_us_ca_842    protonvpn/wg_us_ca_842  :53126  United States  California  28.1 MB   141.8 MB  169.9 MB  1m ago
```

```bash
alle metrics
alle metrics japan_1
alle metrics --json
```

**Accuracy note:** totals are sampled from live connections a few times a minute.
Transfers that open and finish entirely between two samples are under-counted — this
is inherent to the data source, not a bug. Use `metrics` for cumulative usage trends,
not exact accounting. (`--json` also includes `total_sent` / `total_received`
aggregates.)

---

## `alle logs`

`alle logs [-f|--follow] [-n|--lines N]`

Show `alle`'s operation log (default last 200 lines). `-f` streams new lines.

```bash
alle logs -n 50
alle logs -f
```

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
Daemon: running, version 0.1.0.
```

**Upgrades:** the service unit execs a stable shim, so `uv tool upgrade
alle-proxy` (or `pipx upgrade`) never needs to touch it, and a supervised daemon
notices the new version and restarts itself onto it within ~30s. For an
unsupervised daemon, `alle status` prints a one-line warning when the running
daemon is older than the CLI (`run alle restart to pick up the upgrade`).

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
- `bin/sing-box@<version>` — pinned, checksum-verified sing-box binary.
- `alle.log`, plus `*.pid` and `applier.info.json` (daemon pid + version, read by
  `alle status` for the skew warning) / runtime files while running.
