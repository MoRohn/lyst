# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Bootstrap anchors and the master key: mounted file, OS credential store, environment; no silent keyfile;
setup mode when nothing holds the key; .env refuses secret-shaped names."""

import pytest

from lyst.secrets import masterkey
from lyst.security import vault


@pytest.fixture
def fake_keyring(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setenv("LYST_KEYRING", "1")  # the autouse fixture turns the real one off; this one is in memory
    monkeypatch.setattr(vault, "_keyring_get", lambda n: store.get(n))
    monkeypatch.setattr(masterkey, "keyring_available", lambda: "fake")

    class KR:
        @staticmethod
        def set_password(service, name, value):
            store[name] = value

        @staticmethod
        def delete_password(service, name):
            store.pop(name)

    import sys

    monkeypatch.setitem(sys.modules, "keyring", KR)
    return store


def test_anchor_resolution_order_file_then_keyring_then_env(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("LYST_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("LYST_OPENBAO_TOKEN", "from-env")
    assert vault.resolve("LYST_OPENBAO_TOKEN") == ("from-env", "env")
    fake_keyring["LYST_OPENBAO_TOKEN"] = "from-keychain"
    assert vault.resolve("LYST_OPENBAO_TOKEN") == ("from-keychain", "keyring")
    (tmp_path / "LYST_OPENBAO_TOKEN").write_text("from-file\n")
    assert vault.resolve("LYST_OPENBAO_TOKEN") == ("from-file", "file")
    assert vault.status(("LYST_OPENBAO_TOKEN", "LYST_MASTER_KEY")) == [
        {"name": "LYST_OPENBAO_TOKEN", "source": "file", "configured": True},
        {"name": "LYST_MASTER_KEY", "source": None, "configured": False}]


def test_master_key_is_never_generated_beside_the_database(tmp_path, monkeypatch):
    monkeypatch.delenv("LYST_MASTER_KEY", raising=False)
    home = tmp_path / "home"
    assert masterkey.resolve(home) is None
    assert not (home / ".lyst-key").exists()
    from lyst.config import load_settings
    from lyst.store import Stores

    settings = load_settings(home)
    assert settings.setup_required and settings.master_key is None
    with pytest.raises(masterkey.MasterKeyMissing):
        Stores(settings)
    # the development keyfile exists only behind an explicit, insecure flag
    monkeypatch.setenv("LYST_DEV_INSECURE_KEYFILE", "1")
    mk = masterkey.resolve(home)
    assert mk and mk.source == "dev-keyfile" and (home / ".lyst-key").exists()
    assert oct((home / ".lyst-key").stat().st_mode)[-3:] == "600"


def test_master_key_anchors_in_order(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("LYST_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("LYST_MASTER_KEY", "ab" * 32)
    assert masterkey.resolve().source == "env"
    fake_keyring["LYST_MASTER_KEY"] = "cd" * 32
    assert masterkey.resolve().source == "keyring"
    (tmp_path / "LYST_MASTER_KEY").write_text("ef" * 32)
    assert masterkey.resolve().source == "file" and masterkey.resolve().key == bytes.fromhex("ef" * 32)
    with pytest.raises(ValueError, match="at least 32 bytes"):
        (tmp_path / "LYST_MASTER_KEY").write_text("abcd")
        masterkey.resolve()


def test_provisioning_into_the_credential_store_is_once_only(fake_keyring):
    mk = masterkey.provision_into_keyring()
    assert len(mk.key) == 32 and fake_keyring["LYST_MASTER_KEY"] == mk.key.hex() and mk.source == "keyring"
    with pytest.raises(masterkey.MasterKeyMissing, match="already holds"):
        masterkey.provision_into_keyring()
    assert masterkey.remove_from_keyring() and "LYST_MASTER_KEY" not in fake_keyring
    restored = masterkey.store_into_keyring(mk.key.hex())
    assert restored.key == mk.key


def test_env_file_refuses_secret_shaped_names(tmp_path, monkeypatch, caplog):
    from lyst.config import load_env_file

    env = tmp_path / ".env"
    env.write_text("LYST_LLM_TIER=economy\nLYST_OPENROUTER_API_KEY=sk-or-leak\nPG_PASSWORD=x\nLYST_OPENBAO_SECRET_ID=y\n")
    monkeypatch.setenv("LYST_ENV_FILE", str(env))
    for k in ("LYST_LLM_TIER", "LYST_OPENROUTER_API_KEY", "PG_PASSWORD", "LYST_OPENBAO_SECRET_ID"):
        monkeypatch.delenv(k, raising=False)
    with caplog.at_level("WARNING"):
        load_env_file()
    import os

    assert os.environ.get("LYST_LLM_TIER") == "economy"
    assert all(k not in os.environ for k in ("LYST_OPENROUTER_API_KEY", "PG_PASSWORD", "LYST_OPENBAO_SECRET_ID"))
    assert "sk-or-leak" not in caplog.text and "Secrets & Connections" in caplog.text


def test_openrouter_key_is_not_in_static_settings(monkeypatch):
    """A mounted provider key is a seed for the credential service, never a field of Settings."""
    from lyst.config import llm_settings_from_env

    monkeypatch.setenv("LYST_OPENROUTER_API_KEY", "sk-or-mounted")
    s = llm_settings_from_env()
    assert s.provider == "openrouter" and s.api_key is None and s.configured
