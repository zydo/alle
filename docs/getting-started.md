# Getting started

Install alle, add a provider, create channels, and learn the everyday
commands. The [CLI reference](cli-reference.md) documents every command and
flag; this page is the walkthrough.

## Install

`alle` is a Python CLI (Python 3.10+) installed as a user-level tool — no
sudo. Two recommended, fully explicit paths; each step is an ordinary command
you can inspect, and the tool that installed `alle` is also the one that
upgrades and uninstalls it.

**With [`uv`](https://docs.astral.sh/uv/getting-started/installation/):**

```bash
# 1. install uv (see its docs for other methods)
curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. install the alle CLI
uv tool install alle-proxy
# 3. (optional) run the background daemon at login, so channels survive a reboot
alle daemon install
```

**With [`pipx`](https://pipx.pypa.io/stable/):**

```bash
# 1. install pipx (e.g. `brew install pipx` or your distro's package)
# 2. install the alle CLI
pipx install alle-proxy
# 3. (optional) run the background daemon at login
alle daemon install
```

Step 3 is optional: without it the runtime auto-starts on first use
(`alle start` or the first channel you add) and runs for the session.
`alle daemon install` registers it as a user-level login service (macOS
LaunchAgent / `systemd --user`) so it starts at login and is supervised — see
the [CLI reference](cli-reference.md#alle-daemon).

Also works: `python -m pip install alle-proxy` into an environment you manage,
or one-off runs with `uvx --from alle-proxy alle --help`.

For servers and compose stacks there is also an official Docker image — see
[docker.md](docker.md).

After installation:

```bash
alle version
alle --help
```

**Uninstall** with the same tool that installed it — `uv tool uninstall
alle-proxy` or `pipx uninstall alle-proxy` (run `alle stop` first). `~/.alle`
is left behind since it holds your provider credentials and WireGuard keys; a
reinstall picks up where you left off. Remove it with `rm -rf ~/.alle` if you
want everything gone.

## Quick start

Add a provider, create a channel, start the runtime, then use the channel's
local proxy port.

```bash
alle providers add nordvpn
alle channels add nordvpn --country "United States"
alle start
alle channels ls
```

`alle channels ls` prints the local proxy port for each channel:

```text
LABEL            ID                       PORT    COUNTRY        CITY
---------------  -----------------------  ------  -------------  ----------
united_states_1  nordvpn/united_states_1  :53124  United States  (Any City)
```

Use that port from any tool or app that supports an HTTP or SOCKS proxy:

```bash
curl -x http://127.0.0.1:53124 https://api.ipify.org
```

Check health and traffic (`status` is the system summary; `test` is the
per-channel table — fresh IP/latency plus cumulative sent/received):

```bash
alle status
alle test
```

## Provider setup

`alle` supports two provider setup styles today:

**NordVPN** uses an access token:

```bash
alle providers add nordvpn
alle locations nordvpn
alle locations nordvpn --country "United States"
alle channels add nordvpn --country "United States" --city "Seattle"
```

To rotate a bad or expired token later, run `alle providers add nordvpn` again
(or use the gear on the provider in the Web UI): it confirms, validates the new
token, and re-resolves the provider's channels — no need to remove and re-add.
The stored token is never displayed back, only a masked preview. This is distinct
from a bundle import (which changes your whole setup from a file); a token update
changes one live credential. See
[`alle providers add`](cli-reference.md#alle-providers-add-provider).

**Proton VPN** uses WireGuard config files downloaded from Proton:

```bash
alle providers add protonvpn
alle channels add protonvpn --config ~/Downloads/wg-US-CA-842.conf
```

Re-importing the same `.conf` file updates that channel in place, keeping the
same channel id and local port; re-importing a byte-identical file changes
nothing and tells you the channel already exists.

## Friendly names

Channels are identified by a globally-unique, provider-qualified id
(`nordvpn/united_states_1`) — the handle every command takes, shown in the `ID`
column. You can also give one a display label for readability (the `LABEL`
column in `channels ls` and `test`). The id never changes,
so relabelling is always safe:

```bash
alle channels add nordvpn --country "United States" --label "Streaming - US"
alle channels setlabel united_states_1 "Streaming - US"   # or set it later
alle channels setlabel united_states_1                    # omit text to clear
```

## Common commands

Useful commands after setup:

```bash
alle providers ls
alle channels ls
alle channels ls --refs
alle status
alle test
alle logs
alle stop
```

Most read commands support `--json` for scripts:

```bash
alle status --json
alle channels ls --json
alle test --json
```

Channel and provider removals accept multiple targets:

```bash
alle channels rm japan_1 united_states_seattle_1
alle channels rm protonvpn/wg_us_ca_842
alle channels rm 'united_states_*' --dry-run
alle providers rm nordvpn protonvpn -y
```

## Hold more channels than your plan's connection cap

Some subscriptions limit simultaneous connections (NordVPN and Proton VPN
allow ~10). A **disabled** channel stays in your config but is not
materialised at all — no WireGuard handshake or keepalive toward the
provider, so it uses **no connection slot**. Keep a stable of servers on hand
and flip which ones are live:

```bash
alle channels disable japan_1            # free the slot; config + rules stay
alle channels enable japan_1             # dial it again
alle channels disable 'united_states_*'  # same ref grammar as rm
```

Disabled channels stay visible everywhere (`channels ls` grows a STATUS
column; `test` shows a skipped `Disabled` row) and can't be targeted by
routing rules while disabled. This is local intent only — it doesn't
deregister the device from your provider account.

## Where to next

- [Rule-based routing](routing.md) — one router entrypoint, first-match rules.
- [Web UI](web-ui.md) — the browser dashboard (`alle ui`).
- [Backup and declarative setup](declarative-config.md) — the bundle file.
- [CLI reference](cli-reference.md) — every command, flag, and env var.
