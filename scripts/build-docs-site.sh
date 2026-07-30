#!/bin/bash
# Builds the MkDocs site into ./site. Two inputs are generated from their
# real source of truth and removed again on exit, so the working tree never
# carries a tracked duplicate:
#   - docs/index.md        <- README.md, via gen_docs_homepage.py (see there
#                              for what gets rewritten and why)
#   - docs/assets/icon.svg <- src/alle/assets/icon.svg (the app icon,
#                              referenced by mkdocs.yml as the site logo/favicon)
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() {
	rm -f docs/index.md docs/assets/icon.svg
	rmdir docs/assets 2>/dev/null || true
}
trap cleanup EXIT

python3 scripts/gen_docs_homepage.py README.md docs/index.md
mkdir -p docs/assets
cp src/alle/assets/icon.svg docs/assets/icon.svg

uv run --group docs mkdocs build "$@"
