#!/usr/bin/env bash
# Tier 3 driver: run a script inside a fresh macOS guest (see docs/tun-runbook.md).
#
# Clones the base image, boots it headless, installs alle from this checkout,
# and runs the named guest script over SSH. Everything privileged happens in
# the guest — the host's network is never touched.
#
#     scripts/tier3-macos/run.sh                    # the IPv6 tun verification
#     scripts/tier3-macos/run.sh guest-probe.sh     # just report the environment
#
# Requires: tart + sshpass, and the base image pulled once:
#     tart pull ghcr.io/cirruslabs/macos-tahoe-base:latest
set -euo pipefail

VM=${VM:-alle-tier3}
BASE=${BASE:-ghcr.io/cirruslabs/macos-tahoe-base:latest}
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
script=${1:-guest-v6.sh}

# Password auth only: an agent's loaded keys otherwise exhaust sshd's auth
# attempts before the password is ever offered.
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
	-o PreferredAuthentications=password -o PubkeyAuthentication=no -o IdentitiesOnly=yes)

say() { printf '\n== %s\n' "$*"; }
gssh() { sshpass -p admin ssh "${SSH_OPTS[@]}" "admin@$IP" "$@"; }

command -v tart >/dev/null || {
	echo "tart not installed" >&2
	exit 1
}
command -v sshpass >/dev/null || {
	echo "sshpass not installed" >&2
	exit 1
}

if ! tart list --format json | python3 -c \
	'import json,sys;sys.exit(0 if any(v["Name"]==sys.argv[1] and v["Source"]=="local" for v in json.load(sys.stdin)) else 1)' "$VM"; then
	say "cloning $BASE -> $VM"
	tart clone "$BASE" "$VM"
fi

pgrep -f "tart run $VM" >/dev/null || {
	say "booting $VM headless"
	nohup tart run "$VM" --no-graphics >"/tmp/tart-$VM.log" 2>&1 &
}

say "waiting for the guest's IP and sshd"
for _ in $(seq 1 120); do
	IP=$(tart ip "$VM" 2>/dev/null || true)
	[ -n "${IP:-}" ] && nc -z -G 2 "$IP" 22 2>/dev/null && break
	sleep 5
done
[ -n "${IP:-}" ] || {
	echo "guest never came up" >&2
	exit 1
}
echo "   guest at $IP"

say "sync the checkout into the guest"
sshpass -p admin rsync -a --delete -e "ssh ${SSH_OPTS[*]}" \
	--exclude .git --exclude .venv --exclude node_modules \
	--exclude .tun-sandbox-cache --exclude dist --exclude .localonly \
	"$repo/" "admin@$IP:alle/"

say "install uv + alle in the guest"
# `sudo alle tun on` writes root-owned __pycache__ into the user's uv tool
# directory, which blocks a later plain reinstall. Clear it with sudo first.
gssh 'sudo rm -rf ~/.local/share/uv/tools/alle-proxy'
gssh 'command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'
# shellcheck disable=SC2016  # $HOME/$PATH must expand in the guest, not here
gssh 'export PATH=$HOME/.local/bin:$PATH; uv tool install --force ~/alle 2>&1 | tail -1; alle version'

say "stage fixtures + $script"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
python3 "$here/make-confs.py" "$tmp"
sshpass -p admin scp "${SSH_OPTS[@]}" "$tmp"/*.conf "$here/$script" "admin@$IP:/tmp/" >/dev/null

say "running $script"
gssh "export PATH=\$HOME/.local/bin:\$PATH; bash /tmp/$script"
