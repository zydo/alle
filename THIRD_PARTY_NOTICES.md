# Third-Party Notices

alle is an orchestration layer. It does not bundle, modify, or redistribute
any third-party source code or binaries. The components below are obtained at
runtime (downloaded directly from their upstream release pages onto the user's
machine) and remain the work of their respective authors under their own licenses.

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
