"""Per-provider login credentials, stored locally under ~/.alle/credentials.yaml.

alle authenticates to each provider with credentials the user adds explicitly
(``alle providers add <name>``) rather than reading them from the environment. A provider
is "configured" iff it has a complete credential entry in this file. The file is
written ``0600`` and never leaves the machine; alle displays only a masked
preview of secret values, never the raw credential.
"""

from __future__ import annotations

import os
import tempfile

import yaml

from alle import paths
from alle.state import _quarantine


def _path():
    return paths.state_dir() / "credentials.yaml"


def _load_all() -> dict[str, dict]:
    p = _path()
    if not p.exists():
        return {}
    try:
        text = p.read_text()
    except OSError:
        return {}
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
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    header = "# Managed by alle. Provider login credentials — keep this file private.\n"
    # mkstemp creates the temp file 0600 from the first byte — never a window
    # where secrets sit under the default (usually world-readable) umask mode —
    # and the atomic rename means a crash mid-write keeps the previous file.
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=".credentials-", suffix=".yaml"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(header)
            yaml.safe_dump(
                {"providers": providers}, f, sort_keys=False, default_flow_style=False
            )
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def get(provider: str) -> dict | None:
    """The stored credential dict for a provider, or None if not configured."""
    return _load_all().get(provider)


def clean(creds: dict) -> dict:
    """A credential dict with surrounding whitespace stripped from string values —
    the normalization applied before storing (and for comparing against what's
    stored)."""
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in creds.items()}


def set_(provider: str, creds: dict) -> None:
    """Store (or replace) a provider's credentials, stripping surrounding space."""
    data = _load_all()
    data[provider] = clean(creds)
    _save_all(data)


def replace_all(providers: dict[str, dict]) -> None:
    """Replace the whole file with exactly these providers' credentials.

    The bundle-restore path: a restore is declarative, so providers absent
    from ``providers`` lose their stored credential.
    """
    _save_all({provider: clean(creds) for provider, creds in providers.items()})


def remove(provider: str) -> bool:
    """Forget a provider's credentials. True if anything was removed."""
    data = _load_all()
    if provider not in data:
        return False
    del data[provider]
    _save_all(data)
    return True


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
