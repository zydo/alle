#!/usr/bin/env bash
# Tier 2 live-tun sandbox runner (see docs/tun-runbook.md for the tier map).
#
# Builds the sandbox image and runs a command inside an isolated Linux network
# namespace with a real /dev/net/tun — the only place live tun configs run
# during development (the host's network, and the agent session on it, stay
# untouched). Defaults to the smoke flow; pass a command to drop into it, e.g.:
#
#     scripts/tun-sandbox/run.sh                # full smoke: tun/DNS/rules/killswitch
#     scripts/tun-sandbox/run.sh bash           # interactive shell in the sandbox
#
# The pinned sing-box (linux, container arch) is downloaded once into
# .tun-sandbox-cache/ (git-ignored) and checksum-verified against
# src/alle/constants.py on every run.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

docker build -q -t alle-tun-sandbox "$here" >/dev/null

# --user 0:0: the image defaults to an unprivileged user; the sandbox flows
# need root for CAP_NET_ADMIN (tun creation, route-table rewrites), so root
# is an explicit opt-in here rather than the image's default.
exec docker run --rm -i \
	--user 0:0 \
	--cap-add NET_ADMIN \
	--device /dev/net/tun \
	-v "$repo:/repo" \
	-v "$repo/.tun-sandbox-cache:/cache" \
	alle-tun-sandbox \
	"${@:-/repo/scripts/tun-sandbox/smoke.sh}"
