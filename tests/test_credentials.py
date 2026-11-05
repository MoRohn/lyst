# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The credential boundary. One test per threat the design must hold against: cross-tenant access, privilege
escalation, prompt injection and generated code, leaked logs and audit, API exposure, database and backup
compromise, master-key loss, failed rotation, concurrent replacement, provider-side revocation, compromised
connectors, expired OAuth tokens, unauthorised testing, ownership changes, and setup from a cold start."""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.secrets import service as svc_mod
from lyst.secrets.backends import EmbeddedBackend, OpenBaoBackend, SecretUnavailable
from lyst.security import AccessDenied

KEY = "or-test-key-7f3a9c2e1b5d4a6f8e0c1d2b3a4f5e6d"  # a placeholder shaped unlike any real provider key


def openrouter(valid=(KEY,), calls=None):
    """OpenRouter's GET /key for the credential test; anything else is a 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.host == "openrouter.ai" and request.url.path.endswith("/key"):
            tok = request.headers.get("authorization", "")[7:]
            if tok in valid:
                return httpx.Response(200, json={"data": {"label": "lyst", "limit": 50, "usage": 1.25}})
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def blobs_contain(lyst, needle: str) -> bool:
    rows = lyst.stores.control.query("SELECT ciphertext, wrapped_dek FROM secret_blobs")
    return any(needle.encode() in bytes(r["ciphertext"]) or needle.encode() in bytes(r["wrapped_dek"]) for r in rows)


# ------------------------------------------------------------------------- the embedded store


def test_embedded_backend_envelope_binds_reference_and_key(lyst):
    be = lyst.credentials.backend
    assert isinstance(be, EmbeddedBackend)
    be.store("installation:installation:cred_a:v1", "hunter2-hunter2")
    be.store("installation:installation:cred_b:v1", "other-value-9")
    assert be.fetch("installation:installation:cred_a:v1") == "hunter2-hunter2"
    assert not blobs_contain(lyst, "hunter2")
    # a blob moved under another reference (a tampered or spliced database) does not open: the AAD binds it
    lyst.stores.control.execute("UPDATE secret_blobs SET ref = 'installation:installation:cred_c:v1' WHERE ref = 'installation:installation:cred_a:v1'")
    with pytest.raises(SecretUnavailable, match="cannot be opened"):
        be.fetch("installation:installation:cred_c:v1")
    # database compromise or a leaked backup: without the master key nothing opens
    with pytest.raises(SecretUnavailable):
        EmbeddedBackend(lyst.stores, b"\x01" * 32).fetch("installation:installation:cred_b:v1")
    assert be.delete("installation:installation:cred_b:v1") and not be.delete("installation:installation:cred_b:v1")
    with pytest.raises(SecretUnavailable, match="no such"):
        be.fetch("installation:installation:cred_b:v1")


# --------------------------------------------------------------------- connect, replace, revoke


def test_connect_tests_first_and_the_value_never_comes_back(seeded):
    lyst, owner = seeded
    lyst.credentials.http = openrouter(valid=(KEY,))
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        bad = c.post("/v1/credentials", json={"provider": "openrouter", "name": "OpenRouter", "secret": "or-test-key-wrong-wrong-wrong"}, headers=h)
        assert bad.status_code == 400 and "authentication failed" in bad.json()["detail"]
        assert lyst.credentials.resolve("openrouter") is None
        ok = c.post("/v1/credentials", json={"provider": "openrouter", "name": "OpenRouter", "secret": KEY}, headers=h)
        assert ok.status_code == 200 and ok.json()["test"]["ok"] and ok.json()["test"]["limit"] == 50
        cid = ok.json()["id"]
        for path in ("/v1/credentials", f"/v1/credentials/{cid}/history", "/v1/installation", "/v1/workspaces/northwind/settings",
                     f"/v1/credentials/{cid}/test"):
            r = c.post(path, headers=h) if path.endswith("/test") else c.get(path, headers=h)
            assert r.status_code == 200, (path, r.text)
            assert KEY not in r.text and KEY[-20:] not in r.text, path
        settings = c.get("/v1/workspaces/northwind/settings", headers=h).json()
        assert settings["credentials"]["model_credential"]["scope"] == "installation"
        assert settings["model"] is not None  # the gateway is live without a restart
    # the broker hands the key only to the gateway, by purpose, and records the access decision once
    assert lyst.llm.available_for("northwind")
    assert lyst.llm.gateway._headers("northwind")["Authorization"] == f"Bearer {KEY}"
    lyst.llm.gateway._headers("northwind")
    ev = lyst.stores.control.query("SELECT action, detail FROM credential_events WHERE action = 'credential.access'")
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["consumer"] == "gateway"
    assert not blobs_contain(lyst, KEY)


