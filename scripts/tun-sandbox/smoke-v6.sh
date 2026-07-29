#!/usr/bin/env bash
# Tier 2 live-tun IPv6 smoke (runs INSIDE the sandbox container — see run.sh).
#
# The IPv6 guard *shapes* are unit-pinned (tests/test_engine.py). This proves
# the real pinned sing-box honours them at run time, on a real tun, against a
# config the real engine compiled:
#
#   1. auto_route seizes IPv6 too — ::/0 routes via the TUN device;
#   2. a rule targeting a v4-only channel drops its v6 traffic (the
#      `ip_version: 6` guard) and NOTHING reaches the egress interface;
#   3. the guard is selective: v6 for a v6-capable channel is routed to that
#      channel's outbound instead of being swallowed;
#   4. v6 matching no rule at all hits the trailing ::/0 reject (fail closed);
#   5. loop safety still holds with v6 rules present — sing-box's own sockets
#      bypass its tun and v4 traffic keeps flowing.
#
# Docker Desktop disables IPv6 on eth0 outright, so the smoke builds its own
# observable egress: a dummy `v6wan` holding the default v6 route, with a
# static neighbour so the kernel actually transmits rather than waiting on NDP.
# A leak is therefore physically possible — and step 0 proves the detector
# sees one before any zero from it is believed.
#
# The sandbox has no upstream IPv6, so "v6 reaches the public internet" is not
# provable here — that half is covered on the explicit-proxy surface with a
# real fleet. What IS provable, and is what this file exists for: which
# destinations sing-box emits packets for, and which it silently drops.
#
# Never run this outside the sandbox: auto_route owns whatever network
# namespace it runs in.
set -euo pipefail

# shellcheck source=/dev/null
. "$(dirname "$0")/lib.sh"

TUN_NAME="sbtun0"
SB_LOG=/tmp/sing-box-v6.log
LEAK_LOG=/tmp/v6-leak.txt
BASE_LOG=/tmp/v6-leak-baseline.txt
V6WAN=v6wan

V6_OK=2001:db8:a::1        # inside the v6-capable channel's rule
V6_GUARDED=2001:db8:b::1   # inside the v4-only channel's rule — must be dropped
V6_UNMATCHED=2001:db8:c::1 # matches no user rule — must hit the ::/0 reject

fetch_singbox

# ---- step 0: build an egress a leak could use, and prove leaks are visible ---
say "stand up an observable IPv6 egress"
ip link add "$V6WAN" type dummy
ip link set "$V6WAN" up
ip -6 addr add fd00:5117::2/64 dev "$V6WAN"
ip -6 neigh add fd00:5117::1 lladdr 02:00:00:00:00:01 dev "$V6WAN" nud permanent
ip -6 route replace default via fd00:5117::1 dev "$V6WAN"
ip -6 route show default | grep -q "$V6WAN" || fail "no v6 default route on $V6WAN"
pass "default v6 route via $V6WAN (this is where a leak would surface)"

say "baseline: with no sing-box, v6 DOES escape (the detector works)"
tcpdump -i "$V6WAN" -n -l ip6 >"$BASE_LOG" 2>/dev/null &
BASE_PID=$!
sleep 1
curl -sf --max-time 3 -o /dev/null "http://[$V6_GUARDED]:80/" 2>/dev/null || true
sleep 1
kill $BASE_PID 2>/dev/null || true
wait $BASE_PID 2>/dev/null || true
baseline=$(grep -c "$V6_GUARDED" "$BASE_LOG" || true)
[ "$baseline" -gt 0 ] || fail "leak detector saw nothing even with no tun — it proves nothing"
pass "$baseline packet(s) escaped without sing-box: a later zero means something"

# ---- compile the config with the real engine --------------------------------
say "compile a mixed v6-capable / v4-only fleet with the real engine"
python3 - <<PY || fail "engine could not compile the fleet"
import copy, json, os, sys, tempfile
os.environ["ALLE_HOME"] = tempfile.mkdtemp(prefix="v6-smoke-")
sys.path.insert(0, "/repo/src")
from alle.engine import Engine
from alle.state import Store

