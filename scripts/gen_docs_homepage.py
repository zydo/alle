#!/usr/bin/env python3
"""Generate the MkDocs homepage (docs/index.md) from README.md.

Two rewrites, both because the generated page lives one directory deeper
than README.md:

1. ``](docs/foo.md)`` -> ``](foo.md)`` -- internal doc links.
2. GitHub's ``<picture>``/``prefers-color-scheme`` light-dark image pattern
   -> Material for MkDocs' ``#only-light``/``#only-dark`` image suffix
   convention. GitHub's pattern tracks the *browser's* OS-level color
   scheme; Material's tracks the *site's own* light/dark toggle
   (``data-md-color-scheme``) instead, which is what a reader actually
   controls on the docs site. The two conventions render the same picture
   correctly on their respective platforms but aren't interchangeable, so
   the source of truth (README.md) keeps GitHub's form and this rewrite
   produces Material's form for the generated homepage only.
"""

from __future__ import annotations

import re
import sys

_PICTURE = re.compile(
    r"<picture>\s*"
    r'<source media="\(prefers-color-scheme: dark\)" srcset="([^"]+)">\s*'
    r'<img src="([^"]+)" alt="([^"]*)"([^>]*)>\s*'
    r"</picture>",
    re.DOTALL,
)


def _only_light_dark(match: re.Match[str]) -> str:
    dark_src, light_src, alt, rest = match.groups()
    return (
        f'<img src="{light_src}#only-light" alt="{alt}"{rest}>\n'
        f'  <img src="{dark_src}#only-dark" alt="{alt}"{rest}>'
    )


def generate(readme: str) -> str:
    content = readme.replace("](docs/", "](")
    return _PICTURE.sub(_only_light_dark, content)


def main() -> None:
    readme_path, out_path = sys.argv[1], sys.argv[2]
    with open(readme_path, encoding="utf-8") as f:
        readme = f.read()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(generate(readme))


if __name__ == "__main__":
    main()
