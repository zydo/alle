#!/usr/bin/env python3
"""Regenerate the pinned sing-box SHA-256 table for a version bump.

Downloads the mainstream (macOS/Linux x amd64/arm64) release tarballs for a given
sing-box version, extracts each ``sing-box`` binary, and prints the
``SINGBOX_SHA256`` dict (hashes of the *extracted binaries*, matching how
``singbox.ensure_binary`` verifies them) to paste into ``src/alle/constants.py``.
Run on a version bump; mainstream-only by design (see the README platform decision).

    python scripts/gen_singbox_checksums.py 1.13.13
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import urllib.request

PLATFORMS = ["darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64"]
BASE = "https://github.com/SagerNet/sing-box/releases/download"


def main(version: str) -> None:
    print(f"# sing-box {version}")
    print("SINGBOX_SHA256 = {")
    for key in PLATFORMS:
        url = f"{BASE}/v{version}/sing-box-{version}-{key}.tar.gz"
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("/sing-box"))
            extracted = tf.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"archive member is not a file: {member.name}")
            binary = extracted.read()
        print(f'    "{key}": "{hashlib.sha256(binary).hexdigest()}",')
    print("}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: gen_singbox_checksums.py <version>  (e.g. 1.13.13)")
    main(sys.argv[1])
