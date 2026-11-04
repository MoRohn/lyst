from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import duckdb
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from lyst_waitlist.app import create_app
from lyst_waitlist.config import ConfigError, Settings
from lyst_waitlist.store import Store

ORIGIN = "https://morohn.github.io"


def make(tmp_path, **over):
    base = dict(
        db_path=tmp_path / "w.duckdb",
        allowed_origins=(ORIGIN,),
        hash_key=b"k" * 32,
        encryption_key=Fernet.generate_key(),
        rate_limit=(100, 600),
    )
    base.update(over)
    settings = Settings(**base)
    app = create_app(settings)
    return TestClient(app, client=("203.0.113.9", 5000)), app.state.store


def body(**over):
    b = {"kind": "early_access", "email": "Ana@Example.com", "consent": True, "consent_version": "2026-11-04"}
    b.update(over)
    return b


def post(client, payload, origin=ORIGIN):
    return client.post("/v1/registrations", json=payload, headers={"Origin": origin})


def test_success_stores_encrypted_record(tmp_path):
    client, store = make(tmp_path)
    r = post(client, body(role="Business analyst", organization="Acme", interest="Data pipelines"))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert store.count() == 1
    rec = store.export()[0]
    assert rec["email"] == "ana@example.com"
    assert rec["organization"] == "Acme"
    assert rec["consent_scope"] == "early_access_updates"
    assert rec["consent_version"] == "2026-11-04"
    # ciphertext at rest
    with store._lock:
        stored = store._con.execute("SELECT email, organization FROM registrations").fetchone()
    assert "ana@example.com" not in stored[0] and stored[1] != "Acme"


def test_duplicate_is_idempotent_and_indistinguishable(tmp_path):
    client, store = make(tmp_path)
    a = post(client, body())
    b = post(client, body(email="  ana@EXAMPLE.com "))
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert store.count() == 1


@pytest.mark.parametrize(
    "payload,field,code",
    [
        (body(email=""), "email", "email_required"),
        (body(email="not-an-email"), "email", "email_invalid"),
        (body(email="a@b"), "email", "email_invalid"),
        (body(consent=False), "consent", "consent_required"),
        (body(role="CEO of everything"), "role", "invalid"),
        (body(kind="design_partner"), "organization", "organization_required"),
        (body(kind="admin"), "kind", "invalid"),
    ],
)
def test_invalid_payloads(tmp_path, payload, field, code):
    client, store = make(tmp_path)
    r = post(client, payload)
    assert r.status_code == 422
    assert r.json()["errors"][field] == code
    assert store.count() == 0


def test_empty_body_and_wrong_type(tmp_path):
    client, _ = make(tmp_path)
    assert client.post("/v1/registrations", content=b"", headers={"Origin": ORIGIN, "Content-Type": "application/json"}).status_code == 422
    assert client.post("/v1/registrations", content=b"email=a", headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert post(client, ["not", "an", "object"]).status_code == 422


def test_honeypot_not_stored(tmp_path):
    client, store = make(tmp_path)
    r = post(client, body(website="http://spam"))
    assert r.status_code == 200
    assert store.count() == 0


def test_disallowed_origin(tmp_path):
    client, store = make(tmp_path)
    r = post(client, body(), origin="https://evil.example")
    assert r.status_code == 403
    assert store.count() == 0


def test_cors_preflight(tmp_path):
    client, _ = make(tmp_path)
    ok = client.options(
        "/v1/registrations",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
    )
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ORIGIN
    bad = client.options(
        "/v1/registrations",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in bad.headers


def test_rate_limit(tmp_path):
    client, store = make(tmp_path, rate_limit=(3, 600))
    codes = [post(client, body(email=f"u{i}@example.com")).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert store.count() == 3


def test_body_too_large(tmp_path):
    client, _ = make(tmp_path)
    r = post(client, body(message="x" * 20000, kind="design_partner", organization="A"))
    assert r.status_code == 413


def test_design_partner(tmp_path):
    client, store = make(tmp_path)
    r = post(client, body(kind="design_partner", organization="Acme", team_size="6–20", message="Month-end close"))
    assert r.status_code == 200
    rec = store.export()[0]
    assert rec["kind"] == "design_partner" and rec["message"] == "Month-end close"
    assert rec["consent_scope"] == "design_partner_contact"
    # the same email may also be on the early-access list
    assert post(client, body()).status_code == 200
    assert store.count() == 2


def test_store_failure_reports_truthfully(tmp_path):
    client, store = make(tmp_path)

    def boom(_):
        raise RuntimeError("disk full")

    store.add = boom
    r = post(client, body())
    assert r.status_code == 503
    assert "Nothing was stored" in r.json()["message"]


def test_concurrent_writes_single_writer(tmp_path):
    client, store = make(tmp_path, rate_limit=(1000, 600))
    emails = [f"user{i}@example.com" for i in range(40)] * 2  # each twice
    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(lambda e: post(client, body(email=e)).status_code, emails))
    assert set(codes) == {200}
    assert store.count() == 40


def test_remove_and_purge(tmp_path):
    client, store = make(tmp_path)
    post(client, body())
    post(client, body(kind="design_partner", organization="Acme"))
    assert store.remove("ANA@example.com") == 2
    assert store.count() == 0
    post(client, body(email="old@example.com"))
    assert store.purge_older_than(0) == 1


def test_no_read_endpoints(tmp_path):
    client, _ = make(tmp_path)
    assert client.get("/v1/registrations").status_code == 405
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"}


def test_logs_do_not_contain_email(tmp_path, capsys):
    client, _ = make(tmp_path)
    post(client, body(email="secret.person@example.com"))
    out = capsys.readouterr().out
    assert "secret.person" not in out
    assert any(json.loads(line).get("event") == "registered" for line in out.splitlines() if line.startswith("{"))


def test_production_requires_keys():
    with pytest.raises(ConfigError):
        Settings.from_env({"LYST_WAITLIST_ALLOWED_ORIGINS": ORIGIN})
    with pytest.raises(ConfigError):
        Settings.from_env({"LYST_WAITLIST_ALLOWED_ORIGINS": ORIGIN, "LYST_WAITLIST_HASH_KEY": "x" * 40})
    s = Settings.from_env(
        {
            "LYST_WAITLIST_ALLOWED_ORIGINS": ORIGIN,
            "LYST_WAITLIST_HASH_KEY": "x" * 40,
            "LYST_WAITLIST_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        }
    )
    assert s.allowed_origins == (ORIGIN,)


def test_store_reopens(tmp_path):
    key = Fernet.generate_key()
    s = Store(tmp_path / "x.duckdb", b"k" * 32, key)
    from lyst_waitlist.store import Registration

    s.add(Registration(kind="early_access", email="a@example.com", consent_version="2026-11-04"))
    s.close()
    s2 = Store(tmp_path / "x.duckdb", b"k" * 32, key)
    assert s2.count() == 1 and s2.export()[0]["email"] == "a@example.com"
