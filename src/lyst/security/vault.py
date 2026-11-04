# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Secrets never live in files Lyst reads as configuration. A named secret (the master encryption key, the
OpenRouter key, a connector password) is resolved, in order, from:

1. **A mounted secret file**: ``$LYST_SECRETS_DIR/<NAME>`` (default ``/run/secrets``), which is how Docker
   secrets, Kubernetes secrets, Vault Agent and External Secrets deliver values to a container.
2. **The operating system's credential store** through ``keyring``: the macOS Keychain, Windows Credential
   Manager, or Secret Service (GNOME Keyring, KWallet) on Linux, under the service name ``lyst``. This is the
   local-machine path: ``lyst secret set LYST_OPENROUTER_API_KEY`` stores it; the value is encrypted by the OS
   and released to the signed-in user's processes.
3. **The process environment**, for orchestrators that inject secrets as variables (ECS, Nomad, CI). Lyst
   itself never writes secrets to its environment, and ``.env`` files are refused for secret-shaped names.

Values are read on demand and cached in memory for the process; they are never logged, never written to a
database table in clear, and never returned by an API. Connector credentials may reference a named secret as
``secret:NAME`` instead of carrying the value. (This module is ``vault.py`` because ``lyst.security``
already imports the standard library's ``secrets``.)
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger("lyst.secrets")
SERVICE = "lyst"
SECRET_SHAPED = re.compile(r"(API_KEY|SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE_KEY|MASTER_KEY|CLIENT_SECRET)$", re.I)
KNOWN = ("LYST_MASTER_KEY", "LYST_OPENROUTER_API_KEY", "LYST_LLM_API_KEY", "LYST_OIDC_CLIENT_SECRET")


def is_secret_name(name: str) -> bool:
    return bool(SECRET_SHAPED.search(name))


class SecretStore:
    def __init__(self, *, secrets_dir: Path | None = None, use_keyring: bool = True, use_env: bool = True):
        self._secrets_dir = Path(secrets_dir) if secrets_dir else None
        self._use_keyring = use_keyring
        self.use_env = use_env
        self._cache: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    @property
    def secrets_dir(self) -> Path:
        return self._secrets_dir or Path(os.environ.get("LYST_SECRETS_DIR", "/run/secrets"))

    @property
    def use_keyring(self) -> bool:
        return self._use_keyring and os.environ.get("LYST_KEYRING", "1") != "0"

    # -- keyring access is isolated so tests can replace it ---------------------------------
    @staticmethod
    def _keyring_get(name: str) -> str | None:
        try:
            import keyring  # noqa: PLC0415

            return keyring.get_password(SERVICE, name)
        except Exception as exc:  # noqa: BLE001 - no backend, locked keychain, headless session
            log.debug("keyring unavailable for %s: %s", name, type(exc).__name__)
            return None

    @staticmethod
    def _keyring_set(name: str, value: str) -> None:
        import keyring  # noqa: PLC0415

        keyring.set_password(SERVICE, name, value)

    @staticmethod
    def _keyring_delete(name: str) -> bool:
        import keyring  # noqa: PLC0415
        from keyring.errors import PasswordDeleteError  # noqa: PLC0415

        try:
            keyring.delete_password(SERVICE, name)
            return True
        except PasswordDeleteError:
            return False

    @staticmethod
    def backend_name() -> str:
        try:
            import keyring  # noqa: PLC0415

            return type(keyring.get_keyring()).__module__.split(".")[-1]
        except Exception:  # noqa: BLE001
            return "none"

    # -- resolution -------------------------------------------------------------------------

    def resolve(self, name: str) -> tuple[str, str] | None:
        """(value, source) or None. Sources: file, keyring, env."""
        with self._lock:
            if name in self._cache:
                return self._cache[name]
        found = None
        f = self.secrets_dir / name
        if f.is_file():
            found = (f.read_text().strip(), "file")
        if found is None and self.use_keyring:
            v = self._keyring_get(name)
            if v:
                found = (v, "keyring")
        if found is None and self.use_env and os.environ.get(name):
            found = (os.environ[name], "env")
        if found:
            with self._lock:
                self._cache[name] = found
        return found

    def get(self, name: str) -> str | None:
        r = self.resolve(name)
        return r[0] if r else None

    def source(self, name: str) -> str | None:
        r = self.resolve(name)
        return r[1] if r else None

    def set(self, name: str, value: str) -> str:
        """Store in the OS credential store. Returns the backend that holds it now."""
        if not value or not value.strip():
            raise ValueError("an empty secret is not stored")
        self._keyring_set(name, value.strip())
        with self._lock:
            self._cache.pop(name, None)
        return self.backend_name()

    def delete(self, name: str) -> bool:
        ok = self._keyring_delete(name)
        with self._lock:
            self._cache.pop(name, None)
        return ok

    def forget(self) -> None:
        with self._lock:
            self._cache.clear()

    def status(self, names: tuple[str, ...] = KNOWN) -> list[dict[str, str | None]]:
        """Where each known secret comes from. Never the values."""
        return [{"name": n, "source": self.source(n), "configured": bool(self.get(n))} for n in names]

    def reference(self, value: str | None) -> str | None:
        """Connector credentials may be ``secret:NAME``; resolve that, otherwise return the value as given."""
        if value and value.startswith("secret:"):
            name = value[len("secret:"):].strip()
            got = self.get(name)
            if got is None:
                raise KeyError(f"secret {name} is not configured in any backend")
            return got
        return value


STORE = SecretStore()