def test_replace_validates_first_deletes_the_old_blob_and_the_audit_chain_is_tamper_evident(seeded):
    lyst, owner = seeded
    new_key = KEY.replace("7f3a", "9e1b")
    lyst.credentials.http = openrouter(valid=(KEY, new_key))
    cred = lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="openrouter", name="OpenRouter", secret=KEY)
    old_ref = lyst.stores.control.scalar("SELECT secret_ref FROM credentials WHERE id = ?", [cred["id"]])
    with pytest.raises(ValueError, match="did not pass"):
        lyst.credentials.replace(owner, cred["id"], "or-test-key-not-valid-not-valid")
    assert lyst.credentials.use("openrouter", purpose="inference")[0] == KEY  # the failed replacement changed nothing
    out = lyst.credentials.replace(owner, cred["id"], new_key)
    assert out["version"] == 2
    assert lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs WHERE ref = ?", [old_ref]) == 0  # not retained decryptable
    assert lyst.credentials.use("openrouter", purpose="inference")[0] == new_key  # caches invalidated
    actions = [e["action"] + ":" + e["outcome"] for e in lyst.credentials.history(owner, cred["id"]) if e["action"] != "credential.access"]
    assert actions[:3] == ["credential.connect:ok", "credential.replace:failed", "credential.replace:ok"]
    assert lyst.credentials.verify_audit_chain()["ok"]
    lyst.stores.control.execute("UPDATE credential_events SET detail = '{\"version\": 9}' WHERE action = 'credential.replace' AND outcome = 'ok'")
    verdict = lyst.credentials.verify_audit_chain()
    assert not verdict["ok"] and verdict["broken_at"]


def test_revoke_invalidates_use_and_says_what_lyst_could_not_do_at_the_provider(seeded):
    lyst, owner = seeded
    lyst.credentials.http = openrouter()
    cred = lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="openrouter", name="OpenRouter", secret=KEY)
    assert lyst.credentials.use("openrouter", purpose="inference")
    out = lyst.credentials.revoke(owner, cred["id"])
    assert "OpenRouter dashboard" in out["provider_side"]  # provider-side revocation is not available: reported, not pretended
    assert lyst.credentials.use("openrouter", purpose="inference") is None
    assert lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs") == 0
    assert not lyst.llm.available_for("northwind")
    # an expired or revoked key at the provider is reported by the test, never guessed
    lyst.credentials.http = openrouter(valid=())
    cred2 = lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="openrouter", name="OR2", secret=KEY, test_first=False)
    t = lyst.credentials.test(owner, cred2["id"])
    assert not t["ok"] and t["outcome"] == "authentication failed"


def test_concurrent_replacement_is_detected_and_leaves_no_orphan_blob(seeded, monkeypatch):
    lyst, owner = seeded
    lyst.credentials.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
    cred = lyst.credentials.connect(owner, scope_kind="workspace", scope_id="northwind", provider="rest_token", name="API", secret="first-value-1",
                                    fields={"url": "https://api.test/ping"})
    real = svc_mod.prov.test_credential

    def racing(provider, secret, fields, *, http=None):  # another administrator replaces it between our read and our write
        lyst.stores.control.execute("UPDATE credentials SET version = version + 1 WHERE id = ?", [cred["id"]])
        return real(provider, secret, fields, http=http)

    monkeypatch.setattr(svc_mod.prov, "test_credential", racing)
    before = lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs")
    with pytest.raises(RuntimeError, match="concurrently"):
        lyst.credentials.replace(owner, cred["id"], "second-value-2")
    assert lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs") == before
    assert lyst.credentials.connector_secrets(cred["id"], workspace="northwind")["token"] == "first-value-1"


