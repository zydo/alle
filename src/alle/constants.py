"""Shared constants for the single sing-box process alle runs."""

from __future__ import annotations

# Pinned sing-box release. alle always uses exactly this build, downloaded
# from upstream into ~/.alle/bin/sing-box@<version> and verified against the
# checksums below — never any other sing-box that happens to be on PATH.
SINGBOX_VERSION = "1.13.19"

# SHA256 of the *extracted sing-box binary* (not the tarball) for each supported
# platform. Upstream publishes no checksum file, so we pin the bytes ourselves
# (see THIRD_PARTY_NOTICES.md). Pinning the binary lets us verify both a fresh
# download and the on-disk file on every start. Regenerate on a version bump with
# scripts/gen_singbox_checksums.py.
SINGBOX_SHA256 = {
    "darwin-amd64": "078164e43464f2282ae526151411320582c3e60a0294cec24a627edf205305a6",
    "darwin-arm64": "5b75c1dec19488675f725adc7a6e3a7301a553117af835dc47669b1fa918976b",
    "linux-amd64": "7e9dcd7239c49478a576d79f272751e5ed1c2aba7cc08ab1b2bd69c00c904ba1",
    "linux-arm64": "0bd9f22cd677d7fe70324944b3dfaf967971607ac3f713d1b754248d8b0d702d",
}

# The Clash API address/secret are not constants: they are generated per
# installation (see singbox.clash_api) so the API is authenticated and two
# users on one machine don't fight over a hard-coded port.

# Tag prefixes inside the generated sing-box config, so a channel id maps to its
# inbound/outbound deterministically.
INBOUND_PREFIX = "in-"
OUTBOUND_PREFIX = "out-"

# The always-on router entrypoint's inbound tag. Cannot collide with channel
# tags (in-<provider>-<id>): provider keys are registry words, and tag_to_ref
# ignores two-part tags, so stats/metrics never mistake it for a channel.
ROUTER_INBOUND_TAG = "in-router"

# The OS-level tun inbound. Two-part tag, same collision-safety as in-router.
TUN_INBOUND_TAG = "in-tun"
# Interface address for the TUN device. /30 in a range nothing routable uses;
# validated live in the Tier 2 sandbox.
TUN_ADDRESS = "172.19.0.1/30"  # noqa: S1313
# IPv6 prefix for the TUN device (a ULA /126). Its purpose is the LEAK FIX,
# not IPv6 transport: giving the tun a v6 address makes auto_route seize the
# IPv6 default route too, so IPv6 traffic enters the tun instead of bypassing
# the VPN out the physical interface. Once captured it is REJECTED (see the
# engine's ::/0 rule): the supported providers' WireGuard configs are
# IPv4-only, so IPv6 cannot be carried through the tunnel — blocking it is
# the honest behavior ("no IPv6 while on the VPN"), the alternative is a
# silent leak of the user's home IPv6. Real IPv6-over-VPN needs a provider
# that ships IPv6 WireGuard endpoints; revisit when one is added.
TUN_ADDRESS_V6 = "fdfe:dcba:9876::1/126"
# Conservative MTU (validated in the Tier 2 sandbox): everything re-enters
# sing-box in userspace anyway, so there is no gain in riding the 9000-byte
# sing-box default and risking fragmentation through WireGuard endpoints.
TUN_MTU = 1400

# Tunnel MTU for every generated WireGuard endpoint. sing-box's default (1408)
# assumes a clean 1500-byte path; behind any extra encapsulation (Docker bridge
# on a GCP VM: host MTU 1460) the encrypted outer packet exceeds the path MTU
# and wireguard-go dies with "sendmmsg: message too long" — a hard sing-box
# crash that takes every channel down. 1280 (the IPv6 minimum) plus WireGuard
# overhead fits any real path, so it can never trigger that crash; the small
# per-packet cost is the price of never crashing. ``ALLE_WG_MTU`` overrides it
# for operators who know their path (see engine._wg_mtu).
WG_MTU = 1280

# Where hijacked DNS goes in TUN mode. Decision: plain UDP
# to a well-known public resolver, dialed directly (sing-box's default for a
# DNS server with no detour) — never a LAN resolver (privacy stance: DNS is
# deliberately excluded from LAN-direct) and, in v1, never a channel (with
# multiple channels there is no "the tunnel" to prefer; a dns-via-channel
# toggle is future work). Direct dialing also means resolution keeps working
# under the kill-switch — required, since domain rules and endpoint dialing
# need it; the kill-switch still blocks apps' own unhijacked resolver
# traffic like any other unmatched flow.
TUN_DNS_UPSTREAM = "1.1.1.1"  # noqa: S1313
TUN_DNS_TAG = "dns-remote"
