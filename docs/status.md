# Current status

What works today, what is planned. `alle` is usable now as a CLI-first client
for per-app and per-workflow VPN exits.

## Providers

| Provider   | Support                                                                   |
| ---------- | ------------------------------------------------------------------------- |
| NordVPN    | Token/API setup, location selection, automatic WireGuard channel creation |
| Proton VPN | WireGuard `.conf` import                                                  |

Which providers can come next, and why some cannot, is in
[VPN provider research](vpn-provider-research.md).

## Platforms

| Platform | Support                                                           |
| -------- | ----------------------------------------------------------------- |
| macOS    | Supported                                                         |
| Linux    | Supported                                                         |
| Docker   | Supported — [`ziyudo/alle`](https://hub.docker.com/r/ziyudo/alle) |
| Windows  | Planned                                                           |

## Features

| Area              | Status                                                                                                           |
| ----------------- | ---------------------------------------------------------------------------------------------------------------- |
| Core CLI          | Providers, channels, per-channel proxies, status, tests (probe + speed + traffic), logs                          |
| Routing           | Ruleset-based router entrypoint with domain/CIDR/all matchers, kill-switch, CLI shadow lint, built-in LAN bypass |
| Web UI            | Dashboard (channels, probe/speed, routes, kill-switch) + Bundle + Logs pages                                     |
| REST API          | Everything the CLI does over `/api/v1` (Bearer auth) — for scripts and compose siblings                          |
| Docker            | Container profile: proxy hub for compose networks, VPN gateway container (tun), declarative boot config          |
| Desktop companion | Planned                                                                                                          |
| Distribution      | PyPI, one-command uv bootstrap, Homebrew, and Docker Hub                                                         |

## Planned next

- More WireGuard-capable VPN providers — see
  [VPN provider research](vpn-provider-research.md).
- Desktop companion with OS-level VPN integration.
- Windows support and broader distribution.

## Non-goals

- OpenVPN or IKEv2/IPsec support.
- VPN providers without usable WireGuard support — the excluded list and the
  reasoning for each is in [VPN provider research](vpn-provider-research.md).
- SOCKS5-only or unencrypted proxy providers.
- Bundling `sing-box` inside the Python package.
