# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The master encryption key: the one secret Lyst cannot store in itself.

It encrypts the DuckDB files at rest and wraps every data key of the embedded secret backend, so it must
come from a **trust anchor outside the application's data**, in this order:

1. a mounted secret file ``$LYST_SECRETS_DIR/LYST_MASTER_KEY`` (Docker/Kubernetes secrets, Vault Agent);
2. OpenBao KV (``LYST_OPENBAO_URL`` configured) at ``<prefix>/master-key``;
3. the operating system's credential store (macOS Keychain, Windows Credential Manager, Secret Service);
4. the ``LYST_MASTER_KEY`` environment variable injected by an orchestrator.

Lyst never silently generates a key beside the database. With no anchor it starts in **setup mode**, where
the first owner initialises encryption from the browser: Lyst generates the key, stores it in the OS
credential store (the only anchor it can write to itself), and shows a one-time recovery copy to be kept
offline. Unattended deployments must provide anchor 1, 2 or 4. The development-only keyfile exists solely
for `LYST_DEV_INSECURE_KEYFILE=1` and is refused otherwise.

Loss of the master key means the databases and every stored secret are unrecoverable by design.
"""

from __future__ import annotations

import logging
import os
import secrets as rnd
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("lyst.masterkey")
NAME = "LYST_MASTER_KEY"
SERVICE = "lyst"


class MasterKeyMissing(RuntimeError):
    """No trust anchor holds the master key: the engine runs in setup mode until one does."""


@dataclass(frozen=True)
class MasterKey:
    key: bytes
    source: str
    version: int = 1


def _parse(hexvalue: str) -> bytes:
    key = bytes.fromhex(hexvalue.strip())
    if len(key) < 32:
        raise ValueError("the master key must be at least 32 bytes (64 hex characters)")
    return key


def _keyring_get(name: str) -> str | None:
    from ..security.vault import _keyring_get as get  # noqa: PLC0415

    return get(name)


def keyring_available() -> str | None:
    """The OS credential store's backend name when one can hold secrets, else None."""
    if os.environ.get("LYST_KEYRING", "1") == "0":
        return None
    try:
        import keyring  # noqa: PLC0415
        from keyring.backends.fail import Keyring as Fail  # noqa: PLC0415
        from keyring.backends.null import Keyring as Null  # noqa: PLC0415

        kr = keyring.get_keyring()
        if isinstance(kr, Fail | Null):
            return None
        return type(kr).__module__.split(".")[-1]
    except Exception:  # noqa: BLE001
        return None


def _openbao_master_key() -> str | None:
    url = os.environ.get("LYST_OPENBAO_URL")
    if not url:
        return None
    from .backends import OpenBaoBackend, SecretUnavailable  # noqa: PLC0415

    try:
        bao = OpenBaoBackend(url, mount=os.environ.get("LYST_OPENBAO_MOUNT", "secret"), prefix=os.environ.get("LYST_OPENBAO_PREFIX", "lyst"),
                             token=_anchor_value("LYST_OPENBAO_TOKEN"), role_id=os.environ.get("LYST_OPENBAO_ROLE_ID"),
                             secret_id=_anchor_value("LYST_OPENBAO_SECRET_ID"), namespace=os.environ.get("LYST_OPENBAO_NAMESPACE"))
        return bao.fetch("master-key")
    except SecretUnavailable as exc:
        log.warning("OpenBao holds no master key: %s", exc)
        return None


def _anchor_value(name: str) -> str | None:
    """A bootstrap secret for the anchors themselves (OpenBao token or AppRole secret id): file, keyring, env."""
    from ..security.vault import anchor  # noqa: PLC0415

    return anchor(name)


def resolve(home: Path | None = None) -> MasterKey | None:
    from ..security.vault import secrets_dir  # noqa: PLC0415

    f = secrets_dir() / NAME
    if f.is_file():
        return MasterKey(_parse(f.read_text()), "file")
    bao = _openbao_master_key()
    if bao:
        return MasterKey(_parse(bao), "openbao")
    kr = _keyring_get(NAME)
    if kr:
        return MasterKey(_parse(kr), "keyring")
    env = os.environ.get(NAME)
    if env:
        return MasterKey(_parse(env), "env")
    if home and os.environ.get("LYST_DEV_INSECURE_KEYFILE") == "1":
        path = home / ".lyst-key"
        if path.exists():
            log.warning("using the development keyfile %s (LYST_DEV_INSECURE_KEYFILE=1): not for real data", path)
            return MasterKey(_parse(path.read_text()), "dev-keyfile")
        home.mkdir(parents=True, exist_ok=True)
        key = rnd.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key.hex())
        log.warning("created the development keyfile %s (LYST_DEV_INSECURE_KEYFILE=1): not for real data", path)
        return MasterKey(key, "dev-keyfile")
    return None


def require(home: Path | None = None) -> MasterKey:
    mk = resolve(home)
    if mk is None:
        raise MasterKeyMissing(
            "No master encryption key is available. Initialise Lyst from the browser (the key is stored in the operating system's "
            "credential store), or for an unattended deployment mount it at $LYST_SECRETS_DIR/LYST_MASTER_KEY, serve it from "
            "OpenBao, or inject LYST_MASTER_KEY.")
    return mk


def provision_into_keyring() -> MasterKey:
    """Generate a master key and store it in the OS credential store. Used by setup mode on a machine with one."""
    if keyring_available() is None:
        raise MasterKeyMissing("this machine has no usable OS credential store; mount the key or use OpenBao")
    if _keyring_get(NAME):
        raise MasterKeyMissing("the credential store already holds a master key")
    import keyring  # noqa: PLC0415

    key = rnd.token_bytes(32)
    keyring.set_password(SERVICE, NAME, key.hex())
    return MasterKey(key, "keyring")


def remove_from_keyring() -> bool:
    try:
        import keyring  # noqa: PLC0415

        keyring.delete_password(SERVICE, NAME)
        return True
    except Exception:  # noqa: BLE001
        return False


def store_into_keyring(hexvalue: str) -> MasterKey:
    """Recovery: put a saved master key back into the credential store (for example on a rebuilt machine)."""
    key = _parse(hexvalue)
    import keyring  # noqa: PLC0415

    keyring.set_password(SERVICE, NAME, key.hex())
    return MasterKey(key, "keyring")


__all__ = ["MasterKey", "MasterKeyMissing", "keyring_available", "provision_into_keyring", "remove_from_keyring", "require",
           "resolve", "store_into_keyring"]
