# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Connector drafts: connections proposed from an OpenAPI description, tested in a dry run, promoted by another admin."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.connectors.drafts import propose_from_openapi
from lyst.connectors.rest import RestConnector
from lyst.security import AccessDenied

WS = "northwind"
TOKEN = "tok-draft-secret-01"
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Procurement API", "version": "1"},
    "servers": [{"url": "https://api.example.test/v2"}],
    "components": {
        "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        "schemas": {
            "PurchaseOrder": {"type": "object", "properties": {"id": {"type": "integer"}, "supplier": {"type": "string"},
                                                                "amount": {"type": "number"}, "updated_at": {"type": "string"}}},
            "Page": {"type": "object", "properties": {"data": {"type": "array", "items": {"$ref": "#/components/schemas/PurchaseOrder"}},
                                                      "meta": {"type": "object", "properties": {"next_cursor": {"type": "string"}}}}},
        },
    },
    "paths": {
        "/purchase-orders": {"get": {"summary": "List purchase orders", "parameters": [
            {"name": "cursor", "in": "query"}, {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 100}},
            {"name": "updated_after", "in": "query"}],
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Page"}}}}}}},
        "/purchase-orders/{id}": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/PurchaseOrder"}}}}}}},
        "/suppliers": {"get": {"parameters": [{"name": "page", "in": "query"}], "responses": {"200": {"content": {
            "application/json": {"schema": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}}}}}}}},
        "/health": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"type": "object",
                                                                                              "properties": {"ok": {"type": "boolean"}}}}}}}}},
    },
}


def test_proposals_read_lists_pagination_incremental_and_auth():
    props = {p["endpoint"]: p for p in propose_from_openapi(SPEC)}
    assert set(props) == {"/purchase-orders", "/suppliers"}  # item and health endpoints are not lists to sync
    po = props["/purchase-orders"]
    assert po["config"] == {"url": "https://api.example.test/v2/purchase-orders", "records_key": "data", "next_key": "meta.next_cursor",
                            "page_param": "cursor", "page_size_param": "limit", "page_size": "100", "since_param": "updated_after",
                            "auth_header": "X-API-Key"}
    assert po["cursor_column"] == "updated_at" and po["table"] == "purchase_orders" and po["needs_token"]
    assert any("incremental" in w for w in po["why"])
    assert props["/suppliers"]["config"]["page_param"] == "page" and "records_key" not in props["/suppliers"]["config"]
    with pytest.raises(ValueError):
        propose_from_openapi({"paths": {}})


