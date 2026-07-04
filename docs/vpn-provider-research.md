# VPN Provider Research

Reference for which providers `alle` can support and how, given that the engine
is **sing-box** (not OpenVPN). Snapshot: 2026-06.

## Key conclusions

1. **sing-box cannot speak OpenVPN or IKEv2/IPsec.** The only usable protocols for
   commercial VPNs are **WireGuard**, SOCKS5 (excluded — unencrypted), and Shadowsocks
   (niche). OpenVPN-only providers are unsupportable.
2. **`alle` is WireGuard-first + encrypted-only.** No OpenVPN-only providers, no
   unencrypted SOCKS5. Credentials come from the provider API where one exists,
   else from importing the provider's WireGuard `.conf`.

## Provider archetypes

All WireGuard-capable commercial VPN providers fall into one of two categories:

**Token/API** — you provide an account credential; the provider's API derives WireGuard
keys and resolves servers. No manual config download.

**Config/portal** — no API for key derivation; you download a WireGuard `.conf` from
the provider's web portal and import it.

## MVP providers

NordVPN and ProtonVPN are the two MVP targets — one per archetype.

| Provider  | Archetype     | Credential           | Status      |
| --------- | ------------- | -------------------- | ----------- |
| NordVPN   | Token/API     | access token         | Implemented |
| ProtonVPN | Config/portal | n/a — import `.conf` | Implemented |

## Post-MVP token/API providers

These use the same token/API archetype as NordVPN. Adding them requires only a new
provider definition and credential flow — no core architecture changes.

| Provider                | Credential shape             | Notes                          |
| ----------------------- | ---------------------------- | ------------------------------ |
| Mullvad                 | 16-digit account number      | Clean API, pubkey registration |
| IVPN                    | account id                   | Clean API, pubkey registration |
| Private Internet Access | p-number username & password | per-server `addKey` endpoint   |

## Post-MVP config/portal providers

These use the same `.conf` import archetype as ProtonVPN. Any of these can be
added with zero core changes.

AirVPN, Windscribe, Surfshark, CyberGhost, FastestVPN, PrivateVPN, PureVPN, VyprVPN,
VPN Unlimited — all offer WireGuard `.conf` download from their portal.

## Excluded providers

- **OpenVPN-only** (no WireGuard): ExpressVPN, HideMyAss, Perfect Privacy, Privado,
  SlickVPN, VPN.ac/VPNSecure, Giganews — sing-box cannot speak OpenVPN.
- **SOCKS5-only** (no WireGuard): IPVanish — excluded; unencrypted proxy is out of scope.
- **TorGuard** — offers WireGuard + Shadowsocks/V2Ray; post-MVP if Shadowsocks support
  is added.

## Sources

- sing-box outbound protocols — <https://sing-box.sagernet.org/configuration/outbound/>
- NordVPN API — `api.nordvpn.com/v1/servers/countries`, `/v1/users/services/credentials`
- Mullvad WireGuard API — <https://api.mullvad.net/app/v1/wireguard-keys>
- PIA manual connections (`addKey`) — <https://github.com/pia-foss/manual-connections>
