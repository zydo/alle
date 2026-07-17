#!/usr/bin/env bash
# Execute an installed wheel or sdist in an otherwise empty environment.
set -euo pipefail

artifact=${1:?usage: release-artifact-smoke.sh DIST}
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
cleanup() {
	if [ -n "${pid:-}" ]; then
		kill -TERM "$pid" 2>/dev/null || true
		wait "$pid" 2>/dev/null || true
	fi
	rm -rf "$work"
}
trap cleanup EXIT

python3 -m venv "$work/venv"
"$work/venv/bin/pip" install --require-hashes -r "$root/docker/requirements.lock"
"$work/venv/bin/pip" install --no-deps "$artifact"

export ALLE_HOME="$work/state"
export ALLE_API_LISTEN=127.0.0.1:18080
export ALLE_API_SECRET=release-artifact-smoke-secret
"$work/venv/bin/alle" version
"$work/venv/bin/alle" --help >/dev/null
test ! -e "$work/venv/bin/alled" # the unsafe direct daemon entry point is absent

"$work/venv/bin/alle" run >"$work/daemon.log" 2>&1 &
pid=$!
for _ in $(seq 1 90); do
	"$work/venv/bin/alle" health >/dev/null 2>&1 && break
	kill -0 "$pid" 2>/dev/null || {
		cat "$work/daemon.log" >&2
		exit 1
	}
	sleep 1
done
"$work/venv/bin/alle" health
"$work/venv/bin/python" - <<'PY'
import json, os, urllib.error, urllib.request
base = "http://127.0.0.1:18080"
auth = {"Authorization": "Bearer " + os.environ["ALLE_API_SECRET"]}
data = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/v1/status", headers=auth), timeout=10))
assert "channel_count" in data, data
try:
    urllib.request.urlopen(base + "/api/v1/status", timeout=10)
    raise SystemExit("unauthenticated API request succeeded")
except urllib.error.HTTPError as error:
    assert error.code in (401, 403), error.code
PY
kill -TERM "$pid"
wait "$pid"
pid=
grep -q "data plane released" "$work/daemon.log"
