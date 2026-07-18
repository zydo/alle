# Getting started

Install alle, add a provider, create channels, and learn the everyday
commands. The [CLI reference](cli-reference.md) documents every command and
flag; this page is the walkthrough.

## Install

Choose a native user-level install or the scoped Docker deployment. Native
TUN can capture the host; Docker never does.

| Choice                | Supervisor                 | Traffic captured                          | Host-wide VPN |
| --------------------- | -------------------------- | ----------------------------------------- | ------------- |
| One-command uv script | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| Homebrew              | `brew services`            | Host apps or host TUN                     | Yes           |
| Manual uv install     | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| Manual pipx install   | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| Docker proxy hub      | Docker restart policy      | Proxy-aware containers/apps               | No            |
| Docker gateway        | Docker restart policy      | alle netns + explicitly joined containers | No            |

### One-command script (macOS + Linux)

```bash
curl -LsSf \
  https://github.com/zydo/alle/releases/latest/download/install.sh | sh
```

The release-pinned bootstrap supports normal, non-root macOS and systemd Linux
hosts on arm64/aarch64 and x86_64. It verifies and installs a pinned uv when
needed, installs that release's exact `alle-proxy` version, registers the
user-level login service, and verifies readiness. It refuses containers, WSL,
non-systemd Linux sessions, and installs already owned by another package
manager; it never invokes `sudo` or a system package manager. On Linux, opt
into running after logout explicitly by replacing the final `sh` with
`sh -s -- --linger`.

The command works from bash, zsh, ash, and other interactive shells because
they only feed the asset to its declared POSIX `sh` interpreter; the installer
does not depend on the caller's shell syntax.

If the uv tool directory was not already on `PATH`, the installer updates the
appropriate shell profile and prints both the exact temporary `export` command
and the absolute `alle` path. Restart the shell before relying on bare `alle`,
or run that printed export to use it immediately in the current shell.

The one-liner trusts HTTPS and GitHub for the first downloaded byte, and its
`latest` URL moves to each new stable release. To inspect an immutable,
explicitly tagged asset and verify that release's published digest before
executing it:

```bash
version=v0.1.10
base="https://github.com/zydo/alle/releases/download/$version"
curl -LsSf -O "$base/install.sh"
curl -LsSf -O "$base/install.sh.sha256"
sha256sum -c install.sh.sha256             # Linux
# or: shasum -a 256 -c install.sh.sha256   # macOS
less install.sh
sh install.sh                              # add --linger on Linux if desired
```

### Manual install with [`uv`](https://docs.astral.sh/uv/)

```bash
# Install uv first using its official instructions, then install alle.
uv tool install alle-proxy
# If uv reports that its tool directory is not on PATH:
uv tool update-shell
```

Restart the shell after `uv tool update-shell` before using bare `alle`. To run
the background daemon at login, optionally register it in that new shell:

```bash
alle daemon install
```

Without the service step, the runtime auto-starts on first use and runs for the
session.

### Install with [Homebrew](https://brew.sh) (macOS + Linux)

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

### Manual install with [`pipx`](https://pipx.pypa.io/stable/)

```bash
# 1. install pipx (e.g. `brew install pipx` or your distro's package)
# 2. install the alle CLI
pipx install alle-proxy
# 3. (optional) run the background daemon at login
alle daemon install
```

The pipx service step is optional: without it the runtime auto-starts on first
use (`alle start` or the first channel you add) and runs for the session.
`alle daemon install` registers it as a user-level login service (macOS
LaunchAgent / `systemd --user`) so it starts at login and is supervised — see
the [CLI reference](cli-reference.md#alle-daemon).

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

**Uninstall** according to the channel that owns alle:

```bash
# macOS only: do this first if you installed the optional root helper
sudo alle helper uninstall

# One-command script: remove service, uv-owned alle tool, and all alle state
curl -LsSf \
  https://github.com/zydo/alle/releases/latest/download/install.sh | \
  sh -s -- --uninstall

# Manual uv
alle stop
alle daemon uninstall
uv tool uninstall alle-proxy

# Manual pipx
alle stop
alle daemon uninstall
pipx uninstall alle-proxy

# pip, from the same managed Python environment used to install alle
alle stop
alle daemon uninstall
python -m pip uninstall alle-proxy

# Homebrew
brew services stop alle
alle stop
brew uninstall alle
```

The script uninstaller acts only when its bootstrap receipt proves ownership.
It removes only the uv-owned alle tool recorded or adopted by the bootstrap,
never uv itself or a pipx, pip, or Homebrew installation. It refuses while the
optional macOS root helper is still installed because a user-level script
cannot safely remove its root LaunchDaemon. It deletes the dedicated state
directory recorded during bootstrap, including provider credentials and
WireGuard keys, and restores Linux login lingering only when the bootstrap
enabled it. It retains uv itself because uv may now be used independently.
If uninstall is interrupted after teardown starts, rerun the same command; the
receipt-backed cleanup resumes without claiming an unrelated installation.
Manual package-manager uninstalls leave `~/.alle` behind; remove it separately
if you want their state gone. If you set `ALLE_HOME` for the bootstrap, make it
a dedicated alle state directory: successful script uninstall removes that
recorded directory in full. The explicit `alle stop` in the manual recipes
also covers a session runtime that was started without a login service.

Upgrades track stable releases by default. A uv, pipx, or pip installation can
explicitly inspect or install a future prerelease with `alle upgrade --check
--prerelease` or `alle upgrade --prerelease`. Homebrew, the one-command
bootstrap's `latest` asset, Docker `latest`, and GitHub's stable `latest` stay
on numeric stable releases. Version ordering follows
[PEP 440](https://packaging.python.org/en/latest/specifications/version-specifiers/)
(`0.1.8` < `0.1.9rc1` < `0.1.9`) while remaining fully compatible with
numeric-only versions.

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
