# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Secrets: mounted files, the OS credential store, the environment; .env refuses secret-shaped names;
connector credentials by reference; the CLI stores without echo."""

import io

import pytest

from lyst.security import vault as sec


@pytest.fixture
def fake_keyring(monkeypatch):
    vault: dict[str, str] = {}
    monkeypatch.setenv("LYST_KEYRING", "1")  # the autouse fixture turns the real one off; this one is in memory
    monkeypatch.setattr(sec.SecretStore, "_keyring_get", staticmethod(lambda n: vault.get(n)))
    monkeypatch.setattr(sec.SecretStore, "_keyring_set", staticmethod(lambda n, v: vault.__setitem__(n, v)))
    monkeypatch.setattr(sec.SecretStore, "_keyring_delete", staticmethod(lambda n: vault.pop(n, None) is not None))
    monkeypatch.setattr(sec.SecretStore, "backend_name", staticmethod(lambda: "fake"))
    return vault


def test_resolution_order_file_then_keyring_then_env(tmp_path, monkeypatch, fake_keyring):
    monkeypatch.setenv("LYST_OPENROUTER_API_KEY", "from-env")
    store = sec.SecretStore(secrets_dir=tmp_path)
    assert store.resolve("LYST_OPENROUTER_API_KEY") == ("from-env", "env")
    store.forget()
    fake_keyring["LYST_OPENROUTER_API_KEY"] = "from-keychain"
    assert store.resolve("LYST_OPENROUTER_API_KEY") == ("from-keychain", "keyring")
    store.forget()
    (tmp_path / "LYST_OPENROUTER_API_KEY").write_text("from-file\n")
    assert store.resolve("LYST_OPENROUTER_API_KEY") == ("from-file", "file")
    assert store.status(("LYST_OPENROUTER_API_KEY", "LYST_MASTER_KEY"))[1] == {"name": "LYST_MASTER_KEY", "source": None, "configured": False}


def test_set_and_delete_go_to_the_credential_store(tmp_path, fake_keyring):
    store = sec.SecretStore(secrets_dir=tmp_path, use_env=False)
    assert store.set("PG_PASSWORD", " s3cret ") == "fake" and fake_keyring["PG_PASSWORD"] == "s3cret"
    assert store.get("PG_PASSWORD") == "s3cret"
    with pytest.raises(ValueError):
        store.set("PG_PASSWORD", "   ")
    assert store.delete("PG_PASSWORD") and store.get("PG_PASSWORD") is None and not store.delete("PG_PASSWORD")


def test_env_file_refuses_secret_shaped_names(tmp_path, monkeypatch, caplog):
    from lyst.config import load_env_file

    env = tmp_path / ".env"
    env.write_text("LYST_LLM_TIER=economy\nLYST_OPENROUTER_API_KEY=sk-or-leak\nPG_PASSWORD=x\n")
    monkeypatch.setenv("LYST_ENV_FILE", str(env))
    for k in ("LYST_LLM_TIER", "LYST_OPENROUTER_API_KEY", "PG_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with caplog.at_level("WARNING"):
        load_env_file()
    import os

    assert os.environ.get("LYST_LLM_TIER") == "economy"
    assert "LYST_OPENROUTER_API_KEY" not in os.environ and "PG_PASSWORD" not in os.environ
    assert "lyst secret set LYST_OPENROUTER_API_KEY" in caplog.text


def test_settings_read_the_key_from_the_store_not_the_env_file(tmp_path, monkeypatch, fake_keyring):
    from lyst.config import load_settings

    monkeypatch.setattr(sec, "STORE", sec.SecretStore(secrets_dir=tmp_path, use_env=False))
    monkeypatch.delenv("LYST_OPENROUTER_API_KEY", raising=False)
    fake_keyring["LYST_OPENROUTER_API_KEY"] = "sk-or-keychain"
    fake_keyring["LYST_MASTER_KEY"] = "ab" * 32
    s = load_settings(tmp_path / "home")
    assert s.llm.provider == "openrouter" and s.llm.api_key == "sk-or-keychain"
    assert s.master_key == bytes.fromhex("ab" * 32) and not (tmp_path / "home" / ".lyst-key").exists()


def test_connector_credentials_by_reference(seeded, tmp_path, monkeypatch, fake_keyring):
    import sqlite3

    root = tmp_path / "imports"
    root.mkdir()
    monkeypatch.setenv("LYST_IMPORT_ROOT", str(root))
    monkeypatch.setattr(sec, "STORE", sec.SecretStore(secrets_dir=tmp_path / "none", use_env=False))
    lyst, owner = seeded
    with sqlite3.connect(root / "a.sqlite") as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    # the files/sqlite connectors have no secret field, so exercise the reference resolver directly
    with pytest.raises(KeyError, match="not configured"):
        sec.STORE.reference("secret:PG_PASSWORD")
    fake_keyring["PG_PASSWORD"] = "hunter2"
    assert sec.STORE.reference("secret:PG_PASSWORD") == "hunter2" and sec.STORE.reference("plain") == "plain"
    # and a stored connection resolves it at connect time
    cfg, secrets = lyst.connections._split("postgres", {"host": "h", "dbname": "d", "user": "u", "password": "secret:PG_PASSWORD"})
    connector = lyst.connections._connector("postgres", cfg, secrets)
    assert connector.secrets["password"] == "hunter2"


def test_cli_secret_set_reads_stdin_and_list_hides_values(monkeypatch, capsys, fake_keyring, tmp_path):
    from lyst.cli import main

    monkeypatch.setattr(sec, "STORE", sec.SecretStore(secrets_dir=tmp_path, use_env=False))
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-or-abcdef\n"))
    main(["secret", "set", "LYST_OPENROUTER_API_KEY", "--stdin"])
    assert fake_keyring["LYST_OPENROUTER_API_KEY"] == "sk-or-abcdef"
    main(["secret", "list"])
    out = capsys.readouterr().out
    assert "LYST_OPENROUTER_API_KEY" in out and "configured via keyring" in out and "abcdef" not in out
    with pytest.raises(SystemExit):
        main(["secret", "set", "NOT_A_SECRET_NAME_TIER", "--stdin"])
