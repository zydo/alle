#!/usr/bin/env bash
# Tier 3 (Darwin, system-wide) IPv6 tun verification — runs INSIDE the guest.
#
# Tier 2 proved the guards against a hand-assembled config in a Linux netns.
# This drives the real CLI on real macOS: `alle tun on` creates a real utun,
# seizes the system's routes, and the fleet is two imported WireGuard .conf
# files — one with a global v6 interface address (v6-capable) and one without
# (the same provider's v4-only servers).
#
# WireGuard encapsulation is what makes the routing decision observable here:
#   - routed into a channel  -> UDP to that channel's endpoint on the physical
#                               interface (the packet left, wrapped);
#   - dropped by a guard     -> nothing at all;
#   - leaked to direct       -> raw IPv6 to the destination on the physical
#                               interface.
# The peers are unreachable by design; a handshake is not needed to observe
# which outbound sing-box chose.
set -euo pipefail

say() { printf '\n== %s\n' "$*"; }
pass() { printf '   PASS: %s\n' "$*"; }
fail() {
	printf '   FAIL: %s\n' "$*" >&2
	exit 1
}

V6_OK=2001:db8:a::1
V6_GUARDED=2001:db8:b::1
V6_UNMATCHED=2001:db8:c::1
PHYS=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' || true)
CAP_LOG=/tmp/tier3-v6.pcap.txt
[ -n "$PHYS" ] || fail "no default interface in the guest"

# ---- start from a known-clean state ------------------------------------------
# A tun left up by an earlier run routes ::/0 into a dead sing-box, which would
# make the baseline below silently measure nothing. Note the binary is
# version-suffixed (sing-box@1.13.13), so `pkill -x sing-box` does not match it.
sudo ALLE_HOME="$HOME/.alle" alle tun off >/dev/null 2>&1 || true
alle stop >/dev/null 2>&1 || true
# Root writes preserve file owners (fsio._preserve_owner) but cannot restore an
# ~/.alle that root itself created; keep the state dir owned by the user.
[ -d "$HOME/.alle" ] && sudo chown -R "$(id -un):$(id -gn)" "$HOME/.alle" 2>/dev/null || true
# Kill orphaned appliers BEFORE sing-box: supervision in a stray applier
# restarts sing-box within a second, so killing sing-box alone never sticks.
pkill -f "alle applier" 2>/dev/null || true
sudo pkill -f "alle applier" 2>/dev/null || true
sudo pkill -f "sing-box@" 2>/dev/null || true
for _ in $(seq 1 20); do
	ifconfig utun225 >/dev/null 2>&1 || break
	sleep 0.5
done
ifconfig utun225 >/dev/null 2>&1 && {
	echo "   a previous utun225 could not be cleared" >&2
	exit 1
}

say "guest facts"
echo "   macOS $(sw_vers -productVersion) / $(uname -m); physical iface $PHYS"
alle version

# ---- make an IPv6 leak observable -------------------------------------------
say "confirm a v6 leak would be visible on $PHYS"
# The guest's vmnet NAT carries real IPv6, so the egress a leak would take
# already exists — nothing to fabricate. The baseline below proves it.
route -n get -inet6 default >/dev/null 2>&1 || fail "no v6 default route in the guest"
pass "v6 default route present on $PHYS ($(route -n get -inet6 default | awk '/gateway/{print $2}'))"

# shellcheck disable=SC2024  # the redirect is the (admin) shell's, and /tmp is
# admin-writable; only tcpdump itself needs root.
capture() {
	sudo tcpdump -i "$PHYS" -n -l "$1" >"$CAP_LOG" 2>/dev/null &
	echo $!
}
probe() { curl -sf --max-time 4 -o /dev/null "http://[$1]:80/" 2>/dev/null || true; }

say "baseline: with no tun, v6 escapes (the detector works)"
TP=$(capture "ip6 and host $V6_GUARDED")
sleep 1
probe "$V6_GUARDED"
sleep 1
sudo kill "$TP" 2>/dev/null || true
baseline=$(grep -c "$V6_GUARDED" "$CAP_LOG" 2>/dev/null || true)
[ "$baseline" -gt 0 ] || fail "leak detector saw nothing with no tun — it proves nothing"
pass "$baseline packet(s) escaped with no tun: a later zero means something"

# ---- build the fleet through the real CLI ------------------------------------
say "import a v6-capable and a v4-only channel from .conf files"
alle stop >/dev/null 2>&1 || true
alle providers add protonvpn >/dev/null 2>&1 || true
alle channels add protonvpn --config /tmp/wg-JP-01.conf >/dev/null
alle channels add protonvpn --config /tmp/wg-US-02.conf >/dev/null
alle channels ls | sed 's/^/   /'
alle channels ls --json | python3 -c '
import json,sys
chans={c["name"]: c["ipv6"] for c in json.load(sys.stdin)["channels"]}
assert chans.get("wg_jp_01") is True, f"wg_jp_01 not v6-capable: {chans}"
assert chans.get("wg_us_02") is False, f"wg_us_02 unexpectedly v6-capable: {chans}"
print("   capability: wg_jp_01 v6=True, wg_us_02 v6=False")
'
pass "per-channel v6 capability resolved from the .conf addresses"

