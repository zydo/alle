#!/bin/bash
# Builds the MkDocs site into ./site. Two inputs are generated from their
# real source of truth and removed again on exit, so the working tree never
# carries a tracked duplicate:
#   - docs/index.md   <- README.md (docs/<file>.md links rewritten to
#                        <file>.md, since the generated page lives inside
#                        docs_dir alongside them)
#   - docs/assets/icon.svg <- src/alle/assets/icon.svg (the app icon,
#                        referenced by mkdocs.yml as the site logo/favicon)
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() {
	rm -f docs/index.md docs/assets/icon.svg
	rmdir docs/assets 2>/dev/null || true
}
trap cleanup EXIT

sed 's#\](docs/#](#g' README.md >docs/index.md
mkdir -p docs/assets
cp src/alle/assets/icon.svg docs/assets/icon.svg

uv run --group docs mkdocs build "$@"
