#!/usr/bin/env bash
# Tier 2 live-tun smoke (runs INSIDE the sandbox container — see run.sh).
#
# Proves the TUN-mode design assumptions against the real pinned sing-box in
# an isolated network namespace:
#
#   1. tun inbound + auto_route seizes the namespace's routing (policy route
#      via the TUN device);
#   2. traffic still flows tun -> sing-box -> direct -> eth0 (loop safety:
#      sing-box's own sockets bypass its tun);
#   3. DNS hijack intercepts a query addressed to a *foreign* resolver and
#      answers from sing-box's own DNS (predefined hosts entry);
#   4. route rules match packet traffic per-rule (one /32 rejected, its
#      neighbor still reachable);
#   5. a catch-all reject (the kill-switch shape) blocks everything;
#   6. killing sing-box restores the namespace's network completely.
#
# Never run this outside the sandbox: auto_route owns whatever network
# namespace it runs in.
set -euo pipefail

# shellcheck source=/dev/null
. "$(dirname "$0")/lib.sh"

TUN_NAME="sbtun0"
SB_LOG=/tmp/sing-box.log

fetch_singbox

# ---- baseline: the namespace has plain connectivity before any tun
say "baseline connectivity (pre-tun)"
curl -sSf --max-time 10 https://1.1.1.1/cdn-cgi/trace >/dev/null || fail "no baseline network in container"
pass "IP-literal HTTPS works with no tun"

# ---- phase A: tun + auto_route + DNS hijack + per-rule matching
say "phase A: tun + auto_route + DNS hijack + rule matching"
cat >/tmp/tun-a.json <<EOF
{
  "log": {"level": "warn"},
  "dns": {
    "servers": [
      {"type": "udp", "tag": "dns-remote", "server": "1.1.1.1"},
      {"type": "hosts", "tag": "dns-hosts", "predefined": {"smoke.test.invalid": ["198.51.100.42"]}}
    ],
    "rules": [
      {"domain": ["smoke.test.invalid"], "server": "dns-hosts"}
    ],
    "strategy": "ipv4_only"
  },
  "inbounds": [
    {
      "type": "tun",
      "tag": "tun-in",
      "interface_name": "${TUN_NAME}",
      "address": ["172.19.0.1/30"],
      "mtu": 1400,
      "auto_route": true,
      "strict_route": false,
      "stack": "system"
    }
  ],
  "outbounds": [{"type": "direct", "tag": "direct"}],
  "route": {
    "rules": [
      {"inbound": ["tun-in"], "action": "sniff"},
      {"protocol": "dns", "action": "hijack-dns"},
      {"inbound": ["tun-in"], "ip_cidr": ["1.0.0.1/32"], "action": "reject"}
    ],
    "final": "direct",
    "auto_detect_interface": true,
    "default_domain_resolver": "dns-remote"
  }
}
EOF
"$SB" check -c /tmp/tun-a.json || fail "phase A config rejected by sing-box check"
pass "config passes sing-box check"
start_singbox /tmp/tun-a.json

ip route get 9.8.7.6 2>/dev/null | grep -q "$TUN_NAME" || fail "default-bound traffic not routed via $TUN_NAME"
pass "auto_route active: foreign IPs route via $TUN_NAME"

curl -sSf --max-time 15 https://1.1.1.1/cdn-cgi/trace >/dev/null ||
	{
		cat "$SB_LOG" >&2
		fail "traffic through tun failed (loop-safety broken?)"
	}
pass "traffic flows tun -> sing-box -> direct (loop safety holds)"

got=$(dig +short +time=4 +tries=1 @9.9.9.9 smoke.test.invalid A || true)
[ "$got" = "198.51.100.42" ] || {
	cat "$SB_LOG" >&2
	fail "DNS hijack missed (got: '$got')"
}
pass "DNS hijack: query to a foreign resolver answered by sing-box hosts entry"

if curl -sf --max-time 5 https://1.0.0.1/cdn-cgi/trace >/dev/null; then
	fail "reject rule for 1.0.0.1/32 did not block"
fi
curl -sSf --max-time 15 https://1.1.1.1/cdn-cgi/trace >/dev/null || fail "neighbor IP wrongly blocked"
pass "per-rule matching on packets: 1.0.0.1/32 rejected, 1.1.1.1 still flows"
stop_singbox

# ---- phase B: kill-switch shape (catch-all reject on the tun)
say "phase B: kill-switch (catch-all reject)"
python3 - <<'EOF'
import json
cfg = json.load(open("/tmp/tun-a.json"))
cfg["route"]["rules"] = [
    {"inbound": ["tun-in"], "action": "sniff"},
    {"protocol": "dns", "action": "hijack-dns"},
    {"inbound": ["tun-in"], "action": "reject"},
]
json.dump(cfg, open("/tmp/tun-b.json", "w"), indent=2)
EOF
"$SB" check -c /tmp/tun-b.json || fail "phase B config rejected"
start_singbox /tmp/tun-b.json
if curl -sf --max-time 5 https://1.1.1.1/cdn-cgi/trace >/dev/null; then
	fail "kill-switch reject did not block tun traffic"
fi
pass "catch-all reject blocks everything through the tun"
stop_singbox

# ---- teardown: killing sing-box must restore the namespace completely
say "teardown restores the namespace"
sleep 0.5
ip link show "$TUN_NAME" >/dev/null 2>&1 && fail "$TUN_NAME still exists after sing-box exit"
ip route get 9.8.7.6 2>/dev/null | grep -q "$TUN_NAME" && fail "policy route still points at dead tun"
curl -sSf --max-time 10 https://1.1.1.1/cdn-cgi/trace >/dev/null || fail "network not restored after teardown"
pass "tun, routes, and connectivity all restored by killing sing-box"

say "ALL SMOKE CHECKS PASSED"