# -------------------------------------------------------------------------- scopes and policy


def test_cross_tenant_and_privilege_boundaries(seeded):
    lyst, owner = seeded
    other = lyst.guard.create_user("bo@acme.test", "Bo")
    lyst.guard.create_workspace(other, "acme", "Acme")
    editor = lyst.guard.create_user("ed@northwind.test", "Ed")
    lyst.guard.add_member(owner, "northwind", editor.user_id, "editor")
    mine = lyst.credentials.connect(owner, scope_kind="workspace", scope_id="northwind", provider="custom", name="warehouse", secret="nw-secret-01")
    # another workspace's owner: cannot list, test, replace, revoke or read history
    for call in (lambda: lyst.credentials.list(other, scope_kind="workspace", scope_id="northwind"),
                 lambda: lyst.credentials.test(other, mine["id"]),
                 lambda: lyst.credentials.replace(other, mine["id"], "stolen-value-9"),
                 lambda: lyst.credentials.revoke(other, mine["id"]),
                 lambda: lyst.credentials.history(other, mine["id"])):
        with pytest.raises(AccessDenied):
            call()
    # an editor of the same workspace cannot manage its credentials (owners only), nor test them
    with pytest.raises(AccessDenied):
        lyst.credentials.connect(editor, scope_kind="workspace", scope_id="northwind", provider="custom", name="x", secret="editor-tries-1")
    with pytest.raises(AccessDenied):
        lyst.credentials.test(editor, mine["id"])
    # installation scope: only installation administrators (the first user, until they add more)
    assert lyst.credentials.is_installation_admin(owner) and not lyst.credentials.is_installation_admin(other)
    with pytest.raises(AccessDenied):
        lyst.credentials.connect(other, scope_kind="installation", scope_id=None, provider="custom", name="x", secret="escalate-01")
    with pytest.raises(AccessDenied):
        lyst.credentials.set_policy(other, "openrouter", workspace_override=True)
    with pytest.raises(AccessDenied):
        lyst.credentials.add_installation_admin(other, other.user_id)
    # a user cannot create credentials in someone else's name, and one user's credential never serves another
    lyst.credentials.set_policy(owner, "openrouter", user_credentials=True)
    with pytest.raises(AccessDenied):
        lyst.credentials.connect(other, scope_kind="user", scope_id=owner.user_id, provider="custom", name="x", secret="impersonate-1")
    lyst.credentials.connect(other, scope_kind="user", scope_id=other.user_id, provider="openrouter", name="mine", secret=KEY, test_first=False)
    assert lyst.credentials.resolve("openrouter", user=other.user_id)["scope_kind"] == "user"
    assert lyst.credentials.resolve("openrouter", user=owner.user_id) is None
    # denied attempts are in the audit chain with the actor
    denied = lyst.stores.control.query("SELECT actor FROM credential_events WHERE action = 'credential.denied'")
    assert {d["actor"] for d in denied} >= {other.user_id, editor.user_id}
    assert lyst.credentials.verify_audit_chain()["ok"]


