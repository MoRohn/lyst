# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Bootstrap trust anchors: the few values that must exist *before* Lyst can open its own secret store.

Application credentials (model keys, database passwords, API tokens) are managed in Settings and live in
``lyst.secrets``. This module only answers "where does a deployment hand Lyst a bootstrap value", in order:

1. a mounted secret file ``$LYST_SECRETS_DIR/<NAME>`` (default ``/run/secrets``: Docker and Kubernetes
   secrets, Vault Agent, External Secrets);
2. the operating system's credential store (macOS Keychain, Windows Credential Manager, Secret Service),
   service ``lyst``, where setup mode places the master key on a workstation;
3. the process environment, for orchestrators that inject values (ECS, Nomad, CI).

Anchors are read on demand and never cached, logged, written to a table or returned by an API. ``.env``
files are refused for secret-shaped names (``config.load_env_file``). (The module is ``vault.py`` because
``lyst.security`` already imports the standard library's ``secrets``.)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("lyst.secrets")
SERVICE = "lyst"
SECRET_SHAPED = re.compile(r"(API_KEY|SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE_KEY|MASTER_KEY|CLIENT_SECRET|SECRET_ID)$", re.I)
KNOWN = ("LYST_MASTER_KEY", "LYST_OPENROUTER_API_KEY", "LYST_LLM_API_KEY", "LYST_OIDC_CLIENT_SECRET", "LYST_OPENBAO_TOKEN",
         "LYST_OPENBAO_SECRET_ID")


def is_secret_name(name: str) -> bool:
    return bool(SECRET_SHAPED.search(name))


def secrets_dir() -> Path:
    return Path(os.environ.get("LYST_SECRETS_DIR", "/run/secrets"))


def keyring_enabled() -> bool:
    return os.environ.get("LYST_KEYRING", "1") != "0"


def _keyring_get(name: str) -> str | None:
    if not keyring_enabled():
        return None
    try:
        import keyring  # noqa: PLC0415

        return keyring.get_password(SERVICE, name)
    except Exception as exc:  # noqa: BLE001 - no backend, locked keychain, headless session
        log.debug("keyring unavailable for %s: %s", name, type(exc).__name__)
        return None


def resolve(name: str) -> tuple[str, str] | None:
    """(value, source) or None. Sources: file, keyring, env."""
    f = secrets_dir() / name
    if f.is_file():
        return f.read_text().strip(), "file"
    v = _keyring_get(name)
    if v:
        return v, "keyring"
    if os.environ.get(name):
        return os.environ[name], "env"
    return None


def anchor(name: str) -> str | None:
    r = resolve(name)
    return r[0] if r else None


def source(name: str) -> str | None:
    r = resolve(name)
    return r[1] if r else None


def status(names: tuple[str, ...] = KNOWN) -> list[dict[str, str | bool | None]]:
    """Where each bootstrap value comes from. Never the values."""
    return [{"name": n, "source": source(n), "configured": source(n) is not None} for n in names]


__all__ = ["KNOWN", "anchor", "is_secret_name", "keyring_enabled", "resolve", "secrets_dir", "source", "status"]
