<p align="center">
  <img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/wordmark.svg" alt="alle" width="320">
</p>

<p align="center">
  <a href="https://github.com/zydo/alle/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/zydo/alle/ci.yml?branch=main&amp;label=CI" alt="CI"></a>
  <a href="https://pypi.org/project/alle-proxy/"><img src="https://img.shields.io/pypi/v/alle-proxy.svg?label=PyPI" alt="PyPI"></a>
  <a href="https://github.com/zydo/homebrew-tap"><img src="https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fraw.githubusercontent.com%2Fzydo%2Fhomebrew-tap%2Fmain%2FFormula%2Falle.rb&amp;search=alle_proxy-(%5B0-9.%5D%2B)%5C.tar&amp;replace=v%241&amp;label=Homebrew" alt="Homebrew"></a>
  <a href="https://hub.docker.com/r/ziyudo/alle"><img src="https://img.shields.io/docker/v/ziyudo/alle?sort=semver&amp;label=Docker" alt="Docker"></a>
</p>

# alle

A universal VPN client that manages multiple VPN connections with rule-based routing, with interfaces for human (Web UI and CLI) and programs (REST API and Docker image).

## VPN Providers

**Supported**

<table>
  <tr>
    <td align="center" width="112"><img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/providers/nordvpn.png" alt="" height="56"><br>NordVPN</td>
    <td align="center" width="112"><img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/providers/protonvpn.png" alt="" height="56"><br>Proton VPN</td>
  </tr>
</table>

**Planned (Developing)**

<table>
  <tr>
    <td align="center" width="112"><img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/providers/mullvad.png" alt="" height="56"><br>Mullvad</td>
    <td align="center" width="112"><img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/providers/ivpn.png" alt="" height="56"><br>IVPN</td>
    <td align="center" width="112"><img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/providers/pia.png" alt="" height="56"><br>PIA</td>
    <td align="center" width="112"><img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/providers/vyprvpn.png" alt="" height="56"><br>VyprVPN</td>
  </tr>
</table>

See [VPN provider research](docs/vpn-provider-research.md) for setup archetypes,
provider-specific constraints, and excluded providers.

<p align="center">
  <img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/webui.png" alt="alle Web UI dashboard" width="900">
  <br>
  <em>Web UI</em>
</p>

# Why alle

## For people

You already pay for a commercial VPN — but its official client connects to one
location at a time. Switching countries means disconnecting, reconnecting, and
breaking whatever was using the old exit. Two locations at once is not on offer.

`alle` keeps several exits live simultaneously, from one provider or mixed across
providers. Different traffic leaves through different VPN servers, decided by
alle's routing rules:

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/why-alle-dark.svg">
    <img src="https://raw.githubusercontent.com/zydo/alle/main/src/alle/assets/readme/why-alle.svg" alt="Three apps routed through alle to three different VPN exits at the same time" width="700">
  </picture>
</p>

## For programs

Every action the CLI performs is also a REST call, and each exit is a stable
`127.0.0.1:<port>` proxy. So another program can drive the whole lifecycle of
many VPN connections — create, probe, rotate, retire — on its own schedule,
with no human clicking a client. That is what makes proxy rotation across
regions, and reaching geofenced resources from wherever they are served,
something you can script.

It also ships as a container image — [`ziyudo/alle`](https://hub.docker.com/r/ziyudo/alle)
— so a compose stack can add it as one service and let sibling containers reach
the internet through whichever exit the rules pick.

## What alle does

`alle` runs multiple VPN exits side by side, each its own local HTTP+SOCKS
proxy. One router entrypoint sends traffic by rule to an exit, straight out, or
nowhere at all — see [Rule-based routing](docs/routing.md). For a whole-machine
VPN through those same rules there is an optional **TUN mode** (`alle tun on`,
one-time privilege grant): [CLI reference](docs/cli-reference.md#alle-tun-onoff),
[runbook](docs/tun-runbook.md).

The runtime model — one `sing-box` process, state, ports, probes — is in
[How it works](docs/how-it-works.md). What is supported today is in
[Current status](docs/status.md).

## Get started

```bash
# macOS + Linux: installs alle and its user-level login service
curl -LsSf \
  https://github.com/zydo/alle/releases/latest/download/install.sh | sh

alle providers add nordvpn
alle channels add nordvpn --country "United States"
alle start
alle channels ls                # prints each channel's local proxy port
```

Point anything proxy-aware at a channel's port, and it exits there.

Homebrew, `uv`, `pipx`, Docker, the checksum-verified manual install, and the
uninstaller are all in **[Getting started](docs/getting-started.md)**; container
deployments are in **[Docker](docs/docker.md)**.

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
- **[Current status](docs/status.md)** — supported providers and platforms,
  feature matrix, what is planned, and the non-goals.
- **[Security model](docs/security.md)** — trust boundaries, credential
  handling, Web UI/API hardening, fail-closed routing.
- **[VPN provider research](docs/vpn-provider-research.md)** — which providers
  can be supported next, and why some can't.

## Security and privacy

See the **[Security model](docs/security.md)**.

## License

MIT
