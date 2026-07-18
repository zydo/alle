# Getting started

Install alle, add a provider, create channels, and learn the everyday
commands. The [CLI reference](cli-reference.md) documents every command and
flag; this page is the walkthrough.

## Install

Choose a native user-level install or the scoped Docker deployment. Native
TUN can capture the host; Docker never does.

| Choice           | Supervisor                 | Traffic captured                          | Host-wide VPN |
| ---------------- | -------------------------- | ----------------------------------------- | ------------- |
| `uv`             | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| `pipx`           | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| Homebrew         | `brew services`            | Host apps or host TUN                     | Yes           |
| Docker proxy hub | Docker restart policy      | Proxy-aware containers/apps               | No            |
| Docker gateway   | Docker restart policy      | alle netns + explicitly joined containers | No            |

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

**With [Homebrew](https://brew.sh) (macOS + Linux):**

```bash
# 1. add the tap and install the headless CLI + Web UI
brew install zydo/tap/alle
# 2. run the background daemon at login, supervised by brew services
brew services start alle
```

The Homebrew channel is deliberately headless — CLI, daemon, control API, and
bundled Web UI, with no menu-bar/tray app. On this channel let `brew services`
own the daemon rather than `alle daemon install` (they would register competing
launchd/`systemd --user` units for the same user). `alle upgrade` recognizes a
brew-owned install and delegates to `brew upgrade`.

Also works: `python -m pip install alle-proxy` into an environment you manage,
or one-off runs with `uvx --from alle-proxy alle --help`.

**With Docker (proxy hub):**

```bash
docker pull ziyudo/alle:latest
docker run -d --name alle --restart unless-stopped \
  --mount type=volume,src=alle-state,dst=/var/lib/alle \
  --mount type=bind,src="$PWD/bundle.yaml",dst=/etc/alle/bundle.yaml,readonly \
  ziyudo/alle:latest
docker exec alle alle health
docker exec alle alle status
```

The bundle mount is optional, but when present use the long syntax so a
missing host file fails instead of becoming a directory. Docker uses
`alle run` plus its restart policy, not `alle daemon install`. Gateway mode
adds an explicit root override, `NET_ADMIN`, and `/dev/net/tun` in place of
the native helper/setcap/sudo ladder. It captures only alle's network
namespace and containers explicitly joined to it—not the Linux or macOS host.
See [docker.md](docker.md) and the [Compose walkthrough](docker-compose.md).

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
