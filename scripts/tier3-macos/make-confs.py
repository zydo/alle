"""Write two WireGuard .conf files for the Tier 3 fleet.

Keys are generated here, never committed: alle validates them as real 32-byte
base64, so anything embedded in a repo file would read as a live secret to the
secret scan.

  wg-JP-01.conf  global v6 interface address -> channel IS v6-capable
  wg-US-02.conf  v4 only                     -> same provider, NOT v6-capable
                                                (Proton's ~20% v4-only servers)
"""

import base64
import os
import pathlib
import sys


def key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


TEMPLATE = """[Interface]
PrivateKey = {priv}
Address = {addr}
DNS = 10.2.0.1

[Peer]
PublicKey = {pub}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {endpoint}:51820
PersistentKeepalive = 25
"""

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else __file__).resolve()
if out.is_file():
    out = out.parent
for name, addr, endpoint in (
    ("wg-JP-01.conf", "10.2.0.2/32, 2a07:b944::2:2/128", "198.51.100.10"),
    ("wg-US-02.conf", "10.2.0.3/32", "198.51.100.11"),
):
    (out / name).write_text(
        TEMPLATE.format(priv=key(), pub=key(), addr=addr, endpoint=endpoint)
    )
    print(f"wrote {name}")
