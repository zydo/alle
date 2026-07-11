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

alle is an independent project and is not affiliated with, endorsed by, or
sponsored by Nord Security, Proton AG, or any VPN provider.