def test_resolution_policy_installation_is_authoritative_until_it_says_otherwise(seeded):
    lyst, owner = seeded
    ws_key = KEY.replace("7f3a", "aaaa")
    lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="openrouter", name="Install", secret=KEY, test_first=False)
    lyst.credentials.connect(owner, scope_kind="workspace", scope_id="northwind", provider="openrouter", name="Team", secret=ws_key, test_first=False)
    assert lyst.credentials.use("openrouter", purpose="inference", workspace="northwind")[0] == KEY  # never auto-overridden
    lyst.credentials.set_policy(owner, "openrouter", workspace_override=True)
    assert lyst.credentials.use("openrouter", purpose="inference", workspace="northwind")[0] == ws_key
    assert lyst.credentials.use("openrouter", purpose="inference", workspace="other")[0] == KEY
    assert lyst.credentials.policy()["openrouter"] == {"workspace_override": True, "user_credentials": False}
    # new workspaces follow the installation: external use is on once OpenRouter is connected install-wide
    lyst.guard.create_workspace(owner, "later", "Later")
    assert lyst.stores.control.scalar("SELECT allow_external_llm FROM workspaces WHERE id = 'later'")


def test_recent_authentication_is_required_for_sensitive_actions(seeded):
    lyst, owner = seeded
    stale = lyst.guard.issue_token(owner, "oidc")
    lyst.stores.control.execute("UPDATE api_tokens SET created_at = now() - INTERVAL 2 HOUR WHERE label = 'oidc'")
    hs = {"Authorization": f"Bearer {stale}"}
    hc = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'cli')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get("/v1/credentials", headers=hs).status_code == 200  # reading status is fine
        r = c.post("/v1/credentials", json={"provider": "custom", "name": "t", "secret": "value-value-1"}, headers=hs)
        assert r.status_code == 401 and r.json()["error"] == "reauth_required"
        assert c.post("/v1/credentials", json={"provider": "custom", "name": "t", "secret": "value-value-1"}, headers=hc).status_code == 200
        cid = c.get("/v1/credentials", headers=hc).json()["credentials"][0]["id"]
        assert c.delete(f"/v1/credentials/{cid}", headers=hs).status_code == 401
        assert c.patch("/v1/installation", json={"policy": {"provider": "openrouter", "workspace_override": True}}, headers=hs).status_code == 401


def test_ownership_changes_end_management_rights(seeded):
    lyst, owner = seeded
    second = lyst.guard.create_user("co@northwind.test", "Co")
    lyst.guard.add_member(owner, "northwind", second.user_id, "owner")
    lyst.credentials.connect(second, scope_kind="workspace", scope_id="northwind", provider="custom", name="k", secret="shared-value-1")
    assert lyst.credentials.list(second, scope_kind="workspace", scope_id="northwind")
    lyst.guard.add_member(owner, "northwind", second.user_id, "editor")  # demoted
    with pytest.raises(AccessDenied):
        lyst.credentials.list(second, scope_kind="workspace", scope_id="northwind")
    lyst.guard.remove_member(owner, "northwind", second.user_id)
    lyst.credentials.on_membership_change("northwind", second.user_id)
    with pytest.raises(AccessDenied):
        lyst.credentials.list(second, scope_kind="workspace", scope_id="northwind")
    assert lyst.credentials.list(owner, scope_kind="workspace", scope_id="northwind")[0]["status"] == "active"  # the workspace keeps it


# ----------------------------------------------------------------------- leakage channels


def test_logs_traces_and_audit_never_carry_plaintext(seeded, capsys):
    lyst, owner = seeded
    lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="custom", name="t", secret="leak-me-not-1234")
    handler = logging.StreamHandler()
    logging.getLogger().addHandler(handler)
    try:
        lyst.credentials.redactor.install()
        logging.getLogger("lyst.test").error("connector failed with leak-me-not-1234 in the URL", extra={"url": "x?token=leak-me-not-1234"})
    finally:
        logging.getLogger().removeHandler(handler)
    assert "leak-me-not-1234" not in capsys.readouterr().err
    assert all("leak-me-not-1234" not in r["detail"] for r in lyst.stores.control.query("SELECT detail FROM credential_events"))
    assert not blobs_contain(lyst, "leak-me-not")
    # audit details are scrubbed even when a caller passes the value by mistake
    lyst.credentials._event(owner, "credential.test", None, "installation", None, "ok", {"echo": "leak-me-not-1234"})
    assert "[secret]" in lyst.stores.control.query("SELECT detail FROM credential_events ORDER BY seq DESC LIMIT 1")[0]["detail"]


