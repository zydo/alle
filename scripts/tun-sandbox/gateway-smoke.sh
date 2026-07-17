#!/usr/bin/env bash
# Tier 2 live check of the GATEWAY PROFILE (runs INSIDE the sandbox — see
# run.sh):
#
#     scripts/tun-sandbox/run.sh /repo/scripts/tun-sandbox/gateway-smoke.sh
#
# engine-smoke.sh proves the engine's tun/killswitch mechanics; this proves
# the container gateway *contract* end to end with the real service code:
#
#   1. `alle gateway init` verifies real privileges (root, /dev/net/tun,
#      CAP_NET_ADMIN) and declares tun + killswitch in state;
#   2. readiness (`alle health`) is red before the daemon exists, and stays
#      red without a viable channel while a namespace process — the joined
#      app's view — gets ZERO direct egress (fail-closed, not half-up);
#   3. an alled-style direct provider-API call is equally blocked: the
#      documented fail-closed control-plane stance (diagnostic, no bypass);
#   4. readiness turns green only once privileges + declared policy + tun
#      interface + sing-box control + a viable channel ALL hold (viability is
#      injected here — a real provider handshake is the Tier 3 live run);
#   5. SIGTERM to the foreground daemon (the container PID-1 shape) reaps
#      sing-box, tears the tun down, and exits within a stop grace period.
#
# Never run this outside the sandbox: auto_route owns whatever network
# namespace it runs in.
set -euo pipefail

. "$(dirname "$0")/lib.sh"

TUN_NAME="alle-tun"
export ALLE_HOME=/tmp/alle-gateway-smoke
export ALLE_GATEWAY=1
export ALLE_SERVICE=1 # exec'd CLI calls must not spawn a second daemon
export PYTHONPATH=/repo/src

rm -rf "$ALLE_HOME"
fetch_singbox
export ALLE_SINGBOX="$SB"
alle() { python3 -m alle "$@"; }

say "gateway init verifies privileges and declares the contract"
alle gateway init
python3 - <<'EOF'
import sys
sys.path.insert(0, "/repo/src")
from alle.state import Store
router = Store.load().router
assert router["tun"] is True and router["killswitch"] is True, router
print("   PASS: tun + killswitch declared in state before any readiness")
EOF

say "readiness red before the daemon/data plane exists"
if alle health >/dev/null 2>&1; then fail "health green with no daemon"; fi
pass "health exits nonzero (compose dependants stay unstarted)"

say "foreground daemon (the container PID-1 shape)"
# Invoke Python directly here instead of backgrounding the `alle` shell
# function: a background function has an intermediate subshell PID, so a
# signal to $! would not exercise the foreground daemon's own handler.
python3 -m alle run >/tmp/alle-run.log 2>&1 &
ALLE_PID=$!
for _ in $(seq 1 60); do
	ip link show "$TUN_NAME" >/dev/null 2>&1 && break
	kill -0 "$ALLE_PID" 2>/dev/null || {
		cat /tmp/alle-run.log >&2
		fail "alle run exited before the tun came up"
	}
	sleep 0.3
done
ip link show "$TUN_NAME" >/dev/null 2>&1 || {
	cat /tmp/alle-run.log >&2
	fail "tun interface never appeared"
}
pass "alle run brought up $TUN_NAME with the declared killswitch"

say "joined-app view: not ready => zero direct egress"
# Interface creation precedes sing-box control readiness and the daemon's
# accepted-generation publication by a few milliseconds. Wait until the only
# remaining readiness failure is channel viability before testing egress.
for _ in $(seq 1 30); do
	health=$(alle health --json 2>/dev/null || true)
	echo "$health" | grep -q '"failing": \["viable_channel"\]' && break
	sleep 0.2
done
if alle health >/dev/null 2>&1; then fail "health green without a viable channel"; fi
health=$(alle health --json 2>/dev/null || true)
echo "$health" | grep -q viable_channel || fail "viable_channel missing from the failing set"
if curl -sf --max-time 5 https://1.1.1.1/cdn-cgi/trace >/dev/null; then
	fail "direct egress possible while the gateway is not ready"
fi
pass "readiness red + unmatched egress rejected (fail-closed)"

say "control-plane stance: alled-style direct egress is policy-blocked too"
if python3 -c 'import urllib.request; urllib.request.urlopen("https://1.1.1.1/cdn-cgi/trace", timeout=5)' 2>/dev/null; then
	fail "a plain python https call escaped the killswitch"
fi
pass "no hidden carve-out: recovery is fail-closed with a named diagnostic"

say "readiness green only when every condition holds"
python3 - <<'EOF'
# Inject a channel with a passing probe in one state write (probe writes do
# not move the config signature; the channel add does, so the daemon
# reconciles it in). A REAL handshaking provider is the Tier 3 live run.
import sys
sys.path.insert(0, "/repo/src")
from alle.state import Store
store = Store.load()
store.add_provider("protonvpn")
wg = {
    "private_key": "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
    "address": ["10.5.0.2/32"],
    "peer": {
        "public_key": "eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXk=",
        "endpoint_host": "192.0.2.10",
        "endpoint_port": 51820,
        "preshared_key": None,
        "allowed_ips": ["0.0.0.0/0"],
        "keepalive": 25,
    },
}
ch = store.add_channel("protonvpn", "US", "", wg)
store.set_probe("protonvpn", ch.id, {"ok": True, "latency_ms": 1})
print(f"   injected viable channel protonvpn/{ch.id}")
EOF
ok=0
for _ in $( # the reconcile restarts sing-box; give it a moment
	seq 1 20
); do
	if alle health >/dev/null 2>&1; then
		ok=1
		break
	fi
	sleep 1
done
[ "$ok" = "1" ] || {
	alle health --json >&2 || true
	cat /tmp/alle-run.log >&2
	fail "health never went green with a viable channel"
}
pass "gateway ready: privileges + policy + interface + control + viable channel"

say "SIGTERM teardown within the stop grace period"
kill -TERM "$ALLE_PID"
for _ in $(seq 1 20); do
	kill -0 "$ALLE_PID" 2>/dev/null || break
	sleep 0.5
done
kill -0 "$ALLE_PID" 2>/dev/null && fail "daemon still alive 10s after SIGTERM"
wait "$ALLE_PID" 2>/dev/null || true
pgrep -x sing-box >/dev/null 2>&1 && fail "sing-box was not reaped"
if ip link show "$TUN_NAME" >/dev/null 2>&1; then
	cat /tmp/alle-run.log >&2
	ip -details link show "$TUN_NAME" >&2 || true
	fail "$TUN_NAME still exists after teardown"
fi
grep -q "data plane released" /tmp/alle-run.log || fail "teardown log line missing"
pass "sing-box reaped, tun removed, clean foreground exit"

say "ALL GATEWAY SMOKE CHECKS PASSED"
