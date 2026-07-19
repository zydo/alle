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

## IPv6 support (planned providers)

Snapshot: **2026-07**, checked against each provider's own support/knowledge-base
articles. Scope is the post-MVP providers above (NordVPN and ProtonVPN are already
implemented, not "planned", and are omitted). For `alle`, what matters is whether the
provider's **WireGuard config/API actually assigns a routable IPv6 address** — a
generic "IPv6 leak protection" feature just blocks IPv6 outside the tunnel and is not
IPv6 support.

| Provider                | IPv6 status                                        | Notes                                                                                                                                                          |
| ----------------------- | -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Mullvad                 | **Supported** (dual-stack)                         | WireGuard tunnels carry IPv6 as well as IPv4 (native since 2014); IPv6 is off by default in the app and Mullvad's own guidance leans toward leaving it off.    |
| AirVPN                  | **Supported** (dual-stack, entry + exit)           | Servers are reachable over both IPv4 and IPv6 entry-IPs; WireGuard configs assign an IPv6 ULA address (`fd7d:76ee:e68f:a993::/64`) alongside the IPv4 one.     |
| IVPN                    | **Partial**                                        | WireGuard gives IPv6-over-IPv4 egress (access the IPv6 internet through an IPv4 tunnel); native IPv6 *to* their servers has been "under development" 2+ years. |
| Windscribe              | **Partial** (WireGuard, select Pro locations only) | IPv6 egress works when "IP Stack" is set to Auto over WireGuard, but only on enabled Pro server locations — not fleet-wide.                                    |
| Private Internet Access | **Not supported** (blocked, not routed)            | IPv4-only by design; the app actively blocks/leak-protects IPv6 rather than tunneling it.                                                                      |
| VyprVPN                 | **Not supported**                                  | Official support: VyprVPN "will only route a VPN connection using IPv4"; recommends disabling IPv6 locally.                                                    |
| Surfshark               | **Not supported**                                  | Official support: "Surfshark does not support the IPv6 protocol"; recommends disabling IPv6 locally.                                                           |
| IPVanish                | **Not supported (yet)**                            | Official article: "IPVanish currently only supports IPv4"; company says IPv6 support is planned "absolutely" but with no ETA.                                  |
| PureVPN                 | **Not supported** (leak-protected only)            | Public IPv6 page is "IPv6 Leak Protection" — blocks IPv6 outside the tunnel, does not route it.                                                                |
| TorGuard                | **Not supported** (leak-protected only)            | Only public IPv6 material is a 2015 "IPv6 Leak Protection" blog post (blocking); forum ETA requests are unanswered.                                            |
| PrivateVPN              | **Undocumented** — likely unsupported              | No official IPv6-support statement; only "disable IPv6" troubleshooting guides exist.                                                                          |
| VPN Unlimited           | **Undocumented** — likely unsupported              | KB has only a generic "what is IPv6" glossary entry; no statement of support anywhere.                                                                         |
| PrivadoVPN              | **Undocumented** — likely unsupported              | No support article confirms tunneling; only "disable IPv6" troubleshooting guides.                                                                             |
| VPN.ac                  | **Undocumented** — likely unsupported              | No KB article confirms IPv6; the app-options doc doesn't mention IPv6 addressing.                                                                              |
| FastestVPN              | **Undocumented** — likely unsupported              | Only "disable IPv6" troubleshooting guides found; no support statement either way.                                                                             |

**Bottom line:** of the planned providers, only **Mullvad** and **AirVPN** hand out a
real dual-stack IPv6 address in their WireGuard config today. IVPN and Windscribe are
conditional/partial. Everything else is IPv4-only (either by explicit statement or by
the absence of any documented support) — an IPv6-capable client on those providers
would need to fall back to IPv4 (or disable IPv6 locally, per their own guidance) to
avoid leaking outside the tunnel.

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
- Mullvad IPv6 — <https://mullvad.net/en/blog/ipv6-support>,
  <https://mullvad.net/en/blog/introducing-wireguard-over-tcp-and-ipv6>
- AirVPN IPv6 (dual-stack entry/exit, WireGuard ULA) — <https://airvpn.org/specs/>
- IVPN IPv6 (partial, egress-only) —
  <https://www.ivpn.net/knowledgebase/general/do-you-support-ipv6/>
- Windscribe IPv6 (WireGuard, select Pro locations) —
  <https://windscribe.com/knowledge-base/articles/does-windscribe-support-ipv6>
- PIA IPv6 (blocked, not routed) —
  <https://helpdesk.privateinternetaccess.com/hc/en-us/articles/46777101512987-Why-Do-You-Block-IPv6>
- VyprVPN IPv6 (unsupported) —
  <https://support.vyprvpn.com/hc/en-us/articles/360038600131-Does-VyprVPN-support-IPv6>
- Surfshark IPv6 (unsupported) —
  <https://support.surfshark.com/hc/en-us/articles/360011550239-Does-Surfshark-support-IPv6-Do-I-have-it-on-my-network>
- IPVanish IPv6 (unsupported, planned) —
  <https://support.ipvanish.com/hc/en-us/articles/33788722461083-Does-IPVanish-support-IPv6>
- PureVPN IPv6 (leak protection only) —
  <https://www.purevpn.com/features/ipv6-leak-protection>
- TorGuard IPv6 (leak protection only) —
  <https://blog.torguard.net/ipv6-leak-protection-with-torguard-vpn/>
