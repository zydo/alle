# Shared helpers for the Tier 2 sandbox scripts (sourced, never executed).
# Callers set TUN_NAME and SB_LOG before using start_singbox/stop_singbox.

say() { printf '\n== %s\n' "$*"; }
pass() { printf '   PASS: %s\n' "$*"; }
fail() {
	printf '   FAIL: %s\n' "$*" >&2
	exit 1
}

# Fetch + checksum-verify the pinned sing-box into the /cache mount (mirrors
# alle.singbox.ensure_binary). Sets SB_VERSION, ARCH, and SB (binary path).
fetch_singbox() {
	say "pinned sing-box"
	read -r SB_VERSION SB_SHA < <(
		python3 - <<'EOF'
import platform, sys
sys.path.insert(0, "/repo/src")
from alle.constants import SINGBOX_SHA256, SINGBOX_VERSION
arch = {"aarch64": "arm64", "x86_64": "amd64"}[platform.machine()]
print(SINGBOX_VERSION, SINGBOX_SHA256[f"linux-{arch}"])
EOF
	)
	ARCH=$(python3 -c 'import platform; print({"aarch64":"arm64","x86_64":"amd64"}[platform.machine()])')
	SB=/cache/sing-box-${SB_VERSION}-linux-${ARCH}
	mkdir -p /cache
	if [ ! -x "$SB" ] || ! echo "$SB_SHA  $SB" | sha256sum -c --status; then
		local asset="sing-box-${SB_VERSION}-linux-${ARCH}.tar.gz"
		local url="https://github.com/SagerNet/sing-box/releases/download/v${SB_VERSION}/${asset}"
		echo "   downloading $url"
		curl -sSfL "$url" | tar -xzO "sing-box-${SB_VERSION}-linux-${ARCH}/sing-box" >"$SB.tmp"
		echo "$SB_SHA  $SB.tmp" | sha256sum -c --status || fail "sing-box checksum mismatch"
		chmod +x "$SB.tmp" && mv "$SB.tmp" "$SB"
	fi
	pass "sing-box ${SB_VERSION} (linux-${ARCH}) checksum-verified"
}

# Start the fetched sing-box on a config and wait for TUN_NAME to appear.
start_singbox() { # $1 = config path
	pkill -x sing-box 2>/dev/null || true
	sleep 0.3
	"$SB" run -c "$1" >"$SB_LOG" 2>&1 &
	SB_PID=$!
	for _ in $(seq 1 30); do
		ip link show "$TUN_NAME" >/dev/null 2>&1 && sleep 0.5 && return 0
		kill -0 "$SB_PID" 2>/dev/null || {
			cat "$SB_LOG" >&2
			fail "sing-box exited at startup"
		}
		sleep 0.2
	done
	cat "$SB_LOG" >&2
	fail "TUN device never appeared"
}

stop_singbox() {
	kill "$SB_PID" 2>/dev/null || true
	wait "$SB_PID" 2>/dev/null || true
}
