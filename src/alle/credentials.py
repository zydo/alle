"""Per-provider login credentials, stored locally under ~/.alle/credentials.yaml.

alle authenticates to each provider with credentials the user adds explicitly
(``alle providers add <name>``) rather than reading them from the environment. A provider
is "configured" iff it has a complete credential entry in this file. The file is
written ``0600`` and never leaves the machine; alle displays only a masked
preview of secret values, never the raw credential.
"""

from __future__ import annotations

import os
import stat

import yaml

from alle import paths


def _path():
    return paths.state_dir() / "credentials.yaml"


def _load_all() -> dict[str, dict]:
    p = _path()
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("providers") or {}


def _save_all(providers: dict[str, dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    header = "# Managed by alle. Provider login credentials — keep this file private.\n"
    # Created 0600 from the first byte — never a window where the file holds
    # secrets under the default (usually world-readable) umask mode.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        f.write(header)
        yaml.safe_dump(
            {"providers": providers}, f, sort_keys=False, default_flow_style=False
        )
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # tighten a pre-existing looser file too


def get(provider: str) -> dict | None:
    """The stored credential dict for a provider, or None if not configured."""
    return _load_all().get(provider)


def set_(provider: str, creds: dict) -> None:
    """Store (or replace) a provider's credentials, stripping surrounding space."""
    cleaned = {k: (v.strip() if isinstance(v, str) else v) for k, v in creds.items()}
    data = _load_all()
    data[provider] = cleaned
    _save_all(data)


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
