#!/usr/bin/env python3
"""Fill the `alle` Homebrew formula's top-level `url`/`sha256` from a published
`alle-proxy` sdist.

Every release, the `homebrew-tap` tap's formula must point at the exact sdist
that passed the tag publish gate and reached PyPI. This script reads the sdist's
URL and SHA-256 straight from PyPI's JSON API (the digest PyPI recorded for the
uploaded file — never one this script computes) and rewrites the *first*
`url`/`sha256` pair in the formula, i.e. the package's own source. The resource
pins (pyyaml, pycountry) sit after the first `resource "` line and are left
untouched.

Run it against a version only after that version exists on PyPI:

    scripts/update-homebrew-formula.py --version 0.1.9

By default it edits packaging/homebrew/alle.rb in place; `--formula` and
`--output` override the source and destination (use `--output -` for stdout).
It is idempotent: re-running for the same version reproduces the same file.

Only the standard library is used, so it runs anywhere Python 3.10+ does with no
install step.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

PACKAGE = "alle-proxy"
DEFAULT_FORMULA = (
    Path(__file__).resolve().parents[1] / "packaging" / "homebrew" / "alle.rb"
)


class UpdateError(RuntimeError):
    """A fatal, user-facing failure with an actionable message."""


def _fetch_sdist(version: str) -> tuple[str, str]:
    """Return the (url, sha256) of ``version``'s source distribution on PyPI."""
    api = f"https://pypi.org/pypi/{PACKAGE}/{version}/json"
    req = urllib.request.Request(api, headers={"Accept": "application/json"})  # noqa: S310 — fixed https PyPI URL
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https URL
            data = json.load(resp)
    except (OSError, ValueError) as e:
        raise UpdateError(
            f"could not fetch {PACKAGE}=={version} metadata from PyPI: {e}. "
            "Update the formula only after the release is published."
        ) from e
    for entry in data.get("urls", []):
        if entry.get("packagetype") == "sdist":
            sha = (entry.get("digests") or {}).get("sha256") or ""
            url = entry.get("url") or ""
            if not (sha and url):
                raise UpdateError(
                    f"PyPI sdist entry for {version} is missing url/sha256"
                )
            return url, sha
    raise UpdateError(f"no sdist found for {PACKAGE}=={version} on PyPI")


def rewrite_source(text: str, url: str, sha256: str) -> str:
    """Replace the formula's own `url`/`sha256` (the pair before the first
    resource block) with ``url``/``sha256``, leaving resource pins untouched."""
    resource_at = text.find("\n  resource ")
    head = text if resource_at == -1 else text[:resource_at]
    tail = "" if resource_at == -1 else text[resource_at:]

    head, url_hits = re.subn(
        r'^(\s*)url\s+"[^"]*"', rf'\g<1>url "{url}"', head, count=1, flags=re.MULTILINE
    )
    head, sha_hits = re.subn(
        r'^(\s*)sha256\s+"[^"]*"',
        rf'\g<1>sha256 "{sha256}"',
        head,
        count=1,
        flags=re.MULTILINE,
    )
    if url_hits != 1 or sha_hits != 1:
        raise UpdateError(
            "could not locate the formula's own url/sha256 before the first "
            "resource block — is packaging/homebrew/alle.rb intact?"
        )
    return head + tail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--version", required=True, help="published alle-proxy version, e.g. 0.1.9"
    )
    ap.add_argument(
        "--formula",
        type=Path,
        default=DEFAULT_FORMULA,
        help="formula to read (default: %(default)s)",
    )
    ap.add_argument(
        "--output",
        default=None,
        help="write here instead of editing --formula in place ('-' for stdout)",
    )
    args = ap.parse_args(argv)

    version = args.version.lstrip("v")
    try:
        text = args.formula.read_text()
    except OSError as e:
        raise UpdateError(f"could not read {args.formula}: {e}") from e

    url, sha256 = _fetch_sdist(version)
    updated = rewrite_source(text, url, sha256)

    if args.output == "-":
        sys.stdout.write(updated)
    else:
        dest = Path(args.output) if args.output else args.formula
        dest.write_text(updated)
        print(f"updated {dest} → {PACKAGE} {version}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
