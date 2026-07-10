"""Per-provider login credentials, stored locally under ~/.alle/credentials.yaml.

alle authenticates to each provider with credentials the user adds explicitly
(``alle providers add <name>``) rather than reading them from the environment. A provider
is "configured" iff it has a complete credential entry in this file. The file is
written ``0600`` and never leaves the machine; alle displays only a masked
preview of secret values, never the raw credential.
"""

from __future__ import annotations

from contextlib import contextmanager

import yaml

from alle import fsio, paths
from alle.state import StoreReadError, _quarantine


def _path():
    return paths.state_dir() / "credentials.yaml"


def _lock_path():
    return paths.state_dir() / "credentials.lock"


def _load_all() -> dict[str, dict]:
    p = _path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        return {}  # genuinely absent — no provider configured yet
    except OSError as e:
        # Unreadable is not empty: a save from a blank view would wipe every
        # provider's credential. Abort the caller instead (see StoreReadError).
        raise StoreReadError(f"cannot read {p.name}: {e}") from e
    try:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise yaml.YAMLError("root is not a mapping")
    except yaml.YAMLError as e:
        # Preserve the bytes and fail loudly: a save after a silent empty read
        # would otherwise wipe every provider's credential (see state._quarantine).
        _quarantine(p, e)
        return {}
    providers = data.get("providers") or {}
    return providers if isinstance(providers, dict) else {}


def _save_all(providers: dict[str, dict]) -> None:
    # mkstemp (inside write_durably) creates the temp file 0600 from the first
    # byte — never a window where secrets sit under the default (usually
    # world-readable) umask mode — and the fsync + atomic rename mean a crash
    # at any point keeps a complete file (previous or new).
    def dump(f):
        f.write(
            "# Managed by alle. Provider login credentials — keep this file private.\n"
        )
        yaml.safe_dump(
            {"providers": providers}, f, sort_keys=False, default_flow_style=False
        )

    fsio.write_durably(_path(), dump, prefix=".credentials-", suffix=".yaml")


@contextmanager
def transaction():
    """Exclusive read-modify-write of credentials.yaml under an OS file lock.

    Yields the mutable ``{provider: creds}`` dict; whatever it looks like on
    exit is written back atomically. Serialises concurrent writers (CLI and
    Web UI mutations) so neither loses the other's update — the same contract
    as :func:`alle.state.transaction`, on its own lock file.
    """
    with fsio.locked(_lock_path()):
        data = _load_all()
        yield data
        _save_all(data)


def get(provider: str) -> dict | None:
    """The stored credential dict for a provider, or None if not configured."""
    return _load_all().get(provider)


def snapshot() -> dict[str, dict]:
    """The whole stored mapping — the setup-transaction journal's rollback copy."""
    return _load_all()


def clean(creds: dict) -> dict:
    """A credential dict with surrounding whitespace stripped from string values —
    the normalization applied before storing (and for comparing against what's
    stored)."""
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in creds.items()}


def set_(provider: str, creds: dict) -> None:
    """Store (or replace) a provider's credentials, stripping surrounding space."""
    with transaction() as data:
        data[provider] = clean(creds)


def replace_all(providers: dict[str, dict]) -> None:
    """Replace the whole file with exactly these providers' credentials.

    The bundle-restore path: a restore is declarative, so providers absent
    from ``providers`` lose their stored credential.
    """
    with transaction() as data:
        data.clear()
        data.update({provider: clean(creds) for provider, creds in providers.items()})


def remove(provider: str) -> bool:
    """Forget a provider's credentials. True if anything was removed."""
    with transaction() as data:
        return data.pop(provider, None) is not None


def configured() -> list[str]:
    """Providers that currently have a stored credential, sorted."""
    return sorted(_load_all())


def mask(value: str, show: int = 4) -> str:
    """First and last ``show`` characters, the rest as stars (e.g. ``nGx4****a91k``).

    Values too short to keep any plaintext on both ends are fully starred so a
    secret is never partially leaked.
    """
    v = value or ""
    if len(v) <= show * 2:
        return "*" * len(v)
    return f"{v[:show]}{'*' * (len(v) - show * 2)}{v[-show:]}"
