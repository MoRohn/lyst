# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Secret Management Service: master-key trust anchors, interchangeable backends, the credential boundary."""

from __future__ import annotations

import os

from .backends import EmbeddedBackend, OpenBaoBackend, SecretBackend, SecretUnavailable
from .masterkey import MasterKey, MasterKeyMissing
from .service import CredentialService, ReauthRequired


def backend_from_env(stores, master_key: bytes) -> SecretBackend:
    """OpenBao when LYST_OPENBAO_URL is set (enterprise), otherwise the embedded encrypted store."""
    url = os.environ.get("LYST_OPENBAO_URL")
    if url:
        from .masterkey import _anchor_value  # noqa: PLC0415

        return OpenBaoBackend(url, mount=os.environ.get("LYST_OPENBAO_MOUNT", "secret"), prefix=os.environ.get("LYST_OPENBAO_PREFIX", "lyst"),
                              token=_anchor_value("LYST_OPENBAO_TOKEN"), role_id=os.environ.get("LYST_OPENBAO_ROLE_ID"),
                              secret_id=_anchor_value("LYST_OPENBAO_SECRET_ID"), namespace=os.environ.get("LYST_OPENBAO_NAMESPACE"))
    return EmbeddedBackend(stores, master_key)


__all__ = ["CredentialService", "EmbeddedBackend", "MasterKey", "MasterKeyMissing", "OpenBaoBackend", "ReauthRequired",
           "SecretBackend", "SecretUnavailable", "backend_from_env"]
