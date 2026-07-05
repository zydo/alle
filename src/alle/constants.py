"""Shared constants for the single sing-box process alle runs."""

from __future__ import annotations

# Pinned sing-box release. alle always uses exactly this build, downloaded
# from upstream into ~/.alle/bin/sing-box@<version> and verified against the
# checksums below — never any other sing-box that happens to be on PATH.
SINGBOX_VERSION = "1.13.13"

# SHA256 of the *extracted sing-box binary* (not the tarball) for each supported
# platform. Upstream publishes no checksum file, so we pin the bytes ourselves
# (see THIRD_PARTY_NOTICES.md). Pinning the binary lets us verify both a fresh
# download and the on-disk file on every start. Regenerate on a version bump with
# scripts/gen_singbox_checksums.py.
SINGBOX_SHA256 = {
    "darwin-amd64": "fb7ef2dead0a0231fa438e1cfdd4ad8a653a47e33f5cd1007560b33a12de7bf8",
    "darwin-arm64": "b6056a1fa50e3abbe4d1c6bb85687396c6faf5c3d42f347e760191a5b218751d",
    "linux-amd64": "2d8e80be91f196aff601f3ab2d5a855ac1dd5a447666cb7ec0cad99323b87dfe",
    "linux-arm64": "d21721e273f5aab8a20a1bfda378602fdca2b40d9a7145a781bbdef1f496a1d5",
}

# The Clash API address/secret are not constants: they are generated per
# installation (see singbox.clash_api) so the API is authenticated and two
# users on one machine don't fight over a hard-coded port.

# Tag prefixes inside the generated sing-box config, so a channel id maps to its
# inbound/outbound deterministically.
INBOUND_PREFIX = "in-"
OUTBOUND_PREFIX = "out-"
