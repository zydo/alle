"""geosite/geoip rule-set data: fetch, verify, cache, and record.

sing-box (>= 1.12; alle pins 1.13) loads geo data only as *rule-sets* —
one binary ``.srs`` file per category — so alle consumes the upstreams that
publish that format natively. The default source is the sing-box project's
own pair (`SagerNet/sing-geosite`, built from the canonical community
`v2fly/domain-list-community` data, and `SagerNet/sing-geoip`), each
auto-publishing per-category ``.srs`` files on a ``rule-set`` branch.

Cadence: downloads happen only on explicit user actions — adding a rule that
references an uncached category, applying a bundle that does, or
``alle routes geo refresh``. The compiled sing-box config only ever gets
``type: local`` rule-set entries pointing into the cache, never
``type: remote`` — the engine cannot fetch or auto-update on its own, so the
no-background-traffic posture holds end-to-end. Applies work offline from
the cache.

Integrity: the upstream publishes no signatures for ``.srs`` files, so the
model is commit-pinning plus recorded digests. A refresh resolves the branch
head once (one GitHub API call), every file is downloaded via the
commit-SHA-pinned raw URL (immutable content for that commit), its sha256 is
recorded in ``state.json`` (``geodata`` — config-relevant: a refresh
reconciles), and the digest is re-verified whenever the file is used. Cache
files are content-addressed (``geosite-netflix.<sha256:12>.srs``), so a
refresh swaps files atomically and stale ones are pruned afterwards. A
category's existence is validated by the fetch itself (404 → clear error,
with suggestions from the recorded manifest when available).

The manifest (the branch's category listing) is recorded at refresh into
``rulesets/manifest.json`` — regenerable ergonomics data for ``geo ls`` and
typo suggestions, deliberately outside ``state.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from alle import applog, paths, routes
from alle.state import Store

KINDS = routes.GEO_TYPES

# Source registry. Adding an upstream (or a custom mirror) is one entry here:
# per kind, the GitHub repo, the branch whose head is pinned at refresh, and
# the in-repo path template for a category's .srs file.
SOURCES: dict[str, dict] = {
    "sagernet": {
        "geosite": {
            "repo": "SagerNet/sing-geosite",
            "branch": "rule-set",
            "path": "geosite-{name}.srs",
        },
        "geoip": {
            "repo": "SagerNet/sing-geoip",
            "branch": "rule-set",
            "path": "geoip-{name}.srs",
        },
    },
    "metacubex": {
        "geosite": {
            "repo": "MetaCubeX/meta-rules-dat",
            "branch": "sing",
            "path": "geo/geosite/{name}.srs",
        },
        "geoip": {
            "repo": "MetaCubeX/meta-rules-dat",
            "branch": "sing",
            "path": "geo/geoip/{name}.srs",
        },
    },
}
DEFAULT_SOURCE = "sagernet"

_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"
_TIMEOUT = 30
_MAX_BYTES = 32 * 1024 * 1024  # far above any real category file
_MAGIC = b"SRS"  # binary rule-set header; catches HTML error pages etc.


class GeoDataError(Exception):
    """Geo rule-set data could not be fetched, verified, or found."""


def cache_dir() -> Path:
    d = paths.state_dir() / "rulesets"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _manifest_path() -> Path:
    return cache_dir() / "manifest.json"


def source_name(store: Store) -> str:
    name = (store.data.get("geodata") or {}).get("source") or DEFAULT_SOURCE
    if name not in SOURCES:
        raise GeoDataError(
            f"unknown geo source {name!r} (known: {', '.join(sorted(SOURCES))})"
        )
    return str(name)


def _record(store: Store, kind: str) -> dict:
    return (store.data.get("geodata") or {}).get(kind) or {}


def referenced(store: Store) -> dict[str, set[str]]:
    """``{kind: {category, …}}`` for every geo matcher in the rule table."""
    out: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for rule in store.rules():
        if rule.get("type") in KINDS:
            out[rule["type"]].add(str(rule.get("value")))
    return out


def _file_name(kind: str, name: str, sha256: str) -> str:
    return f"{kind}-{name}.{sha256[:12]}.srs"


def cached_path(store: Store, kind: str, name: str) -> Path | None:
    """The verified cache file for a recorded category, or None.

    Verifies the recorded sha256 against the file on disk on every call —
    the compile must never hand sing-box a file that doesn't match the
    record (tampering, torn writes, manual edits all surface here).
    """
    entry = (_record(store, kind).get("files") or {}).get(name)
    if not entry:
        return None
    path = cache_dir() / _file_name(kind, name, str(entry.get("sha256")))
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
        return None
    return path


def _http_get(url: str, *, accept: str | None = None) -> bytes:
    headers = {"User-Agent": "alle/1"}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — fixed https URLs built from the source registry
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310
            data = r.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise GeoDataError(f"HTTP {e.code} fetching {url}") from e
    except OSError as e:
        raise GeoDataError(f"could not fetch {url}: {e}") from e
    if len(data) > _MAX_BYTES:
        raise GeoDataError(f"{url} exceeds the {_MAX_BYTES >> 20} MiB cap")
    return data


def _head_commit(repo: str, branch: str) -> str:
    data = _http_get(
        f"{_API}/repos/{repo}/branches/{branch}",
        accept="application/vnd.github+json",
    )
    try:
        sha = json.loads(data)["commit"]["sha"]
    except (ValueError, KeyError, TypeError) as e:
        raise GeoDataError(f"unexpected GitHub API response for {repo}") from e
    if not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
        raise GeoDataError(f"unexpected commit id for {repo}: {sha!r}")
    return str(sha)


def _fetch_file(spec: dict, commit: str, kind: str, name: str) -> bytes:
    rel = spec["path"].format(name=name)
    url = f"{_RAW}/{spec['repo']}/{commit}/{rel}"
    try:
        data = _http_get(url)
    except GeoDataError as e:
        if "HTTP 404" in str(e):
            hint = _suggest(kind, name)
            raise GeoDataError(
                f"no {kind} category {name!r} at {spec['repo']}@{commit[:12]}"
                + (f" — did you mean: {hint}?" if hint else "")
                + f" (browse names: {upstream_url(kind)})"
            ) from e
        raise
    if not data.startswith(_MAGIC):
        raise GeoDataError(f"{url} is not a binary rule-set (bad header)")
    return data


def _write_cache(kind: str, name: str, data: bytes) -> dict:
    sha256 = hashlib.sha256(data).hexdigest()
    path = cache_dir() / _file_name(kind, name, sha256)
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # content-addressed name: rename is the atomic commit
    return {"sha256": sha256, "size": len(data), "fetched_at": int(time.time())}


def ensure_matchers(matchers: list[tuple[str, str]]) -> list[str]:
    """Fetch every uncached geo category among ``matchers`` (pre-mutation).

    Called by rule/bundle writes *before* any state change, so a bad category
    name or an unreachable upstream fails the whole operation cleanly. Returns
    the newly fetched ``kind:name`` refs (empty when everything was cached).
    """
    wanted = [(t, v) for t, v in matchers if t in KINDS]
    if not wanted:
        return []
    store = Store.load()
    source = source_name(store)
    fetched: list[str] = []
    commits: dict[str, str] = {}
    for kind, name in dict.fromkeys(wanted):
        if cached_path(store, kind, name) is not None:
            continue
        spec = SOURCES[source][kind]
        record = _record(store, kind)
        commit = commits.get(kind) or record.get("commit")
        if not commit or record.get("source") != source:
            commit = _head_commit(spec["repo"], spec["branch"])
        commits[kind] = commit
        data = _fetch_file(spec, commit, kind, name)
        entry = _write_cache(kind, name, data)
        store.update_geodata(kind, source=source, commit=commit, files={name: entry})
        fetched.append(f"{kind}:{name}")
        applog.log(
            f"geo: fetched {kind} {name} from {spec['repo']}@{commit[:12]} "
            f"({entry['size']} bytes)"
        )
    if fetched:
        prune(store)
    return fetched


def refresh() -> dict:
    """Re-pin both kinds to the current branch heads, re-download every
    referenced category, record fresh manifests, and prune stale files."""
    store = Store.load()
    source = source_name(store)
    refs = referenced(store)
    report: dict = {"source": source, "kinds": {}}
    manifest: dict = {"source": source, "recorded_at": int(time.time())}
    for kind in KINDS:
        spec = SOURCES[source][kind]
        commit = _head_commit(spec["repo"], spec["branch"])
        files: dict[str, dict] = {}
        for name in sorted(refs[kind]):
            data = _fetch_file(spec, commit, kind, name)
            files[name] = _write_cache(kind, name, data)
        store.update_geodata(
            kind, source=source, commit=commit, files=files, replace=True
        )
        names = _fetch_manifest(spec, commit)
        manifest[kind] = {"commit": commit, "names": names}
        report["kinds"][kind] = {
            "commit": commit,
            "fetched": sorted(refs[kind]),
            "categories_available": len(names),
        }
    _manifest_path().write_text(json.dumps(manifest))
    pruned = prune(store)
    report["pruned"] = pruned
    applog.log(
        "geo: refreshed from "
        + ", ".join(
            f"{SOURCES[source][k]['repo']}@{report['kinds'][k]['commit'][:12]}"
            for k in KINDS
        )
        + f"; {sum(len(report['kinds'][k]['fetched']) for k in KINDS)} file(s), "
        f"{len(pruned)} pruned"
    )
    return report


def _fetch_manifest(spec: dict, commit: str) -> list[str]:
    """The branch's category names via the git trees API (best-effort: an
    empty list degrades `geo ls`/suggestions, never a fetch)."""
    prefix_parts = spec["path"].split("/")[:-1]  # in-repo directory of the files
    leaf = spec["path"].rsplit("/", 1)[-1]  # e.g. geosite-{name}.srs
    head, tail = leaf.split("{name}", 1)
    try:
        tree_sha = commit
        for part in prefix_parts:  # walk subdirectories (MetaCubeX layout)
            listing = json.loads(
                _http_get(
                    f"{_API}/repos/{spec['repo']}/git/trees/{tree_sha}",
                    accept="application/vnd.github+json",
                )
            )
            entry = next(
                (e for e in listing.get("tree", []) if e.get("path") == part), None
            )
            if entry is None:
                return []
            tree_sha = entry["sha"]
        listing = json.loads(
            _http_get(
                f"{_API}/repos/{spec['repo']}/git/trees/{tree_sha}",
                accept="application/vnd.github+json",
            )
        )
    except (GeoDataError, ValueError):
        return []
    names = []
    for entry in listing.get("tree", []):
        p = str(entry.get("path", ""))
        if p.startswith(head) and p.endswith(tail):
            names.append(p[len(head) : len(p) - len(tail)])
    return sorted(names)


def manifest() -> dict:
    try:
        data = json.loads(_manifest_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def categories(
    kind: str | None = None, query: str | None = None
) -> dict[str, list[str]]:
    """Available category names from the manifest (offline — no network).

    Returns ``{kind: [name, …]}``. ``kind`` filters to one; ``query``
    case-insensitive-substring-filters the names within each kind. The
    manifest is populated at refresh; before the first refresh the lists are
    empty (and the user is guided to browse the upstream source — see the
    docs — or run ``alle routes geo refresh``).
    """
    man = manifest()
    kinds = [kind] if kind else list(KINDS)
    q = (query or "").lower()
    out: dict[str, list[str]] = {}
    for k in kinds:
        names = (man.get(k) or {}).get("names") or []
        out[k] = [n for n in names if not q or q in n.lower()]
    return out


def upstream_url(kind: str) -> str:
    """The plaintext source repo where a user can browse category names and
    (for geosite) the domains inside each category."""
    if kind == "geosite":
        return "https://github.com/v2fly/domain-list-community/tree/master/data"
    return "https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2"


def _suggest(kind: str, name: str) -> str | None:
    import difflib

    names = (manifest().get(kind) or {}).get("names") or []
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
    return ", ".join(close) if close else None


def prune(store: Store) -> list[str]:
    """Remove cache files no record references (post-refresh/apply cleanup)."""
    keep = {_manifest_path().name}
    for kind in KINDS:
        for name, entry in (_record(store, kind).get("files") or {}).items():
            keep.add(_file_name(kind, name, str(entry.get("sha256"))))
    removed = []
    for path in cache_dir().iterdir():
        if path.name not in keep and path.suffix == ".srs":
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return sorted(removed)
