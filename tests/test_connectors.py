# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Connector SDK: files connector, PostgreSQL connector against a local server (skipped when none), incremental sync,
read-only enforcement, secrets never returned, scheduled syncs."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from lyst.connectors import CONNECTORS, ConnectorError, manifests
from lyst.security import AccessDenied

from .conftest import ask


def _pg_available():
    try:
        import psycopg

        with psycopg.connect(host="localhost", dbname="postgres", connect_timeout=2) as c:
            c.execute("select 1")
        return True
    except Exception:  # noqa: BLE001
        return False


needs_pg = pytest.mark.skipif(not _pg_available(), reason="no local PostgreSQL on localhost:5432")


@pytest.fixture
def pg_database():
    import psycopg

    name = "lyst_test_" + uuid.uuid4().hex[:8]
    with psycopg.connect(host="localhost", dbname="postgres", autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(host="localhost", dbname=name, autocommit=True) as c:
            c.execute("CREATE TABLE orders (id serial PRIMARY KEY, placed_at timestamp NOT NULL, region text, amount numeric(12,2))")
            c.execute("INSERT INTO orders (placed_at, region, amount) SELECT timestamp '2026-01-01' + (g || ' days')::interval, "
                      "(ARRAY['EMEA','APAC','NA'])[1 + g % 3], 100 + g FROM generate_series(0, 99) g")
            c.execute("CREATE VIEW big_orders AS SELECT * FROM orders WHERE amount > 150")
        yield name
    finally:
        with psycopg.connect(host="localhost", dbname="postgres", autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_manifests_declare_fields_and_secrets():
    kinds = {m["kind"]: m for m in manifests()}
    assert set(kinds) >= {"postgres", "files"}
    pw = next(f for f in kinds["postgres"]["fields"] if f["name"] == "password")
    assert pw["secret"] is True and "incremental" in kinds["postgres"]["capabilities"]


def test_files_connector_is_confined_to_the_import_root(seeded, tmp_path, monkeypatch):
    lyst, owner = seeded
    root = tmp_path / "imports"
    (root / "drop").mkdir(parents=True)
    monkeypatch.setenv("LYST_IMPORT_ROOT", str(root))
    pd.DataFrame({"region": ["EMEA", "APAC"], "target": [0.3, 0.35]}).to_csv(root / "drop" / "targets.csv", index=False)
    pd.DataFrame({"k": [1, 2, 3]}).to_parquet(root / "drop" / "keys.parquet")
    with pytest.raises(ConnectorError, match="outside the import root"):
        lyst.connections.create(owner, "northwind", kind="files", name="bad", values={"path": "/etc"})
    c = lyst.connections.create(owner, "northwind", kind="files", name="Drop folder", values={"path": "drop"})
    assert c["test"]["files"] == 2
    tables = lyst.connections.discover(owner, "northwind", c["id"])
    assert {t["name"] for t in tables} == {"targets.csv", "keys.parquet"}
    r = lyst.connections.sync(owner, "northwind", c["id"], source_table="targets.csv", dataset="region_targets")
    assert r.mode == "full" and r.rows_added == 2 and r.version == 1
    q = lyst.data.query(owner, "northwind", "select region, target from region_targets order by 1")
    assert q.rows == [["APAC", 0.35], ["EMEA", 0.3]]
    # a re-sync of a file is a full reload: same rows, new version only if the bytes changed
    r2 = lyst.connections.sync(owner, "northwind", c["id"], source_table="targets.csv", dataset="region_targets")
    assert r2.version == 1 and r2.rows_total == 2


def test_viewer_cannot_create_connections_and_secrets_stay_hidden(seeded, tmp_path, monkeypatch):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    root = tmp_path / "imports"
    (root / "d").mkdir(parents=True)
    monkeypatch.setenv("LYST_IMPORT_ROOT", str(root))
    with pytest.raises(AccessDenied):
        lyst.connections.create(viewer, "northwind", kind="files", name="x", values={"path": "d"})
    with pytest.raises(ConnectorError, match="Host is required"):
        lyst.connections.create(owner, "northwind", kind="postgres", name="x", values={"dbname": "a", "user": "b"})
    from fastapi.testclient import TestClient

    from lyst.api import create_app

    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        c.post("/v1/workspaces/northwind/connections", json={"kind": "files", "name": "d", "values": {"path": "d"}}, headers=h)
        rows = c.get("/v1/workspaces/northwind/connections", headers=h).json()
        assert rows and "secrets" not in rows[0] and "password" not in str(rows[0])
        assert c.get("/v1/connectors", headers=h).status_code == 200


@needs_pg
def test_postgres_full_then_incremental_sync(seeded, pg_database):
    lyst, owner = seeded
    c = lyst.connections.create(owner, "northwind", kind="postgres", name="Orders DB",
                                values={"host": "localhost", "port": "5432", "dbname": pg_database, "user": os.environ.get("USER", "")})
    assert c["test"]["read_only_transactions"] is True and c["test"]["tables_visible"] >= 2
    tables = {t["ref"]: t for t in lyst.connections.discover(owner, "northwind", c["id"])}
    assert "public.orders" in tables and "public.big_orders" in tables
    assert {col["name"] for col in tables["public.orders"]["columns"]} == {"id", "placed_at", "region", "amount"}
    r1 = lyst.connections.sync(owner, "northwind", c["id"], source_table="public.orders", cursor_column="id")
    assert r1.mode == "full" and r1.rows_added == 100 and r1.rows_total == 100 and r1.cursor == 100 and r1.version == 1
    # nothing new: no new version
    r2 = lyst.connections.sync(owner, "northwind", c["id"], source_table="public.orders", cursor_column="id")
    assert r2.mode == "incremental" and r2.rows_added == 0 and r2.version == 1
    import psycopg

    with psycopg.connect(host="localhost", dbname=pg_database, autocommit=True) as pc:
        pc.execute("INSERT INTO orders (placed_at, region, amount) VALUES ('2026-06-01', 'EMEA', 999), ('2026-06-02', 'APAC', 998)")
    r3 = lyst.connections.sync(owner, "northwind", c["id"], source_table="public.orders", cursor_column="id")
    assert r3.mode == "incremental" and r3.rows_added == 2 and r3.rows_total == 102 and r3.version == 2 and r3.cursor == 102
    q = lyst.data.query(owner, "northwind", "select count(*), sum(amount) from orders where amount > 900")
    assert q.rows == [[2, 1997]]
    # the earlier version is still there, reproducible
    assert lyst.stores.workspace("northwind").scalar('SELECT count(*) FROM data."orders__v1"') == 100
    syncs = lyst.connections.syncs(owner, "northwind", c["id"])
    assert syncs[0]["syncs"] == 3 and syncs[0]["rows_total"] == 102
    # the source stays untouched: the connector's transactions are read only
    conn = CONNECTORS["postgres"]({"host": "localhost", "port": "5432", "dbname": pg_database, "user": os.environ.get("USER", ""),
                                   "schemas": "public"}, {})
    with pytest.raises(Exception, match="read-only"):
        with conn._connect() as pgc, pgc.cursor() as cur:
            cur.execute("DELETE FROM orders")
    with pytest.raises(ConnectorError, match="invalid table"):
        list(conn.read("orders; drop table orders"))
    # a scheduled sync runs through the runtime as its owner
    r = ask(lyst, owner, f"sync connection {c['id']} table public.orders by id")
    assert r["status"] == "done" and "0 rows added (incremental)" in r["result"]["answer"]
    s = lyst.runtime.create_schedule(owner, "northwind", "Sync orders", f"sync connection {c['id']} table public.orders by id", 3600)
    admin = lyst.guard.create_user("adm@n.test", "A")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    lyst.runtime.decide(admin, s["approval_id"], True)
    started = lyst.runtime.tick_schedules(datetime.now(UTC) + timedelta(hours=2))
    lyst.runtime.drain()
    assert lyst.runtime.get(owner, started[0])["status"] == "done"


def test_sqlite_connector_full_and_incremental(seeded, tmp_path, monkeypatch):
    import sqlite3

    lyst, owner = seeded
    root = tmp_path / "imports"
    root.mkdir()
    monkeypatch.setenv("LYST_IMPORT_ROOT", str(root))
    db = root / "crm.sqlite"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE deals (id INTEGER PRIMARY KEY, stage TEXT, amount REAL)")
        c.executemany("INSERT INTO deals (stage, amount) VALUES (?, ?)", [("won", 100.0), ("lost", 50.0), ("open", 75.0)])
    conn = lyst.connections.create(owner, "northwind", kind="sqlite", name="CRM export", values={"path": "crm.sqlite"})
    assert conn["test"]["read_only"] and conn["test"]["tables_visible"] == 1
    assert lyst.connections.discover(owner, "northwind", conn["id"])[0]["name"] == "deals"
    r = lyst.connections.sync(owner, "northwind", conn["id"], source_table="deals", cursor_column="id")
    assert r.rows_added == 3 and r.cursor == 3
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO deals (stage, amount) VALUES ('won', 200.0)")
    r = lyst.connections.sync(owner, "northwind", conn["id"], source_table="deals", cursor_column="id")
    assert r.mode == "incremental" and r.rows_added == 1 and r.rows_total == 4 and r.version == 2
    assert lyst.data.query(owner, "northwind", "select sum(amount) from deals where stage = 'won'").rows == [[300.0]]
    with pytest.raises(ConnectorError, match="outside the import root"):
        lyst.connections.create(owner, "northwind", kind="sqlite", name="x", values={"path": "/etc/hosts"})


def test_rest_connector_paginates_and_syncs(seeded, monkeypatch):
    import httpx

    from lyst.connectors.rest import RestConnector

    lyst, owner = seeded
    monkeypatch.setenv("LYST_REST_ALLOWED_HOSTS", "api.example.test")
    records = [{"id": i, "updated_at": f"2026-01-{i:02d}", "owner": {"name": f"p{i}"}, "value": i * 10} for i in range(1, 8)]

    def handler(req: httpx.Request):
        assert req.headers.get("x-api-key") == "secret-key"
        page = int(req.url.params.get("page", "1"))
        since = req.url.params.get("updated_after")
        pool = [r for r in records if not since or r["updated_at"] > since]
        chunk = pool[(page - 1) * 3: page * 3]
        return httpx.Response(200, json={"data": chunk, "meta": {"page": page}})

    RestConnector.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ConnectorError, match="not in LYST_REST_ALLOWED_HOSTS"):
            lyst.connections.create(owner, "northwind", kind="rest", name="x", values={"url": "https://other.test/v1/items"})
        with pytest.raises(ConnectorError, match="must be https"):
            lyst.connections.create(owner, "northwind", kind="rest", name="x", values={"url": "http://api.example.test/v1/items"})
        conn = lyst.connections.create(owner, "northwind", kind="rest", name="Items API", values={
            "url": "https://api.example.test/v1/items", "records_key": "data", "page_param": "page", "auth_header": "X-API-Key",
            "token": "secret-key", "since_param": "updated_after"})
        assert conn["test"]["records_in_first_page"] == 3 and "owner" in conn["test"]["fields"]
        assert lyst.connections.discover(owner, "northwind", conn["id"])[0]["name"] == "items"
        r = lyst.connections.sync(owner, "northwind", conn["id"], source_table="items", cursor_column="updated_at")
        assert r.rows_added == 7 and r.cursor == "2026-01-07"
        q = lyst.data.query(owner, "northwind", "select count(*), sum(value) from items")
        assert q.rows == [[7, 280]]
        assert lyst.data.query(owner, "northwind", "select owner from items where id = 1").rows == [['{"name": "p1"}']]
        records.append({"id": 8, "updated_at": "2026-01-08", "owner": {"name": "p8"}, "value": 80})
        r = lyst.connections.sync(owner, "northwind", conn["id"], source_table="items", cursor_column="updated_at")
        assert r.mode == "incremental" and r.rows_added == 1 and r.rows_total == 8
        assert "secret-key" not in str(lyst.connections.list(owner, "northwind"))
    finally:
        RestConnector.http = None


def test_rest_token_never_follows_a_next_link_to_another_host_and_internal_hosts_are_refused(seeded, monkeypatch):
    import httpx

    from lyst.connectors.rest import RestConnector

    lyst, owner = seeded
    monkeypatch.delenv("LYST_REST_ALLOWED_HOSTS", raising=False)
    seen: list[str] = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        return httpx.Response(200, json={"data": [{"id": 1}], "next": "https://attacker.example/steal"})

    RestConnector.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        conn = RestConnector({"url": "https://api.example.test/v1/items", "records_key": "data", "next_key": "next"},
                             {"token": "tok-123456"})
        with pytest.raises(ConnectorError, match="another host"):
            list(conn.read("items"))
        assert all("attacker" not in u for u in seen)
        for url in ("https://127.0.0.1/x", "https://169.254.169.254/latest/meta-data", "https://[::1]/x"):
            with pytest.raises(ConnectorError, match="internal address"):
                list(RestConnector({"url": url}, {}).read("x"))
        monkeypatch.setenv("LYST_REST_ALLOW_PRIVATE", "1")
        assert list(RestConnector({"url": "https://127.0.0.1/x", "records_key": "data"}, {}).read("x"))
    finally:
        RestConnector.http = None


def test_rest_read_honours_batch_rows_and_mixed_cursor_types(monkeypatch):
    import httpx

    from lyst.connectors.rest import RestConnector

    monkeypatch.setenv("LYST_REST_ALLOWED_HOSTS", "api.example.test")
    pages = [[{"id": 1, "t": 5}, {"id": 2, "t": "2026-01-02"}], [{"id": 3, "t": 9, "extra": "x"}], [{"id": 4, "t": 10}], []]

    def handler(req: httpx.Request):
        return httpx.Response(200, json=pages[int(req.url.params.get("page", "1")) - 1])

    RestConnector.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        conn = RestConnector({"url": "https://api.example.test/v1/items", "page_param": "page"}, {})
        tables = list(conn.read("items", batch_rows=3))
        assert [t.num_rows for t in tables] == [3, 1]
        assert "extra" in tables[0].column_names  # a key missing from some records is a null there
        # the stored cursor is an int and one record's is a string: compared, not a TypeError mid-sync
        assert sum(t.num_rows for t in conn.read("items", cursor_column="t", after=6)) >= 2
    finally:
        RestConnector.http = None


def test_short_secrets_are_scrubbed_where_they_stand_alone():
    from lyst.connectors import scrub

    assert scrub("login failed for pin 931 (try 9310)", {"pin": "931"}) == "login failed for pin [secret] (try 9310)"
    assert scrub("bad password hunter22", {"password": "hunter22"}) == "bad password [secret]"


def test_sync_spools_batches_and_stores_under_the_normalised_name(seeded, monkeypatch):
    """A sync never concatenates the source in memory: every batch goes to disk and DuckDB scans the files."""
    import pyarrow as pa

    import lyst.connectors as connectors

    lyst, owner = seeded
    calls = []

    class Many(connectors.Connector):
        manifest = connectors.Manifest(kind="many_test", label="many", fields=())

        def test(self):
            return {"ok": True}

        def discover(self):
            return []

        def read(self, table, *, cursor_column=None, after=None, batch_rows=50_000):
            calls.append(after)
            for i in range(5):
                if after is not None and i < 5:
                    continue
                yield pa.table({"Id": list(range(i * 100, i * 100 + 100)), "Updated": [i] * 100,
                                **({"Note": ["x"] * 100} if i == 3 else {})})

    monkeypatch.setitem(connectors.CONNECTORS, "many_test", Many)
    monkeypatch.setattr(pa, "concat_tables", lambda *a, **k: (_ for _ in ()).throw(AssertionError("concatenated in memory")))
    conn = lyst.connections.create(owner, "northwind", kind="many_test", name="Many", values={})
    r = lyst.connections.sync(owner, "northwind", conn["id"], source_table="t", dataset="Big Table", cursor_column="Updated")
    assert r.dataset == "big_table" and r.rows_added == 500 and r.cursor == 4
    q = lyst.data.query(owner, "northwind", "select count(*), count(note), max(id) from big_table")
    assert q.rows == [[500, 100, 499]]
    r = lyst.connections.sync(owner, "northwind", conn["id"], source_table="t", dataset="Big Table", cursor_column="Updated")
    assert r.mode == "incremental" and r.rows_added == 0 and r.rows_total == 500
    assert lyst.connections.syncs(owner, "northwind", conn["id"])[0]["last_schema"]
