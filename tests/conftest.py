# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
from __future__ import annotations

import pytest

from lyst import demo
from lyst.app import Lyst
from lyst.config import load_settings


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path, monkeypatch):
    """Tests never touch the real credential store, and never see a secret cached by another test."""
    from lyst.security.vault import STORE

    monkeypatch.setenv("LYST_KEYRING", "0")
    monkeypatch.setenv("LYST_SECRETS_DIR", str(tmp_path / "no-secrets"))
    STORE.forget()
    yield
    STORE.forget()


@pytest.fixture
def lyst(tmp_path, monkeypatch):
    monkeypatch.setenv("LYST_MASTER_KEY", "ab" * 32)
    monkeypatch.setenv("LYST_MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,testserver")
    for k in ("LYST_LLM_BASE_URL", "LYST_LLM_MODEL", "LYST_LLM_EXTERNAL"):
        monkeypatch.delenv(k, raising=False)
    app = Lyst.build(load_settings(tmp_path / "home"), lease_seconds=30)
    yield app
    app.close()


@pytest.fixture
def seeded(lyst):
    owner = lyst.guard.create_user("avery@northwind.test", "Avery")
    demo.seed(lyst, owner)
    return lyst, owner


def ask(lyst, actor, question, ws="northwind"):
    tid = lyst.runtime.submit(actor, ws, question)
    lyst.runtime.drain()
    return lyst.runtime.get(actor, tid)
