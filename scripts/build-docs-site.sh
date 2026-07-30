#!/bin/bash
# Builds the MkDocs site into ./site. The homepage is generated from
# README.md (rewriting its docs/<file>.md links to <file>.md, since the
# generated page lives inside docs_dir alongside them) and removed again on
# exit so the working tree never carries a tracked copy of it.
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() { rm -f docs/index.md; }
trap cleanup EXIT

sed 's#\](docs/#](#g' README.md >docs/index.md

uv run --group docs mkdocs build "$@"
