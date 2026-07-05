<p align="center">
  <img src="assets/wordmark.svg" alt="alle" width="320">
</p>

<p align="center">
  <a href="https://github.com/zydo/alle/actions/workflows/ci.yml"><img src="https://github.com/zydo/alle/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/alle-proxy/"><img src="https://img.shields.io/pypi/v/alle-proxy.svg" alt="PyPI"></a>
</p>

# alle

A universal VPN client that manages multiple VPN connections with rule-based routing.

# Why alle

Most VPN clients are built around one global idea: connect this device to a single
VPN server, then send everything through it until you disconnect or switch.

That is not enough when different resources need to appear from different regions
— a geo-fenced stream, a bank that blocks foreign IPs, a region-locked test
environment. Switching origins means disconnecting from one server and reconnecting
to another, and the official client on one machine usually cannot keep several
locations active at once anyway.

`alle` keeps multiple VPN exits live at the same time, from one provider or mixed
across several. Say you want a US exit, a UK exit, and a Japan exit at once —
NordVPN for the US and Japan, ProtonVPN for the UK:

```text
   streaming + admin   ──►  alle  ──►  United States   (NordVPN)
   test runner         ──►  alle  ──►  Japan           (NordVPN)
   bank login          ──►  alle  ──►  United Kingdom  (Proton VPN)
```

Each app points at the exit it needs; they run concurrently and independently, so
opening the bank never disturbs the stream.

In short: not one global location you keep switching, but several exits alive at
once, each used where it is needed.

## What alle does

`alle` runs multiple VPN exits side by side. Each exit is exposed as its own
local HTTP+SOCKS proxy on `127.0.0.1:<port>`. A single HTTP+SOCKS entry point
will route traffic by rule to a VPN exit or to direct outbound with no proxy.
Instead of changing your whole machine's VPN location, you point each app,
browser profile, script, or test job at the path it needs.