def test_agents_generated_code_and_mcp_have_no_route_to_the_store(seeded):
    from lyst.mcp_server import build_mcp
    from lyst.skills import run_sandboxed, static_check

    lyst, owner = seeded
    lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="custom", name="t", secret="never-for-agents-1")
    # the MCP surface (what a prompt-injected model could call) has no secret or credential tool
    tools = [t.name for t in build_mcp(lyst)._tool_manager.list_tools()]
    assert tools and not any("secret" in t or "credential" in t for t in tools)
    # generated code is refused statically and, if that were bypassed, at runtime; its process has no key material
    assert any("not allowed" in p for p in static_check("from lyst.secrets import service\ndef run(df): return {}"))
    rep = run_sandboxed("def run(df):\n    import lyst.secrets\n    return {}", [{"columns": [], "rows": []}])
    assert "not allowed" in rep["cases"][0]["error"]
    rep = run_sandboxed("def run(df):\n    return {'names': sorted(__import__('os').environ)}", [{"columns": [], "rows": []}])
    out = rep["cases"][0]
    names = [] if out.get("error") else out["output"]["names"]
    assert not any(n.startswith("LYST_") or n.endswith(("_KEY", "_SECRET", "_TOKEN", "_PASSWORD")) for n in names)  # a scrubbed environment


# ----------------------------------------------------------------------------- connectors


def test_connections_keep_their_secrets_in_the_service_and_bound_to_the_workspace(seeded, monkeypatch):
    from lyst.connectors import CONNECTORS, Connector, Field, Manifest

    lyst, owner = seeded
    seen = {}

    class Fake(Connector):
        manifest = Manifest(kind="fake", label="Fake", fields=(Field("url", "URL"), Field("token", "Token", secret=True)), capabilities=("full",))

        def test(self):
            seen["token"] = self.secrets.get("token")
            return {"ok": True}

    monkeypatch.setitem(CONNECTORS, "fake", Fake)
    conn = lyst.connections.create(owner, "northwind", kind="fake", name="Fake API", values={"url": "https://x", "token": "tok-123456789"})
    assert seen["token"] == "tok-123456789" and conn["credential_id"]
    row = lyst.stores.control.query("SELECT secrets, credential_id FROM connections WHERE id = ?", [conn["id"]])[0]
    assert json.loads(row["secrets"]) == {} and row["credential_id"] == conn["credential_id"]
    assert "tok-1234" not in json.dumps(lyst.connections.list(owner, "northwind"), default=str)
    _, worker = lyst.connections._load(owner, "northwind", conn["id"])
    assert worker.secrets["token"] == "tok-123456789"  # only the connector worker, at use time
    # the credential belongs to northwind: another workspace cannot borrow it through a forged connection row
    other = lyst.guard.create_user("bo@acme.test", "Bo")
    lyst.guard.create_workspace(other, "acme", "Acme")
    lyst.stores.control.execute("INSERT INTO connections (id, workspace_id, kind, name, config, secrets, status, created_by, credential_id) "
                                "VALUES ('c_forged', 'acme', 'fake', 'f', '{\"url\": \"https://x\"}', '{}', 'ok', ?, ?)",
                                [other.user_id, conn["credential_id"]])
    from lyst.connectors import ConnectorError

    with pytest.raises(ConnectorError, match="another workspace"):
        lyst.connections._load(other, "acme", "c_forged")
    assert lyst.stores.control.scalar("SELECT status FROM connections WHERE id = 'c_forged'") == "error"
    lyst.connections.delete(owner, "northwind", conn["id"])
    assert lyst.stores.control.scalar("SELECT status FROM credentials WHERE id = ?", [conn["credential_id"]]) == "revoked"
    assert lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs") == 0


