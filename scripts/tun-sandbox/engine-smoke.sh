#!/usr/bin/env bash
# Tier 2 live check of the ENGINE-GENERATED tun config (runs INSIDE the
# sandbox container — see run.sh):
#
#     scripts/tun-sandbox/run.sh /repo/scripts/tun-sandbox/engine-smoke.sh
#
# smoke.sh proves the hand-written design assumptions; this proves what
# alle.engine actually emits for TUN mode drives a live tun:
#
#   1. the generated config passes sing-box check;
#   2. tun + auto_route + strict_route seizes the namespace's routing and
#      traffic still flows (loop safety with strict_route ON, which smoke.sh
#      did not cover);
#   3. DNS hijack answers a query sent to a foreign resolver by resolving
#      through the configured upstream;
#   4. the SAME rule table serves both doors: a block rule rejects through
#      the tun and through the router proxy port, while neighbors flow;
#   5. flipping killswitch in state regenerates a catch-all reject that
#      blocks everything on both doors;
#   6. killing sing-box restores the namespace completely.
#
# Never run this outside the sandbox: auto_route owns whatever network
# namespace it runs in.
set -euo pipefail

# shellcheck source=/dev/null
. "$(dirname "$0")/lib.sh"

TUN_NAME="alle-tun" # engine's fixed non-darwin interface_name
ROUTER_PORT=41080
SB_LOG=/tmp/sing-box.log
export ALLE_HOME=/tmp/alle-engine-smoke

fetch_singbox

# ---- generate the configs straight from alle.engine
say "engine-generated configs (tun on; killswitch off, then on)"
python3 - <<EOF
import json, sys
sys.path.insert(0, "/repo/src")
from alle.engine import Engine
from alle.state import Store

def build(killswitch):
    data = {
        "version": 1,
        "providers": {},
        "router": {
            "port": ${ROUTER_PORT},
            "killswitch": killswitch,
            "lan_direct": True,
            "tun": True,
            "rules": [
                {"id": "r1", "type": "ip_cidr", "value": "1.0.0.1/32", "target": "block"},
            ],
        },
    }
    config, errors = Engine(Store(data=data))._build_config()
    assert not errors, errors
    return config

json.dump(build(False), open("/tmp/engine-tun.json", "w"), indent=2)
json.dump(build(True), open("/tmp/engine-tun-ks.json", "w"), indent=2)
EOF
"$SB" check -c /tmp/engine-tun.json || fail "engine tun config rejected by sing-box check"
"$SB" check -c /tmp/engine-tun-ks.json || fail "engine killswitch config rejected"
pass "both engine configs pass sing-box check"

# ---- baseline
say "baseline connectivity (pre-tun)"
curl -sSf --max-time 10 https://1.1.1.1/cdn-cgi/trace >/dev/null || fail "no baseline network in container"
pass "IP-literal HTTPS works with no tun"

# ---- phase A: live tun from the engine config (killswitch off)
say "phase A: engine config drives a live tun (strict_route on)"
start_singbox /tmp/engine-tun.json

# Seizure signal depends on the mode the engine emits. Pure auto_route puts
# the default route on the tun; auto_redirect (which the engine now emits on
# Linux) instead installs nftables + fwmark policy rules and deliberately
# leaves the route table pointing at the physical NIC — that is the whole
# point ("better than tproxy"). Accept either; the connectivity + per-rule
# tests below are the real proof traffic traverses sing-box.
if ip route get 9.8.7.6 2>/dev/null | grep -q "$TUN_NAME"; then
	pass "auto_route active: foreign IPs route via $TUN_NAME"
elif ip rule 2>/dev/null | grep -qE "fwmark 0x20(23|24)"; then
	pass "auto_redirect active: fwmark policy rules redirect traffic through sing-box"
else
	ip rule >&2 2>/dev/null || true
	fail "neither auto_route (route via $TUN_NAME) nor auto_redirect (fwmark rules) is active"
fi