@pytest.fixture
def api(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_REST_ALLOWED_HOSTS", "api.example.test")
    orders = [{"id": i, "supplier": f"S{i % 3}", "amount": 100.0 * i, "updated_at": f"2026-10-{i:02d}"} for i in range(1, 6)]

    def handler(req: httpx.Request):
        if req.headers.get("x-api-key") != TOKEN:
            return httpx.Response(401)
        if req.url.path.endswith("/purchase-orders"):
            start = int(req.url.params.get("cursor") or 0)
            chunk = orders[start:start + 3]
            nxt = str(start + 3) if start + 3 < len(orders) else None
            return httpx.Response(200, json={"data": chunk, "meta": {"next_cursor": nxt}})
        return httpx.Response(200, json=[{"name": "Acme"}])

    RestConnector.http = httpx.Client(transport=httpx.MockTransport(handler))
    admin = lyst.guard.create_user("ada@northwind.test", "Ada")
    lyst.guard.add_member(owner, WS, admin.user_id, "admin")
    yield lyst, owner, admin
    RestConnector.http = None


def test_draft_test_promote_and_sync(api):
    lyst, owner, admin = api
    drafts = {d["endpoint"]: d for d in lyst.connections.drafts.create(owner, WS, name="Procurement", openapi=SPEC)}
    did = drafts["/purchase-orders"]["id"]
    with pytest.raises(ValueError, match="test the draft"):
        lyst.connections.drafts.promote(admin, WS, did, token=TOKEN)
    bad = lyst.connections.drafts.test(owner, WS, did, token="wrong-token-123")
    assert bad["status"] == "draft" and bad["report"]["category"] == "auth" and "Reconnect" in bad["report"]["action"]
    ok = lyst.connections.drafts.test(owner, WS, did, token=TOKEN)
    rep = ok["report"]
    assert ok["status"] == "tested" and rep["pages_read"] == 2 and rep["second_page_differs"] and rep["first_page_rows"] == 3
    assert rep["fields"] == ["amount", "id", "supplier", "updated_at"] and rep["missing_fields"] == []
    blob = json.dumps(lyst.connections.drafts.get(owner, WS, did), default=str)
    assert TOKEN not in blob and "S1" not in blob  # the token is never stored; sample values are masked
    assert lyst.stores.workspace(WS).scalar("SELECT count(*) FROM datasets WHERE name = 'purchase_orders'") == 0  # a dry run
    with pytest.raises(AccessDenied):
        lyst.connections.drafts.promote(owner, WS, did, token=TOKEN)  # the drafter cannot promote their own draft
    out = lyst.connections.drafts.promote(admin, WS, did, token=TOKEN, note="Checked with procurement")
    assert out["status"] == "promoted" and out["cursor_column"] == "updated_at"
    r = lyst.connections.sync(admin, WS, out["connection_id"], source_table=out["table"], cursor_column=out["cursor_column"])
    assert r.rows_added == 5
    assert lyst.data.query(admin, WS, "SELECT sum(amount) FROM purchase_orders").rows == [[1500.0]]
    assert TOKEN not in json.dumps(lyst.connections.list(admin, WS), default=str)
    rej = lyst.connections.drafts.reject(admin, WS, drafts["/suppliers"]["id"], note="Not needed")
    assert rej["status"] == "rejected"
    with pytest.raises(ValueError):
        lyst.connections.drafts.edit(owner, WS, drafts["/suppliers"]["id"], {"page_param": "p"})


def test_edit_resets_testing_and_only_accepts_connector_settings(api):
    lyst, owner, admin = api
    did = lyst.connections.drafts.create(owner, WS, name="Procurement", openapi=SPEC)[0]["id"]
    lyst.connections.drafts.test(owner, WS, did, token=TOKEN)
    d = lyst.connections.drafts.edit(owner, WS, did, {"page_size": "50"})
    assert d["status"] == "draft" and d["proposal"]["config"]["page_size"] == "50"
    with pytest.raises(ValueError, match="not settings"):
        lyst.connections.drafts.edit(owner, WS, did, {"token": "x"})


def test_draft_routes(api):
    lyst, owner, admin = api
    ho = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    ha = {"Authorization": f"Bearer {lyst.guard.issue_token(admin)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        made = c.post(f"/v1/workspaces/{WS}/connector-drafts", json={"name": "Procurement", "openapi": SPEC}, headers=ho).json()
        did = next(d["id"] for d in made if d["endpoint"] == "/purchase-orders")
        assert c.post(f"/v1/workspaces/{WS}/connector-drafts/{did}/test", json={"token": TOKEN}, headers=ho).json()["status"] == "tested"
        assert c.post(f"/v1/workspaces/{WS}/connector-drafts/{did}/promote", json={"token": TOKEN}, headers=ho).status_code == 403
        assert c.post(f"/v1/workspaces/{WS}/connector-drafts/{did}/promote", json={"token": TOKEN}, headers=ha).json()["status"] == "promoted"
        assert len(c.get(f"/v1/workspaces/{WS}/connector-drafts", headers=ho).json()) == 2
        assert c.post(f"/v1/workspaces/{WS}/connector-drafts", json={"name": "x", "openapi": {"a": 1}}, headers=ho).status_code == 400
