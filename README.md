<p align="center">
  <img src="assets/wordmark.svg" alt="alle" width="320">
</p>

# alle

**alle is an unofficial, universal VPN client.** It lets you run multiple VPN
locations side by side, each exposed as a local HTTP/SOCKS proxy port, all backed
by one local sing-box process.

```text
127.0.0.1:8888  ->  sing-box  ->  VPN exit in the US
127.0.0.1:8889  ->  sing-box  ->  VPN exit in the UK
```

alle has three planned user surfaces, each owning a distinct capability tier:

- **CLI** — the primary, complete surface. Channel management, rule-based routing, and
  per-channel metrics. Channels are accessible as individual proxy ports; an optional
  router entrypoint aggregates them behind configurable rules. No OS-level VPN.
- **Web UI** — same channel/routing/metrics capabilities as the CLI, with a visual
  routing rule editor. Does not set up an OS-level VPN profile.
- **Desktop companion** (macOS, Windows, Linux) — channel management plus **OS-level
  VPN**: configures a system network profile so all system traffic routes through
  sing-box without per-app proxy setup.

All three surfaces are clients of the same core engine; none owns separate state.

## Current status

- Functional provider: **NordVPN** (token/API archetype, end-to-end).
- In progress: **ProtonVPN** `.conf` import (config/portal archetype).
- Additional providers (Mullvad, IVPN, PIA, …) are post-MVP.
- Runtime: one pinned, checksum-verified upstream `sing-box` binary, downloaded on
  demand into `~/.alle/bin/`.
- Platform scope today: macOS and Linux on mainstream amd64/arm64. Windows support
  is planned but not claimed yet.
- No Docker, no OpenVPN/IKEv2, no unencrypted SOCKS5 provider mode.

## Install and run

```bash
# run without installing
uvx alle --help

# install as a tool
uv tool install alle

# from a checkout
uv sync
uv run alle --help
```

Base installs are CLI-only. The optional tray skeleton is intentionally separate:

```bash
pip install "alle[tray]"
# or from a checkout:
uv run alle-tray
```

## Concepts

- **Provider** — a VPN service account added to alle, such as `nordvpn`.
- **Channel** — one VPN location under a provider, exposed as a local proxy on
  `127.0.0.1:<port>`. The CLI and Web UI expose each channel as its own port for
  explicit proxy use.
- **Router entrypoint** *(planned, Phase 2)* — a single local port that routes traffic
  to the right channel via configurable rules (domain, CIDR, etc.). Individual channel
  ports remain accessible alongside the router. Managed via `alle router` in the CLI.
- **Metrics** *(planned, Phase 1 completion)* — per-channel cumulative sent/received
  byte totals, accessible via `alle metrics [<channel>] [--json]`.
- **OS-level VPN** *(Desktop companion, planned)* — a system network profile that
  routes all system traffic into sing-box without per-app proxy configuration.
- **alled** — the local background daemon that reconciles state into sing-box config
  and heartbeat-probes channels. The `alled` console script runs the daemon body; the
  hidden `alle applier` command remains for compatibility.

WireGuard is connectionless, so alle does not model channels as connected or
disconnected. A channel exists in config; its displayed health comes from the most
recent background probe.

## CLI reference

All examples below may be run as `uv run alle ...` from a checkout.

```bash
# providers
alle providers add <name>
alle providers ls [--json]
alle providers rm <name> [-y]

# channels
alle channels add <name> --country "United States" [--city "San Francisco"]
alle channels add <name> --config /path/to/wireguard.conf   # stub; not yet functional
alle channels ls [--json]
alle channels rm <name> --channel <channel_name>

# locations
alle locations <name> [--refresh] [--json]
alle locations <name> --country "United States" [--json]

# lifecycle and inspection
alle status [--json]
alle start
alle stop
alle restart
alle test
alle logs [-f] [-n N]
```

Human text is for terminals. `--json` on read commands is the stable
machine-readable surface for scripts and future clients.