say "route the two v6 test prefixes at the two channels"
alle routes ruleset create V6ok --via protonvpn/wg_jp_01 --cidr 2001:db8:a::/48 >/dev/null
alle routes ruleset create V4only --via protonvpn/wg_us_02 --cidr 2001:db8:b::/48 >/dev/null
pass "rulesets created"

# ---- activate the real tun, system-wide --------------------------------------
say "sudo alle tun on --trial 180 (real utun, real route seizure)"
# CLI mutations auto-start the daemon, and the privilege gate correctly
# refuses tun activation while an admin-owned daemon is running (runbook).
alle stop >/dev/null 2>&1 || true
sudo ALLE_HOME="$HOME/.alle" alle tun on --trial 180 || fail "tun activation refused"
trap 'sudo ALLE_HOME="$HOME/.alle" alle tun off >/dev/null 2>&1 || true' EXIT

# The engine emits utun225 on Darwin (engine._tun_interface_name). Note the
# `|| true`: under `set -e` a grep miss inside a command substitution fails
# the assignment and kills the script mid-activation.
UTUN=utun225
for _ in $(seq 1 60); do
	ifconfig "$UTUN" >/dev/null 2>&1 && break
	sleep 0.5
done
ifconfig "$UTUN" >/dev/null 2>&1 || {
	ifconfig -l >&2
	tail -20 ~/.alle/alle.log >&2
	fail "alle's $UTUN never appeared"
}
pass "alle created $UTUN"

route -n get -inet6 "$V6_GUARDED" 2>/dev/null | grep -q "$UTUN" ||
	route -n get -inet6 default 2>/dev/null | grep -q "$UTUN" ||
	{
		netstat -rn -f inet6 | head -15 >&2
		fail "the tun did not seize IPv6 routing"
	}
pass "IPv6 routing seized by $UTUN"

# ---- what does each destination actually do? ---------------------------------
# What Tier 3 can and cannot observe, stated plainly.
#
# The peers are unreachable by design, so no tunnel ever completes a
# handshake: both channels emit 148-byte handshake initiations forever,
# regardless of traffic, and traffic routed INTO a channel is queued rather
# than sent. So "which channel did this go to" is NOT observable here — that
# discrimination belongs to the sandbox tier, which swaps the endpoints for
# direct outbounds precisely so the choice becomes visible.
#
# What IS observable, and is the property with security consequences: whether
# any v6 escapes the machine unencapsulated. The guest has real upstream IPv6
# and a real default route, so a rule table that lets v6 fall through to
# `direct` really does put the guest's address on the wire — the baseline
# above proves those packets are visible when the tun is down.
probe_watch() { # $1 = destination
	# shellcheck disable=SC2024  # see capture() above
	sudo tcpdump -i "$PHYS" -n -l "ip6" >"$CAP_LOG" 2>/dev/null &
	local tp=$!
	sleep 1
	probe "$1"
	sleep 2
	sudo kill "$tp" 2>/dev/null || true
	sleep 0.3
}
raw_for() { grep -c "$1" "$CAP_LOG" 2>/dev/null || true; }

say "no v6 destination escapes unencapsulated while the tun is up"
for dest in "$V6_OK" "$V6_GUARDED" "$V6_UNMATCHED"; do
	route -n get -inet6 "$dest" 2>/dev/null | grep -q "$UTUN" ||
		fail "$dest does not route via $UTUN — the tun did not capture it"
	probe_watch "$dest"
	escaped=$(raw_for "$dest")
	[ "$escaped" = "0" ] || {
		grep "$dest" "$CAP_LOG" | head -3 >&2
		fail "$escaped raw IPv6 packet(s) for $dest reached $PHYS — v6 bypassed the tunnel"
	}
	printf '   %s: 0 raw IPv6 on %s\n' "$dest" "$PHYS"
done
pass "all three destinations captured by the tun (baseline with tun down: $baseline)"

# ---- teardown ----------------------------------------------------------------
say "alle tun off restores the system"
sudo ALLE_HOME="$HOME/.alle" alle tun off || fail "tun off failed"
trap - EXIT
# Teardown is a reconcile, not a synchronous unplug: the daemon rewrites the
# config and restarts sing-box without the tun, which takes a beat. Poll.
for _ in $(seq 1 30); do
	ifconfig "$UTUN" >/dev/null 2>&1 || break
	sleep 1
done
ifconfig "$UTUN" >/dev/null 2>&1 && fail "$UTUN still present 30s after tun off"
route -n get default 2>/dev/null | grep -q "interface: $PHYS" ||
	fail "default route did not return to $PHYS"
pass "utun gone, default route back on $PHYS"

say "ALL TIER 3 IPv6 CHECKS PASSED"