def wg(v6):
    return {
        "private_key": "PRIV=", "address": ["10.5.0.2/32"] + (["2a07:b944::2:2/128"] if v6 else []),
        "peer": {"public_key": "PUB=", "endpoint_host": "198.51.100.10",
                 "endpoint_port": 51820, "preshared_key": None,
                 "allowed_ips": ["0.0.0.0/0", "::/0"], "keepalive": 25},
    }

store = Store.load()
store.add_provider("protonvpn"); store.add_provider("nordvpn")
capable = store.add_channel("protonvpn", "Japan", "", wg(True))
v4only = store.add_channel("nordvpn", "United States", "", wg(False))
store.ensure_router_port()
store = Store.load()
store.create_ruleset("V6ok", f"protonvpn/{capable.id}", [("ip_cidr", "2001:db8:a::/48")])
store.create_ruleset("V4only", f"nordvpn/{v4only.id}", [("ip_cidr", "2001:db8:b::/48")])
store.set_tun(True)
cfg, errors = Engine(Store.load())._build_config()
if errors:
    raise SystemExit(f"compile errors: {errors}")

# The route rules — the thing under test — are used verbatim. Only the
# WireGuard *endpoints* become direct outbounds of the same tag: the sandbox
# has no peers and no real keys, and what this smoke observes is the routing
# decision, not a tunnel. Every rule still names the same outbound tag.
# bind_interface pins them to the observable egress. Without it sing-box
# auto-detects eth0 — where Docker Desktop disables IPv6 — so every v6 dial
# would die in the socket layer and "no packets escaped" would be true no
# matter what the route rules said. (Verified: with the guard deleted, the
# leak check still passed until this was added.)
cfg["outbounds"] = cfg["outbounds"] + [
    {"type": "direct", "tag": e["tag"]}
    for e in cfg.pop("endpoints", [])
    if e.get("type") == "wireguard"
]
tun = next(i for i in cfg["inbounds"] if i["type"] == "tun")
tun["interface_name"] = "${TUN_NAME}"
tun["auto_route"] = True
cfg["log"] = {"level": "info"}  # routing decisions are the evidence here

# Phase B runs the config as compiled, with the plain direct outbound still
# on the real interface — the v4 loop-safety check needs it there.
json.dump(cfg, open("/tmp/tun-v6-b.json", "w"), indent=2)

# Phase A pins every outbound, route.final's direct one included, to the
# observable egress, so a v6 packet leaving by ANY path is visible.
phase_a = copy.deepcopy(cfg)
for out in phase_a["outbounds"]:
    out["bind_interface"] = "${V6WAN}"
json.dump(phase_a, open("/tmp/tun-v6.json", "w"), indent=2)

rules = [r for r in cfg["route"]["rules"] if "ip_cidr" in r]
guard = [r for r in rules if r.get("ip_version") == 6 and r.get("action") == "reject"]
if not guard:
    raise SystemExit("engine emitted no ip_version:6 guard — nothing to verify")
print(f"   compiled {len(cfg['route']['rules'])} route rules, {len(guard)} v6 guard(s)")
PY
pass "engine compiled a tun config carrying the v6 guards"

"$SB" check -c /tmp/tun-v6.json ||
	fail "compiled config rejected by sing-box check"
pass "compiled config passes sing-box check"

# ---- run it -----------------------------------------------------------------
say "start sing-box on the compiled config"
start_singbox /tmp/tun-v6.json

# auto_route installs the v6 default in a policy table selected by fwmark, so
# `ip -6 route get` (which carries no mark) would answer from the main table
# and prove nothing. Assert the route exists; the capture itself is proven
# behaviourally below — packets that reach the tun cannot also reach $V6WAN.
ip -6 route show table all | grep -qE "^default .*dev $TUN_NAME" ||
	{
		ip -6 route show table all >&2
		fail "auto_route installed no IPv6 default via $TUN_NAME"
	}
pass "auto_route installed an IPv6 default route via $TUN_NAME"

# Watch the physical interface for ANY IPv6 leaving the namespace.
tcpdump -i "$V6WAN" -n -l ip6 >"$LEAK_LOG" 2>/dev/null &
TCPDUMP_PID=$!
trap 'kill $TCPDUMP_PID 2>/dev/null || true' EXIT
sleep 1

