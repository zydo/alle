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
| Distribution      | PyPI, one-command uv bootstrap, Homebrew, Docker Hub                                                          |

## Planned next

- More VPN providers, WireGuard now and OpenVPN once sing-box 1.14 stabilizes —
  see [VPN provider research](vpn-provider-research.md).
- A macOS native app, built as an independent project on top of the public
  REST API — not bundled with this CLI core or the Web UI.
- Windows support and broader distribution.

## Non-goals

- IKEv2/IPsec support.
- VPN providers without a usable WireGuard or (once supported) OpenVPN
  credential path — the excluded list and the reasoning for each is in
  [VPN provider research](vpn-provider-research.md).
- SOCKS5-only or unencrypted proxy providers.
- Bundling `sing-box` inside the Python package.