def test_oauth_client_credentials_yield_short_lived_tokens_that_are_refreshed_not_stored(seeded, monkeypatch):
    lyst, owner = seeded
    issued = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            form = dict(p.split("=") for p in request.content.decode().split("&"))
            if form.get("client_secret") != "cs-secret-0001":
                return httpx.Response(401, json={"error": "invalid_client"})
            issued.append(time.time())
            return httpx.Response(200, json={"access_token": f"at-{len(issued)}", "expires_in": 120, "token_type": "Bearer"})
        return httpx.Response(404)

    lyst.credentials.http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="did not pass"):
        lyst.credentials.connect(owner, scope_kind="workspace", scope_id="northwind", provider="oauth_client", name="CRM", secret="wrong-secret-1",
                                 fields={"token_url": "https://idp.test/oauth/token", "client_id": "lyst"})
    cred = lyst.credentials.connect(owner, scope_kind="workspace", scope_id="northwind", provider="oauth_client", name="CRM", secret="cs-secret-0001",
                                    fields={"token_url": "https://idp.test/oauth/token", "client_id": "lyst"})
    assert lyst.credentials.oauth_token(cred["id"], purpose="connector") == "at-2"
    assert lyst.credentials.oauth_token(cred["id"], purpose="connector") == "at-2"  # cached until shortly before expiry
    real_time = time.time
    monkeypatch.setattr(svc_mod.time, "time", lambda: real_time() + 100)  # within 30 s of expiry
    assert lyst.credentials.oauth_token(cred["id"], purpose="connector") == "at-3"
    assert lyst.credentials.connector_secrets(cred["id"], workspace="northwind") == {"token": "at-3"}
    assert not any("at-" in r["detail"] for r in lyst.stores.control.query("SELECT detail FROM credential_events"))
    assert lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs") == 1  # the client secret, never the access tokens


# -------------------------------------------------------------------- master key: loss and rotation


