# VPN Provider Research

Reference for which providers `alle` can support and how, given that the engine
is **sing-box** (not OpenVPN). Snapshot: **2026-07**, verified against provider documentation.

## Key conclusions

1. **sing-box cannot speak OpenVPN or IKEv2/IPsec.** The only usable protocols for
   commercial VPNs are **WireGuard**, SOCKS5 (excluded — unencrypted), and Shadowsocks
   (niche). OpenVPN-only providers are unsupportable.
2. **`alle` is WireGuard-first + encrypted-only.** No OpenVPN-only providers, no
   unencrypted SOCKS5. Credentials come from the provider API where one exists,
   else from importing the provider's WireGuard `.conf`.
3. **"Supports WireGuard" is not the bar — exporting it is.** Several providers run
   WireGuard *inside their own apps only* (sometimes as a customized,
   non-interoperable variant) with no config download or key API. Those are just as
   unsupportable as OpenVPN-only providers. As IPVanish's own announcement put it:
   "most VPN providers only offer WireGuard connections through their apps rather
   than allowing manual configuration options." The disqualifier is apps-only
   WireGuard, not WireGuard absence.

## Provider archetypes

All *supportable* commercial VPN providers fall into one of two categories:

**Token/API** — you provide an account credential; the provider's API derives WireGuard
keys and resolves servers. No manual config download.

**Config/portal** — you download a WireGuard `.conf` from the provider's web portal
(usually a config generator) and import it.

## MVP providers

NordVPN and ProtonVPN are the two MVP targets — one per archetype.

| Provider  | Archetype     | Credential           | Status      |
| --------- | ------------- | -------------------- | ----------- |
| NordVPN   | Token/API     | access token         | Implemented |
| ProtonVPN | Config/portal | n/a — import `.conf` | Implemented |

## Post-MVP token/API providers

Same archetype as NordVPN: a new provider definition and credential flow — no core
architecture changes.

| Provider                | Credential shape             | Notes                                                                                                                     |
| ----------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Mullvad                 | 16-digit account number      | Clean API, pubkey registration; also has a portal config generator                                                        |
| IVPN                    | account id                   | Clean API, pubkey registration                                                                                            |
| Private Internet Access | p-number username & password | per-server `addKey` endpoint (`pia-foss/manual-connections`, official)                                                    |
| VyprVPN                 | email & password             | Official **VyprVPN WireGuard API** — their open Go client authenticates and drives standard `wg-quick`. No portal `.conf` |

## Post-MVP config/portal providers

Same `.conf` import archetype as ProtonVPN — zero core changes. All verified to
offer standard WireGuard config download/generation from their portal:

| Provider      | Config source                                                  | Notes                                         |
| ------------- | -------------------------------------------------------------- | --------------------------------------------- |
| AirVPN        | `airvpn.org/generator`                                         | Mature generator                              |
| Windscribe    | Config Generators feature                                      |                                               |
| Surfshark     | Manual WireGuard setup in account dashboard                    | Official router/Windows guides                |
| PrivateVPN    | Portal "Generate Config" → download                            |                                               |
| VPN Unlimited | KeepSolid User Office generator                                | Official guides incl. GL.iNet                 |
| IPVanish      | WireGuard Configuration Generator in Account Portal (Dec 2024) | Custom pubkey/port options                    |
| TorGuard      | Config generator; guides target the *official* WireGuard app   | No Shadowsocks needed — plain WireGuard works |
| PrivadoVPN    | Portal generator for WireGuard/OpenVPN manual setups           |                                               |
| VPN.ac        | WireGuard key-management tool (`vpn.ac/wgmanager`) → `.conf`   |                                               |

**Supportable with caveats** (works, but document the friction):

| Provider   | Caveat                                                                                                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PureVPN    | Portal generates WireGuard configs, but they **expire** ("activate within 30 minutes… or redownload") — hostile to alle's stored-config model; expect frequent re-imports |
| FastestVPN | WireGuard `.conf` provided **only via support email** — no self-service generator; provisioning is manual and slow                                                        |

## Excluded providers

- **ExpressVPN** — *has* WireGuard (since ~2025), but only as a **customized,
  non-interoperable variant inside their apps** (post-quantum handshake, ephemeral
  keys, dynamic IPs). Every manual-setup path they publish (Windows, macOS, Linux,
  pfSense, all routers) is **OpenVPN-only**; no standard config export, no key API.
- **CyberGhost** — WireGuard **in their apps only** (Android/iOS/Linux CLI app);
  the "Configure Device" portal generates **OpenVPN configs exclusively**.
- **HideMyAss (HMA)** — no WireGuard manual configuration exists anywhere in their
  support catalog; device/router setup is OpenVPN-only, everything else is
  app-centric.
- **Perfect Privacy** — **no WireGuard at all** (confirmed by current third-party
  reviews); OpenVPN/IPsec only.
- **SlickVPN** — OpenVPN/IPsec/PPTP only; no WireGuard.
- **VPNSecure** — OpenVPN/PPTP only; no WireGuard.
- **Giganews** — a Usenet service whose VPN is a **VyprVPN white-label**; not a
  provider in its own right. If VyprVPN's API path is ever implemented, Giganews
  bundles may ride it — track under VyprVPN.

## Sources

- sing-box outbound protocols — <https://sing-box.sagernet.org/configuration/outbound/>
- NordVPN API — `api.nordvpn.com/v1/servers/countries`, `/v1/users/services/credentials`
- Mullvad WireGuard API + generator — <https://api.mullvad.net/app/v1/wireguard-keys>,
  <https://mullvad.net/en/account/wireguard-config>
- PIA manual connections (`addKey`) — <https://github.com/pia-foss/manual-connections>
- VyprVPN WireGuard Go client (API-based) —
  <https://support.vyprvpn.com/hc/en-us/articles/43750934530317-VyprVPN-WireGuard-Go-Client-Setup>
- IPVanish WireGuard Configuration Generator —
  <https://www.ipvanish.com/blog/generate-wireguard-configurations/>
- ExpressVPN protocols (apps-only custom WireGuard) —
  <https://www.expressvpn.com/what-is-vpn/protocols>; manual-setup catalog (OpenVPN-only) —
  <https://www.expressvpn.com/support/vpn-setup/>
- PrivadoVPN manual-config generator —
  <https://support.privadovpn.com/kb/article/1130-how-to-generate-a-wireguard-or-openvpn-configuration-file-for-manual-setups/>
- VPN.ac WireGuard key manager — <https://vpn.ac/knowledgebase/125/WireGuard-on-OpenWRT.html>
- TorGuard WireGuard guides — <https://torguard.net/wireguard>
- PureVPN WireGuard manual setup (expiring configs) —
  <https://support.purevpn.com/en_US/purevpn-android/setup-wireguard-on-android>
- FastestVPN WireGuard on Linux (config via support email) —
  <https://support.fastestvpn.com/tutorials/linux/wireguard/>
- AirVPN generator — <https://airvpn.org/generator/>; Windscribe config generators —
  <https://windscribe.com/features/config-generators>; Surfshark manual WireGuard —
  <https://support.surfshark.com/hc/en-us/articles/6585805595666>; VPN Unlimited
  WireGuard manuals — <https://www.vpnunlimited.com/help/manuals/wireguard/windows>