## Quick start

```bash
alle providers add nordvpn
alle channels add nordvpn --country "United States" --city "San Francisco"
alle start
alle status

curl -x http://127.0.0.1:8888 https://ifconfig.me
alle test
alle stop
```

Adding or removing providers/channels updates `state.json`; the daemon applies the
change automatically. `start`, `stop`, and `restart` control the local runtime.

## Providers

**NordVPN** and **ProtonVPN** are the two MVP targets across all development phases —
not just an initial step. They represent the two archetypes that cover the
implementation space of nearly every commercial VPN provider:

| Provider | Archetype | Status |
| --- | --- | --- |
| NordVPN | Token/API — credential + automatic server resolution | implemented |
| ProtonVPN | Config — import a WireGuard `.conf` downloaded from the portal | in progress |

Additional providers (Mullvad, IVPN, PIA, …) are **post-MVP**. Each is a variant of
one of the two archetypes and can be added in parallel or after the MVP is complete.

Token providers prompt for credentials and store them in `credentials.yaml`.
Config providers require no credential — you download a `.conf` from the provider
portal and pass it to `alle channels add <provider> --config <file>`.

## State files

Everything lives under `~/.alle/` unless `$ALLE_HOME` is set:

- `state.json` — providers, channels, WireGuard parameters, ports, latest probe
  results; written `0600`.
- `credentials.yaml` — provider credentials; written `0600`.
- `providers/*.json` — cached provider location lists.
- `singbox.json` — generated sing-box config; written read-only (`0400`).
- `bin/sing-box@<version>` — pinned, checksum-verified sing-box binary.
- `alle.log` — alle operation log.
- `applier.pid` / `singbox.pid` and related runtime files while running.

Use `ALLE_HOME=/tmp/alle-test uv run alle ...` for hermetic manual testing.

## Architecture

```text
alle.service  (shared application operations)
    |
    +-- state.Store / credentials
    +-- providers / locations
    +-- engine.Engine
    +-- daemon lifecycle
    +-- singbox.Runner

alle.cli      -> parses args, prompts, renders text/JSON   [channels + routing + metrics]
alle-tray     -> optional PySide6 client skeleton
alled         -> persistent local service (+ future HTTP control API)
Web UI        -> planned: visual UI over same core (channels, routing rules, metrics)
Desktop companion -> planned: channel management + OS-level VPN setup
```

The business layer does not print, prompt, call `sys.exit()`, or scrape CLI text.
All surfaces are clients of the same core; none should accumulate business logic.

## Roadmap

- **Phase 1 (in progress):** Core + CLI fully functional — implement ProtonVPN `.conf`
  import and add `alle metrics` per-channel bandwidth tracking to complete this phase.
- **Phase 2:** Routing in core + CLI — `alle router` commands, rule-based entrypoint
  port, individual channel ports remain accessible.
- **Phase 3:** User-level system daemon — `alle daemon install/uninstall` for macOS
  (LaunchAgent) and Linux (`systemd --user`); sing-box pre-downloaded at install time.
- **Phase 4:** Web UI — visual channel management and routing rule editor; no OS VPN.
- **Phase 5:** macOS desktop companion — menu-bar + OS-level VPN profile.
- **Phase 6:** Windows desktop companion — system tray + OS VPN.
- **Phase 7:** Linux desktop companion — tray/fallback window + OS VPN.
- **Phase 8:** Distribution — PyPI (CLI-only), Homebrew (CLI + desktop extras), GitHub
  Releases (pre-built binaries with sing-box bundled); CI and publish workflows
  automated via GitHub Actions.

## Development

```bash
uv run ruff check
uv run pytest -q
ALLE_HOME="$(mktemp -d)" uv run alle status
```

During feature work, test the core and CLI by default. GUI/Web UI testing belongs to
GUI/Web UI tasks.

## License

MIT; see [LICENSE](LICENSE). alle downloads and runs the unmodified upstream
`sing-box` binary as a separate process; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