def test_master_key_rotation_rewraps_everything_and_a_failed_rotation_changes_nothing(tmp_path, monkeypatch):
    from lyst import cli
    from lyst.app import Lyst
    from lyst.config import load_settings

    home = tmp_path / "home"
    k1, k2 = "11" * 32, "22" * 32
    monkeypatch.setenv("LYST_HOME", str(home))
    monkeypatch.setenv("LYST_MASTER_KEY", k1)
    app = Lyst.build(load_settings(home))
    owner = app.guard.create_user("o@x.test", "O")
    app.guard.create_workspace(owner, "acme", "Acme")
    app.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="custom", name="t", secret="rotate-me-0001")
    app.close()

    # a rotation that fails mid-swap: the old files and key still work
    real_move = cli.shutil.move
    monkeypatch.setattr(cli.shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        cli.rotate_master_key(new_key=bytes.fromhex(k2), update_anchor=False, yes=True)
    monkeypatch.setattr(cli.shutil, "move", real_move)
    app = Lyst.build(load_settings(home))
    assert app.credentials.use("custom", purpose="check")[0] == "rotate-me-0001"
    app.close()

    # a successful rotation: everything opens under the new key and nothing under the old
    assert cli.rotate_master_key(new_key=bytes.fromhex(k2), update_anchor=False, yes=True) == k2
    with pytest.raises(Exception):  # noqa: B017 - DuckDB refuses the file with the old key
        Lyst.build(load_settings(home))
    monkeypatch.setenv("LYST_MASTER_KEY", k2)
    app = Lyst.build(load_settings(home))
    try:
        assert app.credentials.backend.key_version == 2
        assert app.credentials.use("custom", purpose="check")[0] == "rotate-me-0001"
        assert app.stores.control.scalar("SELECT count(*) FROM workspaces WHERE id = 'acme'") == 1
        assert (home / "control.duckdb.pre-rotation").exists()
    finally:
        app.close()


def test_master_key_loss_is_unrecoverable_by_design_and_said_plainly(tmp_path, monkeypatch):
    from lyst.app import Lyst
    from lyst.config import load_settings

    home = tmp_path / "home"
    monkeypatch.setenv("LYST_MASTER_KEY", "33" * 32)
    app = Lyst.build(load_settings(home))
    app.credentials.connect(app.guard.create_user("o@x.test", "O"), scope_kind="installation", scope_id=None, provider="custom", name="t",
                            secret="gone-with-the-key-1")
    app.close()
    monkeypatch.delenv("LYST_MASTER_KEY")
    settings = load_settings(home)
    assert settings.setup_required  # no silent keyfile appeared
    assert not (home / ".lyst-key").exists()
    monkeypatch.setenv("LYST_ENCRYPT", "0")  # even an unencrypted-file attempt does not expose blobs: they are sealed on their own
    monkeypatch.setenv("LYST_MASTER_KEY", "44" * 32)
    with pytest.raises(Exception):  # noqa: B017 - the encrypted file does not open with another key
        Lyst.build(load_settings(home))


# --------------------------------------------------------------------------------- OpenBao


def test_openbao_backend_kv2_with_approle_and_policy_denials(monkeypatch):
    kv, tokens = {}, {}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/v1/auth/approle/login":
            body = json.loads(request.content)
            if body == {"role_id": "r1", "secret_id": "s1"}:
                tokens["t-1"] = True
                return httpx.Response(200, json={"auth": {"client_token": "t-1", "lease_duration": 3600}})
            return httpx.Response(400)
        if p == "/v1/sys/health":
            return httpx.Response(200, json={"sealed": False, "version": "2.1.0"})
        if request.headers.get("X-Vault-Token") not in tokens:
            return httpx.Response(403)
        if p.startswith("/v1/secret/data/lyst/"):
            key = p.rsplit("/", 1)[1]
            if "denied" in request.headers.get("X-Test", ""):
                return httpx.Response(403)
            if request.method == "POST":
                kv[key] = json.loads(request.content)["data"]
                return httpx.Response(200, json={"data": {"version": 1}})
            if key not in kv:
                return httpx.Response(404)
            return httpx.Response(200, json={"data": {"data": kv[key]}})
        if p.startswith("/v1/secret/metadata/lyst/") and request.method == "DELETE":
            return httpx.Response(204 if kv.pop(p.rsplit("/", 1)[1], None) else 404)
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SecretUnavailable, match="AppRole login failed"):
        OpenBaoBackend("http://bao:8200", role_id="r1", secret_id="bad", http=http).fetch("x")
    bao = OpenBaoBackend("http://bao:8200", role_id="r1", secret_id="s1", http=http)
    ref = bao.store("installation:installation:cred_1:v1", "bao-held-value-1")
    assert ref.backend == "openbao" and bao.fetch("installation:installation:cred_1:v1") == "bao-held-value-1"
    assert bao.health()["ok"] and bao.health()["sealed"] is False
    with pytest.raises(SecretUnavailable, match="no such"):
        bao.fetch("installation:installation:missing:v1")
    # a record served for another reference is refused
    kv[next(iter(kv))]["ref"] = "something-else"
    with pytest.raises(SecretUnavailable, match="another reference"):
        bao.fetch("installation:installation:cred_1:v1")
    assert bao.delete("installation:installation:cred_1:v1") and not bao.delete("installation:installation:cred_1:v1")
    # the backend is chosen from the environment
    from lyst.secrets import backend_from_env

    monkeypatch.setenv("LYST_OPENBAO_URL", "http://bao:8200")
    monkeypatch.setenv("LYST_OPENBAO_TOKEN", "t-1")
    assert isinstance(backend_from_env(None, b"k" * 32), OpenBaoBackend)


# ------------------------------------------------------------------------------- cold start


