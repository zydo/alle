# CLI reference

Complete reference for the `alle` command-line interface — the primary, complete
surface for managing providers, channels, and the local runtime.

From a checkout, prefix everything with `uv run` (e.g. `uv run alle status`). An
installed build (`uv tool install alle-proxy` / `uvx --from alle-proxy alle`)
exposes `alle` directly. The examples below omit the prefix.

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
    - [`alle channels rm <channel>...`](#alle-channels-rm-channel)
  - [`alle locations`](#alle-locations)
  - [`alle status`](#alle-status)
  - [`alle start` / `stop` / `restart`](#alle-start--stop--restart)
  - [`alle test`](#alle-test)
  - [`alle metrics`](#alle-metrics)
  - [`alle logs`](#alle-logs)
  - [`alle version`](#alle-version)
  - [Output conventions](#output-conventions)
  - [Exit codes](#exit-codes)
  - [Files](#files)

## Conventions

- **Help** — run `alle`, `alle <group>`, or any command with `-h/--help` to see usage.
  A group or command invoked with no action prints its help instead of erroring.
- **`--json`** — read commands (`providers ls`, `channels ls`, `locations`, `status`,
  `test`, `metrics`) accept `--json` for a stable, machine-readable projection of
  the same data. This is the scripting/cross-language interface (pipe to `jq`, etc.).
  It is **not** the programmatic API for `alle`'s own components — those call the core
  (`alle.service`) directly rather than shelling out. Human table output is for
  terminals and may change; `--json` shape is stable.
- **No separate apply step** — adding or removing channels writes
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
- **Runtime** — the background process managed by `alle start`/`stop`/`restart` that
  reconciles `state.json` into one sing-box process and heartbeat-probes each channel.
  It is auto-started on the first mutation or `alle start`; there is no separate
  user-facing daemon command in the current CLI.

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

### `alle channels ls [--json|--ids|--refs]`

List configured channels (static config only — no live status). Columns: `PROVIDER`,
`NAME`, `PORT`, `COUNTRY`, `CITY`.

```text
PROVIDER    NAME                     PORT    COUNTRY        CITY
----------  -----------------------  ------  -------------  ----------
NordVPN     japan_1                  :53124  Japan          (Any City)
NordVPN     united_states_seattle_1  :53125  United States  Seattle
Proton VPN  wg_us_ca_842             :53126  United States  California
```

For scripting, print just channel names or provider-qualified refs:

```bash
alle channels ls --ids
alle channels ls --refs
```

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

Show whether `alle` is running plus a per-channel health table from the latest probes:
`PROVIDER`, `NAME`, `PORT`, `COUNTRY`, `CITY`, `STATE`, `AGO` (probe age), `LATENCY`
(latency), `IP` (exit IP).

```text
Alle - Active
PROVIDER    NAME                     PORT    COUNTRY        CITY        STATE   AGO      LATENCY  IP
----------  -----------------------  ------  -------------  ----------  ------  -------  -----  ---------------
NordVPN     japan_1                  :53124  Japan          (Any City)  Active  19s ago  398ms  93.118.43.151
Proton VPN  wg_us_ca_842             :53126  United States  California  Active  19s ago  87ms   185.98.169.31
```

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

`alle test [--channel <name>] [--speed] [--json]`

Probe channels **now** (rather than waiting for the next background cycle) and print a
quick connectivity table: `PROVIDER`, `NAME`, `PORT`, `COUNTRY`, `CITY`, `STATE`,
`LATENCY` (probe latency), and `EXIT IP`. With `--channel`, test just one channel by name.
`STATE` is `Healthy`, or the failure reason (`Stopped` while the runtime is down, otherwise
the probe error) — the same convention as [`alle status`](#alle-status).

```text
PROVIDER    NAME                     PORT    COUNTRY        CITY        STATE     LATENCY  EXIT IP
----------  -----------------------  ------  -------------  ----------  --------  -------  ---------------
NordVPN     japan_1                  :53124  Japan          (Any City)  Healthy   398.0ms  93.118.43.151
Proton VPN  wg_us_ca_842             :53126  United States  California  Timeout   -        -
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
plus when traffic was last seen. Optionally filter to one channel by name.

```text
PROVIDER    NAME                     PORT    COUNTRY        CITY        SENT      RECV      TOTAL     SEEN
----------  -----------------------  ------  -------------  ----------  --------  --------  --------  -------
NordVPN     japan_1                  :53124  Japan          (Any City)  104.0 MB  396.7 MB  500.7 MB  3s ago
Proton VPN  wg_us_ca_842             :53126  United States  California  28.1 MB   141.8 MB  169.9 MB  1m ago
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

## `alle version`

Print the installed package version.

```bash
alle version
```

---

## Output conventions

- **Tables** share one style: a header row, a dashed separator, then rows — columns
  left-aligned and joined by two spaces, with a `PROVIDER` column carrying the brand
  name.
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
  state (`0600`).
- `credentials.yaml` — token-provider credentials (`0600`).
- `metrics.db` — SQLite store of per-channel cumulative traffic counters.
- `providers/*.json` — cached provider location lists.
- `singbox.json` — generated sing-box config (`0400`, read-only).
- `bin/sing-box@<version>` — pinned, checksum-verified sing-box binary.
- `alle.log`, plus `*.pid` / runtime files while running.
