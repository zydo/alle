#!/usr/bin/env bash
# Tier 2 verification of the Linux NON-ROOT privilege path:
#
#     scripts/tun-sandbox/run.sh /repo/scripts/tun-sandbox/setcap-smoke.sh
#
# The macOS v1 model needs root to create a utun. Linux has a lighter path:
# grant the pinned sing-box binary CAP_NET_ADMIN with setcap, and it can then
# create the tun + rewrite routes as an ordinary user — no root daemon. This
# script proves that path works before the service gate is taught to accept
# it:
#
#   1. as the unprivileged `sandbox` user, sing-box tun FAILS (no capability);
#   2. after `setcap cap_net_admin,cap_net_raw+ep` on the binary, the SAME
#      unprivileged user brings the tun up and traffic flows;
#   3. `getcap` reports the capability (the exact check the service gate uses).
#
# Never run outside the sandbox.
set -euo pipefail

. "$(dirname "$0")/lib.sh"

TUN_NAME="alle-tun"
SB_LOG=/tmp/sing-box-setcap.log

fetch_singbox
# work on a private copy: /cache is a host mount and setcap on a mounted fs
# would leak the capability out of the sandbox and may be unsupported.
LOCAL_SB=/tmp/sing-box-setcap-bin
cp "$SB" "$LOCAL_SB"
chmod 0755 "$LOCAL_SB"
pass "copied to a local fs for setcap"

say "engine tun config (no router port — tun is the only door)"
export ALLE_HOME=/tmp/alle-setcap
python3 - <<'EOF'
import json, sys
sys.path.insert(0, "/repo/src")
from alle.engine import Engine
from alle.state import Store
data = {"version": 1, "providers": {},
        "router": {"port": 0, "killswitch": False, "lan_direct": True, "tun": True, "rules": []}}
config, errors = Engine(Store(data=data))._build_config()
assert not errors, errors
assert any(i.get("auto_redirect") for i in config["inbounds"] if i["type"] == "tun"), "auto_redirect missing on linux"
json.dump(config, open("/tmp/setcap-tun.json", "w"), indent=2)
EOF
"$LOCAL_SB" check -c /tmp/setcap-tun.json || fail "engine config rejected by check"
pass "engine config valid and carries auto_redirect"

run_as_sandbox() { # runs sing-box as the unprivileged user; returns its result
	pkill -x sing-box-setcap-bin 2>/dev/null || true
	sleep 0.3
	su sandbox -s /bin/bash -c "'$LOCAL_SB' run -c /tmp/setcap-tun.json" >"$SB_LOG" 2>&1 &
	SB_PID=$!
	for _ in $(seq 1 25); do
		ip link show "$TUN_NAME" >/dev/null 2>&1 && sleep 0.5 && return 0
		kill -0 "$SB_PID" 2>/dev/null || return 1
		sleep 0.2
	done
	return 1
}

# ---- phase 1: no capability -> must fail
say "phase 1: unprivileged user WITHOUT the capability"
getcap "$LOCAL_SB" | grep -q cap_net_admin && fail "binary already has caps before setcap"
if run_as_sandbox; then
	kill "$SB_PID" 2>/dev/null || true
	fail "tun came up as an unprivileged user with no capability — unexpected"
fi
grep -qiE "operation not permitted|permission denied|cap|privilege" "$SB_LOG" ||
	{
		cat "$SB_LOG" >&2
		fail "expected a privilege error in the log"
	}
pass "sing-box tun refused for the unprivileged user (no CAP_NET_ADMIN)"

# ---- phase 2: setcap -> same user succeeds
say "phase 2: after setcap cap_net_admin,cap_net_raw+ep"
setcap cap_net_admin,cap_net_raw+ep "$LOCAL_SB" || fail "setcap failed"
getcap "$LOCAL_SB" | grep -q cap_net_admin || fail "getcap does not report cap_net_admin"
pass "getcap reports cap_net_admin (the service gate's exact signal)"

run_as_sandbox || {
	cat "$SB_LOG" >&2
	fail "tun did not come up even with the capability"
}
# Seizure signal: auto_route (route via tun) OR auto_redirect (fwmark rules) —
# the engine emits auto_redirect on Linux, which redirects via nftables and
# leaves the route table on the physical NIC. Connectivity below is the proof.
ip route get 9.8.7.6 2>/dev/null | grep -q "$TUN_NAME" ||
	ip rule 2>/dev/null | grep -qE "fwmark 0x20(23|24)" ||
	{
		ip rule >&2 2>/dev/null || true
		fail "neither auto_route nor auto_redirect active under setcap"
	}
su sandbox -s /bin/bash -c "curl -sSf --max-time 15 https://1.1.1.1/cdn-cgi/trace" >/dev/null ||
	{
		cat "$SB_LOG" >&2
		fail "traffic through the setcap tun failed"
	}
pass "unprivileged user (cap_net_admin only) seizes traffic and it flows — no root"
kill "$SB_PID" 2>/dev/null || true
wait "$SB_PID" 2>/dev/null || true

sleep 0.5
ip link show "$TUN_NAME" >/dev/null 2>&1 && fail "$TUN_NAME survived teardown"
pass "teardown restored the namespace"

say "ALL SETCAP CHECKS PASSED"