def test_setup_mode_initialises_only_from_the_engine_host_and_shows_the_recovery_key_once(tmp_path, monkeypatch):
    import sys

    from lyst.secrets import masterkey
    from lyst.security import vault

    store: dict[str, str] = {}

    class KR:
        @staticmethod
        def set_password(service, name, value):
            store[name] = value

        @staticmethod
        def delete_password(service, name):
            store.pop(name, None)

    monkeypatch.setitem(sys.modules, "keyring", KR)
    monkeypatch.setenv("LYST_KEYRING", "1")
    monkeypatch.setattr(vault, "_keyring_get", lambda n: store.get(n))
    monkeypatch.setattr(masterkey, "keyring_available", lambda: "fake")
    monkeypatch.delenv("LYST_MASTER_KEY", raising=False)
    monkeypatch.setenv("LYST_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LYST_MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,testserver")
    app = create_app(None, start_workers=False)
    with TestClient(app, client=("10.1.2.3", 40000)) as remote:
        assert remote.get("/health").json() == {"status": "setup", "version": "0.1.0", "setup_required": True, "oidc": False}
        assert remote.get("/v1/me").status_code == 503
        assert remote.get("/setup/status").json()["loopback"] is False
        assert remote.post("/setup/initialize", json={}).status_code == 403  # not from the engine host
        assert not (tmp_path / "home" / "control.duckdb").exists()
    with TestClient(app) as local:
        r = local.post("/setup/initialize", json={"recovery_key": "ab" * 32})  # a wrong "recovery" key for a fresh home still works...
        assert r.status_code == 200 and r.json()["recovery_key"] is None and store["LYST_MASTER_KEY"] == "ab" * 32
        assert local.get("/health").json()["status"] == "ok"
        assert local.post("/setup/initialize", json={}).status_code == 409  # ...but only once (already initialised)
    store.clear()
    app2 = create_app(None, start_workers=False)
    with TestClient(app2) as local:
        r = local.post("/setup/initialize", json={})  # a generated key does not open an installation made with another one
        assert r.status_code == 400 and "different master key" in r.json()["detail"] and "LYST_MASTER_KEY" not in store
        r = local.post("/setup/initialize", json={"recovery_key": "ab" * 32})
        assert r.status_code == 200 and local.get("/health").json()["status"] == "ok"
    # a fresh installation returns the generated recovery copy exactly once
    store.clear()
    monkeypatch.setenv("LYST_HOME", str(tmp_path / "home2"))
    with TestClient(create_app(None, start_workers=False)) as local:
        r = local.post("/setup/initialize", json={}).json()
        assert r["initialised"] and len(r["recovery_key"]) == 64 and store["LYST_MASTER_KEY"] == r["recovery_key"]
        assert "recovery_key" not in json.dumps(local.get("/setup/status").json())


def test_connect_leaves_no_orphan_blob_and_drops_the_superseded_one(seeded, monkeypatch):
    lyst, owner = seeded
    lyst.credentials.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
    args = {"scope_kind": "workspace", "scope_id": "northwind", "provider": "rest_token", "name": "API", "fields": {"url": "https://api.test/ping"}}
    lyst.credentials.connect(owner, secret="first-value-1", **args)
    before = lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs")
    lyst.credentials.connect(owner, secret="second-value-2", **args)  # replaces the first: its blob goes
    assert lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs") == before
    assert not blobs_contain(lyst, "first-value-1")
    real_write = lyst.stores.control.write
    monkeypatch.setattr(lyst.stores.control, "write", lambda fn, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        lyst.credentials.connect(owner, secret="third-value-3", **args)
    monkeypatch.setattr(lyst.stores.control, "write", real_write)
    assert lyst.stores.control.scalar("SELECT count(*) FROM secret_blobs") == before


def test_redactor_is_bounded_and_the_audit_chain_verifies_in_batches(seeded):
    lyst, _ = seeded
    r = svc_mod.Redactor()
    r.MAX_VALUES = 3
    for v in ("alpha-secret", "bravo-secret", "charlie-secret", "delta-secret"):
        r.register(v)
    assert r.scrub("alpha-secret delta-secret") == "alpha-secret [secret]"  # the oldest left, the newest is masked
    assert lyst.credentials.verify_audit_chain(batch=2) == lyst.credentials.verify_audit_chain()
