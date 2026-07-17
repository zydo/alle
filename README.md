<p align="center">
  <img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/wordmark.svg" alt="alle" width="320">
</p>

<p align="center">
  <a href="https://github.com/zydo/alle/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/zydo/alle/ci.yml?branch=main&amp;label=CI" alt="CI"></a>
  <a href="https://pypi.org/project/alle-proxy/"><img src="https://img.shields.io/pypi/v/alle-proxy.svg?label=PyPI" alt="PyPI"></a>
  <a href="https://hub.docker.com/r/ziyudo/alle"><img src="https://img.shields.io/docker/v/ziyudo/alle?sort=semver&amp;label=Docker" alt="Docker"></a>
</p>

# alle

A universal VPN client that manages multiple VPN connections with rule-based routing.

<p align="center">
  <img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/webui.png" alt="alle Web UI dashboard" width="900">
</p>

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
local HTTP+SOCKS proxy on `127.0.0.1:<port>`. A single HTTP+SOCKS router
entrypoint routes traffic by rule (domain, IP) to a VPN exit, to direct
outbound, or blocks it. Instead of changing your whole machine's VPN location,
you point each app, browser profile, script, or test job at the path it needs.

Under the hood, `alle` manages one
[`sing-box`](https://github.com/SagerNet/sing-box) process. Each channel becomes
one local proxy inbound routed through one WireGuard VPN peer. Channels can come
from different providers, so a NordVPN exit and a Proton VPN `.conf` import can
run at the same time.

For a whole-machine VPN that captures *all* traffic through the same routing
rules, there is an optional **TUN mode** (`alle tun on`, one-time privilege
grant) — see the [CLI reference](docs/cli-reference.md#alle-tun-onoff) and
the [runbook](docs/tun-runbook.md).

## Current status

`alle` is usable today as a CLI-first client for per-app/per-workflow VPN exits.

**Providers**

| Provider   | Support                                                                   |
| ---------- | ------------------------------------------------------------------------- |
| NordVPN    | Token/API setup, location selection, automatic WireGuard channel creation |
| Proton VPN | WireGuard `.conf` import                                                  |

**Platforms**

| Platform | Support                                                           |
| -------- | ----------------------------------------------------------------- |
| macOS    | Supported                                                         |
| Windows  | Planned                                                           |
| Linux    | Supported                                                         |
| Docker   | Supported — [`ziyudo/alle`](https://hub.docker.com/r/ziyudo/alle) |

**Features**

| Area              | Status                                                                                                           |
| ----------------- | ---------------------------------------------------------------------------------------------------------------- |
| Core CLI          | Providers, channels, per-channel proxies, status, tests (probe + speed + traffic), logs                          |
| Routing           | Ruleset-based router entrypoint with domain/CIDR/all matchers, kill-switch, CLI shadow lint, built-in LAN bypass |
| Web UI            | Dashboard (channels, probe/speed, routes, kill-switch) + Bundle + Logs pages                                     |
| REST API          | Everything the CLI does over `/api/v1` (Bearer auth) — for scripts and compose siblings                          |
| Docker            | Container profile: proxy hub for compose networks, VPN gateway container (tun), declarative boot config          |
| Desktop companion | Planned                                                                                                          |
| Distribution      | PyPI CLI package and Docker Hub image; native installers planned                                                 |

## Install or deploy

| Choice              | Supervisor                 | Traffic captured                          | Host-wide VPN |
| ------------------- | -------------------------- | ----------------------------------------- | ------------- |
| `uv` host install   | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| `pipx` host install | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| Docker proxy hub    | Docker restart policy      | Proxy-aware containers/apps               | No            |
| Docker gateway      | Docker restart policy      | alle netns + explicitly joined containers | No            |

```bash
# Host, with uv
uv tool install alle-proxy

# Host, with pipx
pipx install alle-proxy

# Docker proxy hub (persistent state; no proxy/API ports published)
docker run -d --name alle --restart unless-stopped \
  --mount type=volume,src=alle-state,dst=/var/lib/alle \
  ziyudo/alle:latest
docker exec alle alle status
```

Host installs may add `alle daemon install` to start at login. Containers use
`alle run` as PID 1 and Docker's restart policy instead; those lifecycle
commands are intentionally inapplicable there. See [Getting started](docs/getting-started.md)
for all three choices and [Docker](docs/docker.md) for bundles and gateway scope.

## Quick start

```bash
alle providers add nordvpn
alle channels add nordvpn --country "United States"
alle start
alle channels ls                # prints each channel's local proxy port
```

Point anything proxy-aware at a channel's port:

```bash
curl -x http://127.0.0.1:53124 https://api.ipify.org
```

The full walkthrough — provider setup styles (token vs `.conf` import),
labels, everyday commands, holding more channels than your plan's connection
cap — is in **[Getting started](docs/getting-started.md)**.

## Documentation

**Using alle**

- **[Getting started](docs/getting-started.md)** — install, quick start,
  provider setup, everyday commands, channel enable/disable.
- **[Rule-based routing](docs/routing.md)** — the router entrypoint: rulesets,
  first-match priority, kill-switch, built-in LAN bypass.
- **[Web UI](docs/web-ui.md)** — the browser dashboard (`alle ui`): pages,
  sign-in, remote access over SSH.
- **[CLI reference](docs/cli-reference.md)** — every command, flag, and
  environment variable.

**Automating alle**

- **[REST API](docs/api.md)** — the `/api/v1` contract: everything the CLI can
  do, over HTTP with Bearer auth. Loopback by default; opt-in network exposure
  for compose siblings. Machine-readable spec:
  [openapi.yaml](docs/openapi.yaml).
- **[Declarative setup](docs/declarative-config.md)** and the
  **[bundle format](docs/bundle.md)** — the whole setup (providers, channels,
  rules) as one YAML file: backup/restore, startup config, secret indirection.

**Deploying alle**

- **[Docker](docs/docker.md)** — image design, proxy hub, VPN gateway
  container (tun), trust boundaries.
- **[Docker Compose walkthrough](docs/docker-compose.md)** — bundle authoring,
  secrets, managing alle from a sibling container, day-2 operations,
  troubleshooting.
- **[TUN runbook](docs/tun-runbook.md)** — whole-machine capture: privilege
  models per platform, verification, rollback.

**Understanding alle**

- **[How it works](docs/how-it-works.md)** — the runtime model: one sing-box,
  state, ports, probes.
- **[Security model](docs/security.md)** — trust boundaries, credential
  handling, Web UI/API hardening, fail-closed routing.
- **[VPN provider research](docs/vpn-provider-research.md)** — which providers
  can be supported next, and why some can't.

## Security and privacy

- Credentials and WireGuard keys stay local (`~/.alle`, owner-only
  permissions); tokens are never read from the environment implicitly.
- Proxy ports bind to loopback on hosts; the Docker image opts into the
  container network as its trust boundary.
- The loopback proxies are unauthenticated (alle assumes a single-user
  machine); the control API — Web UI and REST — is always authenticated.
- `sing-box` is a pinned upstream release, checksum-verified before every run.

The full threat model lives in **[docs/security.md](docs/security.md)**.

## Roadmap and non-goals

Planned next steps:

- More WireGuard-capable VPN providers. See
  [VPN Provider Research](docs/vpn-provider-research.md).
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
