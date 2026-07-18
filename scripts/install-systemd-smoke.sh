#!/usr/bin/env bash
# Exercise the staged installer against a real systemd user manager + logind.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
image=alle-install-systemd-smoke
name="alle-install-systemd-smoke-$$"
work=$(mktemp -d)
cleanup() {
	docker rm -f "$name" >/dev/null 2>&1 || true
	rm -rf "$work"
}
trap cleanup EXIT

docker build -f "$root/scripts/install-systemd-smoke.Dockerfile" -t "$image" "$root"
docker run --user root --privileged --cgroupns=host --detach --name "$name" \
	--mount "type=bind,src=$root,dst=/src,readonly" \
	--tmpfs /run --tmpfs /run/lock "$image"

for _ in $(seq 1 60); do
	docker exec "$name" systemctl is-system-running >/dev/null 2>&1 && break
	docker exec "$name" systemctl is-system-running 2>/dev/null | grep -q degraded && break
	sleep 1
done
docker exec "$name" systemctl is-system-running --wait >/dev/null 2>&1 ||
	docker exec "$name" systemctl is-system-running | grep -q degraded

docker exec "$name" loginctl enable-linger tester
docker exec "$name" systemctl start user@1001.service
docker exec "$name" sh -c '
  mkdir -p /host-view/etc /host-view/proc/1
  cp /etc/os-release /host-view/etc/os-release
  printf "Linux version release-smoke\n" > /host-view/proc/version
  printf "0::/user.slice/user-1001.slice\n" > /host-view/proc/1/cgroup
'

run_as_tester() {
	docker exec "$name" runuser -u tester -- env \
		HOME=/home/tester \
		PATH=/home/tester/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
		XDG_RUNTIME_DIR=/run/user/1001 \
		DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus \
		_ALLE_INSTALL_TEST_ROOT=/host-view \
		"$@"
}

test "$(run_as_tester id -u)" = 1001
run_as_tester sh /src/scripts/install.sh | tee "$work/first.log"
run_as_tester sh /src/scripts/install.sh | tee "$work/second.log"
grep -q "leaving the tool unchanged" "$work/second.log"
run_as_tester /home/tester/.local/bin/alle daemon status --json |
	grep -q '"active"[[:space:]]*:[[:space:]]*true'
run_as_tester /home/tester/.local/bin/alle health
run_as_tester sh /src/scripts/install.sh --uninstall
run_as_tester test ! -e /home/tester/.alle
run_as_tester test ! -e /home/tester/.local/bin/alle
run_as_tester test ! -e /home/tester/.local/state/alle/bootstrap-receipt
run_as_tester test ! -e /home/tester/.local/state/alle/uninstall-phase
run_as_tester test -x /home/tester/.local/bin/uv

# Linger existed before the bootstrap, so uninstall must not claim or undo it.
test "$(docker exec "$name" loginctl show-user tester -p Linger --value)" = yes

# The tool and state are gone only after both the daemon and its separately
# sessioned sing-box child have stopped. Inspect tester-owned processes without
# depending on procps being present in the minimal smoke image.
docker exec "$name" sh -eu -c '
  for status in /proc/[0-9]*/status; do
    uid=$(sed -n "s/^Uid:[[:space:]]*\([0-9][0-9]*\).*/\1/p" "$status" 2>/dev/null || true)
    [ "$uid" = 1001 ] || continue
    proc=${status%/status}
    command=$(tr "\000" " " < "$proc/cmdline" 2>/dev/null || true)
    case "$command" in
      *"alle applier"*|*"alle run"*|*"sing-box@"*)
        echo "tester runtime survived bootstrap uninstall: $command" >&2
        exit 1
        ;;
    esac
  done
'
