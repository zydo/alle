#!/usr/bin/env bash
# Boot a release-shaped image on the selected architecture with a fresh volume.
set -euo pipefail
image=${1:?usage: container-release-smoke.sh IMAGE [PLATFORM]}
platform=${2:-linux/amd64}
name="alle-release-smoke-${platform##*/}"
bundle=$(mktemp)
cleanup() {
	docker rm -f "$name" >/dev/null 2>&1 || true
	rm -f "$bundle"
}
trap cleanup EXIT
printf '%s\n' 'kind: alle-bundle' 'bundle_version: 1' 'router:' '  killswitch: false' >"$bundle"
docker run -d --platform "$platform" --name "$name" \
	-e ALLE_API_LISTEN=127.0.0.1:18080 \
	-e ALLE_API_SECRET=release-container-smoke-secret \
	--mount type=bind,src="$bundle",dst=/etc/alle/bundle.yaml,readonly \
	"$image" >/dev/null
for _ in $(seq 1 90); do
	docker exec "$name" alle health >/dev/null 2>&1 && break
	sleep 2
done
docker exec "$name" alle health
test "$(docker exec "$name" id -u)" = 1000
docker exec -i "$name" python - <<'PY'
import json, urllib.error, urllib.request
base = "http://127.0.0.1:18080"
headers = {"Authorization": "Bearer release-container-smoke-secret"}
assert "channel_count" in json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/v1/status", headers=headers), timeout=10))
try:
    urllib.request.urlopen(base + "/api/v1/status", timeout=10)
    raise SystemExit("unauthenticated API request succeeded")
except urllib.error.HTTPError as error:
    assert error.code in (401, 403)
PY
docker stop "$name" >/dev/null
test "$(docker inspect -f '{{.State.ExitCode}}' "$name")" = 0