probe() { curl -sf --max-time 4 -o /dev/null "http://[$1]:80/" 2>/dev/null || true; }

# One pass over all three destinations, then read what sing-box did with each.
for dest in "$V6_OK" "$V6_GUARDED" "$V6_UNMATCHED"; do probe "$dest"; done
sleep 1
kill $TCPDUMP_PID 2>/dev/null || true
wait $TCPDUMP_PID 2>/dev/null || true

emitted() { grep -c "$1" "$LEAK_LOG" 2>/dev/null || true; }
# sing-box logs every accepted connection as inbound/tun; only a connection it
# actually routes gets a matching outbound/... line naming the chosen outbound.
routed_to() { grep -E "outbound/.*\[$2\].*\[$1\]" "$SB_LOG" >/dev/null 2>&1; }
saw_inbound() { grep -E "inbound/tun.*\[$1\]" "$SB_LOG" >/dev/null 2>&1; }

say "v6 for a v6-capable channel is carried to that channel's outbound"
saw_inbound "$V6_OK" || {
	tail -20 "$SB_LOG" >&2
	fail "sing-box never saw $V6_OK"
}
routed_to "$V6_OK" "out-protonvpn-[^]]*" || {
	grep "$V6_OK" "$SB_LOG" >&2
	fail "$V6_OK was not routed to the v6-capable channel's outbound"
}
ok_packets=$(emitted "$V6_OK")
[ "$ok_packets" -gt 0 ] || fail "$V6_OK produced no packets on $V6WAN — nothing was carried"
pass "$V6_OK routed to out-protonvpn-* and emitted $ok_packets packet(s)"

say "v6 toward a v4-only channel is dropped by the ip_version:6 guard"
saw_inbound "$V6_GUARDED" || fail "sing-box never saw $V6_GUARDED — the tun did not capture it"
if routed_to "$V6_GUARDED" "out-nordvpn-[^]]*"; then
	fail "$V6_GUARDED reached the v4-only channel's outbound — the guard did not fire"
fi
guarded_packets=$(emitted "$V6_GUARDED")
[ "$guarded_packets" = "0" ] || {
	grep "$V6_GUARDED" "$LEAK_LOG" | head >&2
	fail "$guarded_packets packet(s) for $V6_GUARDED escaped to $V6WAN"
}
pass "$V6_GUARDED captured by the tun, routed nowhere, 0 packets escaped"

say "v6 matching no rule hits the trailing ::/0 reject"
saw_inbound "$V6_UNMATCHED" || fail "sing-box never saw $V6_UNMATCHED"
unmatched_packets=$(emitted "$V6_UNMATCHED")
[ "$unmatched_packets" = "0" ] || {
	grep "$V6_UNMATCHED" "$LEAK_LOG" | head >&2
	fail "$unmatched_packets packet(s) for $V6_UNMATCHED escaped to $V6WAN"
}
pass "$V6_UNMATCHED captured by the tun, 0 packets escaped (baseline was $baseline)"

stop_singbox

# ---- phase B: the same rules, outbounds back on the real interface ----------
say "loop safety holds with v6 rules present"
# Wait for the previous instance to release the tun and its listening ports;
# start_singbox only waits for the device to *appear*, not to be free.
for _ in $(seq 1 40); do
	ip link show "$TUN_NAME" >/dev/null 2>&1 || break
	sleep 0.25
done
start_singbox /tmp/tun-v6-b.json
curl -sSf --max-time 15 https://1.1.1.1/cdn-cgi/trace >/dev/null ||
	{
		tail -30 "$SB_LOG" >&2
		fail "v4 traffic through the tun broke (loop safety?)"
	}
pass "v4 still flows tun -> sing-box -> direct (sing-box's own sockets bypass its tun)"

stop_singbox

say "teardown restores the namespace"
sleep 0.5
ip link show "$TUN_NAME" >/dev/null 2>&1 && fail "$TUN_NAME still exists after sing-box exit"
curl -sSf --max-time 10 https://1.1.1.1/cdn-cgi/trace >/dev/null || fail "network not restored"
pass "tun and connectivity restored by killing sing-box"

say "ALL IPv6 SMOKE CHECKS PASSED"
