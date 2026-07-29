# Third-Party Notices

alle's package ships its own code plus small provider brand logos for the Web UI
(see "Provider brand assets" below). Two kinds of third-party component are
involved when you run it:

1. **Python dependencies** — declared in `pyproject.toml` and installed from PyPI
   alongside alle. These are the work of their respective authors under their own
   licenses (below).
2. **sing-box** — not a Python dependency and never bundled. alle downloads the
   upstream binary at runtime, verifies it against a pinned SHA-256, and runs it
   as a separate process.

## Python dependencies

### packaging

- Project: https://github.com/pypa/packaging
- License: **Apache License 2.0 or BSD 2-Clause License**
- Role in alle: standards-compliant PEP 440 release parsing and ordering for
  channel-aware upgrades.

### PyYAML

- Project: https://github.com/yaml/pyyaml
- License: **MIT License**
- Role in alle: reading and writing the YAML credential store (`credentials.yaml`).

### pycountry

- Project: https://github.com/pycountry/pycountry
- License: **GNU Lesser General Public License v2.1 only (LGPL-2.1)**
- Role in alle: ISO country/subdivision name resolution for location selection.

## sing-box

- Project: https://github.com/SagerNet/sing-box
- Binary: official release pinned by alle, downloaded from
  `https://github.com/SagerNet/sing-box/releases` into
  `~/.alle/bin/sing-box@<version>` and verified against a pinned SHA-256.
- License: **GNU General Public License v3.0 or later**, with an additional
  naming/association clause.

alle runs the unmodified sing-box binary as a **separate process** (it never
links against or embeds sing-box), so alle itself remains under the MIT
License. sing-box is a separate work distributed under the GPL-3.0-or-later:

```
Copyright (C) 2022 by nekohasekai <contact-sagernet@sekai.icu>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.

In addition, no derivative work may use the name or imply association
with this application without prior consent.
```

> alle does not redistribute the sing-box binary; each user downloads it
> directly from the upstream release page. alle is an independent project and
> is not affiliated with, endorsed by, or sponsored by the sing-box project.

## Provider brand assets

alle's Web UI shows small provider wordmarks/logos to identify each supported
provider in the dashboard. These are trademarks of their respective owners,
used here nominatively — solely to identify the provider and its service — and
do not imply any affiliation with, endorsement by, or sponsorship from the
trademark holders.

- **NordVPN** (`src/alle/assets/nordvpn.svg`) — "NordVPN" and the NordVPN logo
  are trademarks of Nord Security.
- **Proton VPN** (`src/alle/assets/protonvpn.svg`) — "Proton VPN" and the Proton
  logo are trademarks of Proton AG. This SVG was derived from an upstream asset
  whose embedded Inkscape/RDF metadata mislabeled it "Proton Mail"; that stale
  metadata has been corrected to "Proton VPN."

### README provider icons

`src/alle/assets/readme/providers/*.png` are README-only (excluded from the installed
wheel). Each is the provider's own published icon-only mark, downloaded from the
source below and **not redrawn**: the only processing is a proportional resize
and centring on a uniform white rounded tile, so every mark stays legible in
both light and dark READMEs.

| Provider                | Source                                                                                                                                                               | Trademark holder               |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| NordVPN                 | `sb.nordcdn.com/m/5dd86f435e9b1aea/original/nordvpn-symbolBlue.svg`                                                                                                  | Nord Security                  |
| Proton VPN              | `pmecdn.protonweb.com` → `static/logos/vpn/vpn-badge.svg`                                                                                                            | Proton AG                      |
| Mullvad                 | `mullvad.net/favicon.svg`                                                                                                                                            | Mullvad VPN AB                 |
| IVPN                    | `github.com/ivpn/desktop-app` → `ui/References/Linux/ui/ivpnicon.svg` (GPL-3.0 repo, no separate trademark grant)                                                    | IVPN / Privatus Limited        |
| Private Internet Access | `assets-cms.privateinternetaccess.com/photos/shares/pia-homepage/PIA-Logo.svg`                                                                                       | Private Internet Access, Inc.  |
| VyprVPN                 | `www.vyprvpn.com/site/templates/images/vyprvpn_logo.svg`                                                                                                             | Certida                        |
| AirVPN                  | Google Play Store listing icon for `org.airvpn.eddie` (`play-lh.googleusercontent.com`, 512×512) — airvpn.org itself only publishes a 16×16 favicon of the same mark | AirVPN                         |
| Windscribe              | `windscribe.com/favicon.ico` (48×48 frame)                                                                                                                           | Windscribe Limited             |
| Surfshark               | `surfshark.com/website/_next/public/global/favicon-192.png`                                                                                                          | Surfshark B.V.                 |
| PrivateVPN              | `privatevpn.com/apple-touch-icon.png`                                                                                                                                | PrivateVPN                     |
| VPN Unlimited           | `vpnunlimited.com/icon.png`                                                                                                                                          | KeepSolid Inc.                 |
| IPVanish                | `ipvanish.com/wp-content/uploads/2021/10/cropped-ipv-icon-270x270.png`                                                                                               | IPVanish                       |
| TorGuard                | `torguard.net/favicon.ico` (32×32 frame — no larger icon-only mark is published)                                                                                     | TorGuard                       |
| PrivadoVPN              | `privadovpn.com/favicon.svg`                                                                                                                                         | Privado Networks, Inc.         |
| VPN.ac                  | `vpn.ac/assets/images/touch-icon.png`                                                                                                                                | VPN.ac                         |
| PureVPN                 | `purevpn.com/wp-content/uploads/2023/02/cropped-pvpn-favicon-img-192x192.png`                                                                                        | PureVPN                        |
| FastestVPN              | `fastestvpn.com/favicon.ico` (32×32 frame — no larger icon-only mark is published)                                                                                   | FastestVPN                     |
| ExpressVPN              | `expressvpn.com/apple-touch-icon.png`                                                                                                                                | Express VPN International Ltd. |
| CyberGhost              | `cyberghostvpn.com/apple-touch-icon.png`                                                                                                                             | CyberGhost S.R.L.              |
| HideMyAss (HMA)         | `static2.hidemyass.com` → `web/i/icons/favicon/android-chrome-256x256.png`                                                                                           | HideMyAss (HMA VPN)            |
| SlickVPN                | `slickvpn.com/assets/logo-icon.png`                                                                                                                                  | SlickVPN                       |
| VPNSecure.me            | `vpnsecure.me/favicon.svg`                                                                                                                                           | VPNSecure.me                   |

One asset was edited beyond resizing: VyprVPN publishes no standalone icon, only
a combined lockup. The wordmark group was removed and the viewBox re-fit to the
mark's own bounds — no path data was altered.

Two of these carry usage terms narrower than general nominative use, recorded
here so the position is explicit rather than assumed:

- **Mullvad** — its press page grants the logos "for editorial purposes in which
  our work is described."
- **VyprVPN** — its Terms of Service (§2) state the marks are provided for
  personal, non-commercial use and grant no third-party logo permission.

Both are shown here to identify a provider alle supports or plans to support,
which is the nominative use described above; neither provider has reviewed or
approved this project. Remove either on request from the trademark holder.

alle is an independent project and is not affiliated with, endorsed by, or
sponsored by any of the trademark holders listed above, or any VPN provider.