curl -sSf --max-time 15 https://1.1.1.1/cdn-cgi/trace >/dev/null ||
	{
		cat "$SB_LOG" >&2
		fail "traffic through tun failed (strict_route loop safety?)"
	}
pass "traffic flows tun -> sing-box -> direct with strict_route ON"

got=$(dig +short +time=4 +tries=1 @9.9.9.9 one.one.one.one A || true)
echo "$got" | grep -qE '^1\.(1\.1\.1|0\.0\.1)$' ||
	{
		cat "$SB_LOG" >&2
		fail "DNS hijack missed or upstream resolution failed (got: '$got')"
	}
pass "DNS hijack: foreign-resolver query answered via the configured upstream"

if curl -sf --max-time 5 https://1.0.0.1/cdn-cgi/trace >/dev/null; then
	fail "block rule for 1.0.0.1/32 did not reject through the tun"
fi
curl -sSf --max-time 15 https://1.1.1.1/cdn-cgi/trace >/dev/null || fail "neighbor IP wrongly blocked"
pass "user block rule rejects through the tun; neighbor flows"

if curl -sf --max-time 5 --proxy socks5h://127.0.0.1:${ROUTER_PORT} https://1.0.0.1/cdn-cgi/trace >/dev/null; then
	fail "block rule did not reject through the router entrypoint"
fi
curl -sSf --max-time 15 --proxy socks5h://127.0.0.1:${ROUTER_PORT} https://1.1.1.1/cdn-cgi/trace >/dev/null ||
	fail "router entrypoint broken while tun is up"
pass "same rule table serves both doors (tun + router proxy port)"
stop_singbox

# ---- phase B: killswitch flip -> system-wide catch-all reject
say "phase B: killswitch on (engine-regenerated config)"
start_singbox /tmp/engine-tun-ks.json
if curl -sf --max-time 5 https://1.1.1.1/cdn-cgi/trace >/dev/null; then
	fail "killswitch did not block tun traffic"
fi
if curl -sf --max-time 5 --proxy socks5h://127.0.0.1:${ROUTER_PORT} https://1.1.1.1/cdn-cgi/trace >/dev/null; then
	fail "killswitch did not block the router entrypoint"
fi
pass "killswitch blocks unmatched traffic on both doors"

# loop safety: the loopback contract ports (control API, Web UI, channel
# proxies) must stay reachable even with tun + strict_route + killswitch —
# loopback traffic never enters the tun.
python3 -m http.server 8901 --bind 127.0.0.1 >/dev/null 2>&1 &
HTTP_PID=$!
sleep 0.7
curl -sSf --max-time 5 http://127.0.0.1:8901/ >/dev/null ||
	fail "loopback service unreachable under tun+strict_route+killswitch"
kill "$HTTP_PID" 2>/dev/null || true
CLASH=$(python3 -c 'import json; print(json.load(open("/tmp/engine-tun-ks.json"))["experimental"]["clash_api"]["external_controller"])')
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$CLASH/" || true)
[ "$code" != "000" ] || fail "clash api loopback port unreachable under killswitch"
pass "loopback contract ports bypass the tun (plain listener + clash api)"

# loop safety finding to document: an ordinary process's DIRECT outbound
# call (the daemon's provider API calls are exactly this) IS subject to the
# killswitch — only sing-box's own sockets bypass the tun.
if python3 -c 'import urllib.request; urllib.request.urlopen("https://1.1.1.1/cdn-cgi/trace", timeout=5)' 2>/dev/null; then
	fail "expected a plain python https call to be blocked under killswitch"
fi
pass "confirmed: non-sing-box direct egress (e.g. provider API calls) is killswitched"
stop_singbox

# ---- teardown
say "teardown restores the namespace"
sleep 0.5
ip link show "$TUN_NAME" >/dev/null 2>&1 && fail "$TUN_NAME still exists after sing-box exit"
curl -sSf --max-time 10 https://1.1.1.1/cdn-cgi/trace >/dev/null || fail "network not restored after teardown"
pass "tun, routes, and connectivity all restored by killing sing-box"

say "ALL ENGINE SMOKE CHECKS PASSED"
