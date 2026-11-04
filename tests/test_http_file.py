# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The HTTP file connector: CSV, Parquet and JSON at a URL; host allow-list; size limit; incremental by cursor."""

from __future__ import annotations

import io

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lyst.connectors import ConnectorError
from lyst.connectors.http_file import HttpFileConnector


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setenv("LYST_REST_ALLOWED_HOSTS", "files.example.test")
    rows = [{"id": i, "updated_at": f"2026-02-{i:02d}", "amount": i * 1.5} for i in range(1, 6)]
    state = {"rows": rows, "auth": None}

    def handler(req: httpx.Request) -> httpx.Response:
        state["auth"] = req.headers.get("authorization")
        path = req.url.path
        if path.endswith("sales.csv"):
            body = "id,updated_at,amount\n" + "\n".join(f"{r['id']},{r['updated_at']},{r['amount']}" for r in state["rows"])
            return httpx.Response(200, text=body, headers={"content-type": "text/csv", "etag": '"v1"'})
        if path.endswith("sales.parquet"):
            buf = io.BytesIO()
            pq.write_table(pa.table({k: [r[k] for r in state["rows"]] for k in ("id", "updated_at", "amount")}), buf)
            return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "application/octet-stream"})
        if path.endswith("export"):
            return httpx.Response(200, json={"meta": {"n": len(state["rows"])}, "data": state["rows"]})
        if path.endswith("huge.csv"):
            return httpx.Response(200, text="a\n" + "x\n" * 10, headers={"content-length": str(10**9)})
        return httpx.Response(404)

    HttpFileConnector.http = httpx.Client(transport=httpx.MockTransport(handler))
    yield state
    HttpFileConnector.http = None


def test_csv_parquet_and_json_files_sync_with_incremental_cursor(seeded, served):
    lyst, owner = seeded
    with pytest.raises(ConnectorError, match="not in LYST_REST_ALLOWED_HOSTS"):
        lyst.connections.create(owner, "northwind", kind="http_file", name="x", values={"url": "https://elsewhere.test/sales.csv"})
    with pytest.raises(ConnectorError, match="must be https"):
        lyst.connections.create(owner, "northwind", kind="http_file", name="x", values={"url": "http://files.example.test/sales.csv"})
    with pytest.raises(ConnectorError, match="LYST_HTTP_FILE_MAX_MB"):
        lyst.connections.create(owner, "northwind", kind="http_file", name="x", values={"url": "https://files.example.test/huge.csv"})
    with pytest.raises(ConnectorError, match="returned 404"):
        lyst.connections.create(owner, "northwind", kind="http_file", name="x", values={"url": "https://files.example.test/missing.csv"})

    csv = lyst.connections.create(owner, "northwind", kind="http_file", name="Sales export",
                                  values={"url": "https://files.example.test/exports/sales.csv", "token": "tok-123456789"})
    assert csv["test"]["rows"] == 5 and csv["test"]["columns"] == ["id", "updated_at", "amount"] and csv["test"]["etag"] == '"v1"'
    assert served["auth"] == "Bearer tok-123456789"
    disc = lyst.connections.discover(owner, "northwind", csv["id"])
    assert disc[0]["name"] == "sales" and disc[0]["rows_estimate"] == 5
    r = lyst.connections.sync(owner, "northwind", csv["id"], source_table="sales", cursor_column="updated_at")
    assert r.mode == "full" and r.rows_added == 5 and r.cursor == "2026-02-05"
    served["rows"].append({"id": 6, "updated_at": "2026-02-06", "amount": 9.0})
    r = lyst.connections.sync(owner, "northwind", csv["id"], source_table="sales", cursor_column="updated_at")
    assert r.mode == "incremental" and r.rows_added == 1 and r.rows_total == 6
    assert lyst.data.query(owner, "northwind", "select count(*), round(sum(amount), 1) from sales").rows == [[6, 31.5]]
    r = lyst.connections.sync(owner, "northwind", csv["id"], source_table="sales", cursor_column="updated_at")
    assert r.rows_added == 0 and r.rows_total == 6

    pqc = lyst.connections.create(owner, "northwind", kind="http_file", name="Parquet", values={"url": "https://files.example.test/sales.parquet"})
    assert pqc["test"]["rows"] == 6
    r = lyst.connections.sync(owner, "northwind", pqc["id"], source_table="sales", dataset="sales_pq")
    assert r.rows_added == 6 and lyst.data.query(owner, "northwind", "select max(id) from sales_pq").rows == [[6]]

    js = lyst.connections.create(owner, "northwind", kind="http_file", name="JSON", values={
        "url": "https://files.example.test/api/export", "format": "json", "records_key": "data"})
    assert js["test"]["rows"] == 6
    r = lyst.connections.sync(owner, "northwind", js["id"], source_table="export", cursor_column="id")
    assert r.rows_added == 6 and r.cursor == 6
    served["rows"].append({"id": 7, "updated_at": "2026-02-07", "amount": 1.0})
    assert lyst.connections.sync(owner, "northwind", js["id"], source_table="export", cursor_column="id").rows_added == 1
    assert "tok-1234" not in str(lyst.connections.list(owner, "northwind"))