Under the hood, `alle` manages one
[`sing-box`](https://github.com/SagerNet/sing-box) process. Each channel becomes
one local proxy inbound routed through one WireGuard VPN peer. Channels can come
from different providers, so a NordVPN exit and a Proton VPN `.conf` import can
run at the same time.

## Current status

`alle` is usable today as a CLI-first client for per-app/per-workflow VPN exits.

**Providers**

| Provider   | Support                                                                   |
| ---------- | ------------------------------------------------------------------------- |
| NordVPN    | Token/API setup, location selection, automatic WireGuard channel creation |
| Proton VPN | WireGuard `.conf` import                                                  |

**Platforms**

| Platform | Support   |
| -------- | --------- |
| macOS    | Supported |
| Linux    | Supported |
| Windows  | Planned   |

**Features**

| Phase             | Status                                                                 |
| ----------------- | ---------------------------------------------------------------------- |
| Core CLI          | Providers, channels, per-channel proxies, status, tests, logs, metrics |
| Routing           | Planned                                                                |
| Web UI            | Planned                                                                |
| Desktop companion | Planned                                                                |
| Distribution      | PyPI CLI package; native installers planned                            |

## Install

`alle` requires Python 3.10 or newer.

With `pip`:

```bash
python -m pip install alle-proxy
```

With `pipx`:

```bash
pipx install alle-proxy
```

With `uv` as an installed tool:

```bash
uv tool install alle-proxy
```

Or run it directly with `uvx`:

```bash
uvx --from alle-proxy alle --help
```

After installation:

```bash
alle version
alle --help
```

## Quick start

Add a provider, create a channel, start the runtime, then use the channel's local
proxy port.

```bash
alle providers add nordvpn
alle channels add nordvpn --country "United States"
alle start
alle channels ls
```

`alle channels ls` prints the local proxy port for each channel:

```text
PROVIDER  NAME             PORT    COUNTRY        CITY
--------  ---------------  ------  -------------  ----------
NordVPN   united_states_1  :53124  United States  (Any City)
```

Use that port from any tool or app that supports an HTTP or SOCKS proxy:

```bash
curl -x http://127.0.0.1:53124 https://api.ipify.org
```

Check health and traffic:

```bash
alle status
alle test
alle metrics
```

**Provider setup**

`alle` supports two provider setup styles today:

**NordVPN** uses an access token:

```bash
alle providers add nordvpn
alle locations nordvpn
alle locations nordvpn --country "United States"
alle channels add nordvpn --country "United States" --city "Seattle"
```

**Proton VPN** uses WireGuard config files downloaded from Proton:

```bash
alle providers add protonvpn
alle channels add protonvpn --config ~/Downloads/wg-US-CA-842.conf
```

Re-importing the same `.conf` file updates that channel in place, keeping the
same channel id and local port.

**Common commands**

Useful commands after setup:

```bash
alle providers ls
alle channels ls
alle channels ls --refs
alle status
alle test
alle metrics
alle logs
alle stop
```

Most read commands support `--json` for scripts:

```bash
alle status --json
alle channels ls --json
alle metrics --json
```

Channel and provider removals accept multiple targets:

```bash
alle channels rm japan_1 united_states_seattle_1
alle channels rm protonvpn/wg_us_ca_842
alle channels rm 'united_states_*' --dry-run
alle providers rm nordvpn protonvpn -y
```

For the complete command reference, see the
[CLI Reference](docs/cli-reference.md).

## Rule-based routing

To be implemented.

## How it works

- `alle` keeps its local state under `~/.alle/`, or under `$ALLE_HOME` when that
  environment variable is set. This includes providers, channels, credentials,
  metrics, generated config, logs, and runtime files.

- `alle` manages one [`sing-box`](https://github.com/SagerNet/sing-box) process
  instead of starting one VPN process per channel. The generated config contains
  one local HTTP+SOCKS inbound per channel.

- Each channel routes to one WireGuard peer. NordVPN channels are created from
  the provider API; Proton VPN channels are created by importing a WireGuard
  `.conf` file. After creation, both behave the same way.

- WireGuard is connectionless, so `alle` does not model channels as connected or
  disconnected. A channel exists in config; its health comes from the latest
  probe.

- Local proxy ports are assigned by the OS and stored in state. Use
  `alle channels ls` to see the current ports.

- The background runtime applies state changes, keeps the `sing-box` process in
  sync, probes channel health, and records per-channel traffic totals.

- `alle` uses a pinned upstream `sing-box` release and verifies its checksum
  before running it.

## Security and privacy

- Provider credentials and WireGuard private keys are stored locally under
  `~/.alle/` or `$ALLE_HOME`.
- The state directory is kept owner-only (`0700`), and credential/state/config
  files inside it are written with private permissions from the first byte.
- `alle` does not read provider tokens from environment variables; credentials
  are added explicitly with `alle providers add`.
- `alle` downloads a pinned upstream `sing-box` release and verifies its checksum
  before running it.
- Local proxy ports bind to loopback. Traffic only uses a VPN exit when an app is
  pointed at one of those proxies.
- The loopback proxies are unauthenticated: on a multi-user machine, any local
  user or process can send traffic through your channels (and your provider
  account). alle assumes a single-user machine; don't run it where that
  assumption fails. The internal stats API *is* authenticated with a generated
  per-installation secret, so connection metadata is not exposed locally.

## Roadmap and non-goals

Planned next steps:

- Rule-based routing through a single local HTTP+SOCKS entry point.
- More WireGuard-capable VPN providers. See
  [VPN Provider Research](docs/vpn-provider-research.md).
- Web UI for managing channels and routing rules.
- Desktop companion with OS-level VPN integration.
- Windows support and broader distribution.

Non-goals:

- OpenVPN or IKEv2/IPsec support.
- VPN providers without usable WireGuard support, such as ExpressVPN, HideMyAss,
  Perfect Privacy, Privado, SlickVPN, VPN.ac/VPNSecure, and Giganews.
- SOCKS5-only or unencrypted proxy providers.
- Bundling `sing-box` inside the Python package.

## License

MIT
