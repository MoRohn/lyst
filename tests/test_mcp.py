# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst as an MCP server: the same tokens, roles, guard and audit as the API."""

import json

from fastapi.testclient import TestClient

from lyst.api import create_app

HEAD = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _call(c, token, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    h = {**HEAD, "MCP-Protocol-Version": "2025-06-18"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = c.post("/mcp/", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _result(resp):
    assert "error" not in resp, resp
    res = resp["result"]
    if "structuredContent" in res:
        return res["structuredContent"]
    return json.loads(res["content"][0]["text"])


def test_mcp_tools_run_under_the_callers_roles(seeded):
    lyst, owner = seeded
    tok = lyst.guard.issue_token(owner)
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    vtok = lyst.guard.issue_token(viewer)
    with TestClient(create_app(lyst, start_workers=False)) as c:
        tools = _call(c, tok, "tools/list")["result"]["tools"]
        assert {t["name"] for t in tools} == {"ask", "workspaces", "datasets", "metrics", "query", "findings", "task", "search", "metric_lineage",
                                                   "events", "ingest_file"}
        ws = _result(_call(c, tok, "tools/call", {"name": "workspaces", "arguments": {}}))
        assert ws["result"][0]["id"] == "northwind" if "result" in ws else ws[0]["id"] == "northwind"
        m = _result(_call(c, tok, "tools/call", {"name": "metrics", "arguments": {"workspace": "northwind"}}))
        names = {x["name"] for x in (m["result"] if isinstance(m, dict) and "result" in m else m)}
        assert "operating_margin" in names
        a = _result(_call(c, tok, "tools/call", {"name": "ask", "arguments": {"workspace": "northwind",
                                                                                 "question": "What was revenue last quarter?"}}))
        assert a["status"] == "done" and "Q3 2026" in a["result"]["answer"] and a["result"]["supporting"]["queries"]
        q = _result(_call(c, tok, "tools/call", {"name": "query", "arguments": {"workspace": "northwind",
                                                                                   "sql": "select count(*) from pnl"}}))
        assert q["rows"] == [[396]]
        # the guard holds: a write is refused, and a viewer cannot see a restricted column
        bad = _call(c, tok, "tools/call", {"name": "query", "arguments": {"workspace": "northwind", "sql": "drop table pnl"}})
        assert bad["result"].get("isError") and "only read queries" in bad["result"]["content"][0]["text"]
        lyst.data.set_column_policy(owner, "northwind", "pnl", "opex", "confidential", "admin")
        restricted = _call(c, vtok, "tools/call", {"name": "query", "arguments": {"workspace": "northwind", "sql": "select sum(opex) from pnl"}})
        assert restricted["result"].get("isError") and "restricted" in restricted["result"]["content"][0]["text"]
        # no token, no service; an outsider sees nothing
        anon = _call(c, None, "tools/call", {"name": "workspaces", "arguments": {}})
        assert anon["result"].get("isError") and "credentials" in anon["result"]["content"][0]["text"]
        outsider = lyst.guard.issue_token(lyst.guard.create_user("o@x.test", "O"))
        denied = _call(c, outsider, "tools/call", {"name": "datasets", "arguments": {"workspace": "northwind"}})
        assert denied["result"].get("isError") and "may not" in denied["result"]["content"][0]["text"]
    assert lyst.stores.control.scalar("SELECT count(*) FROM audit_log WHERE outcome = 'denied'") >= 1  # the outsider


def test_mcp_hardening_annotations_audit_truncation_and_files(seeded, monkeypatch):
    import base64

    lyst, owner = seeded
    tok = lyst.guard.issue_token(owner)
    monkeypatch.setenv("LYST_MCP_MAX_BYTES", "2000")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        tools = {t["name"]: t for t in _call(c, tok, "tools/list")["result"]["tools"]}
        assert tools["query"]["annotations"]["readOnlyHint"] is True
        assert tools["ask"]["annotations"]["readOnlyHint"] is False
        assert tools["ingest_file"]["annotations"]["destructiveHint"] is False
        big = _result(_call(c, tok, "tools/call", {"name": "query", "arguments": {
            "workspace": "northwind", "sql": "SELECT * FROM pnl", "max_rows": 500}}))
        assert big["truncated"] is True and len(big["rows"]) < big["total_rows"]
        csv = base64.b64encode(b"store,sales\nLeeds,10\nYork,12\n").decode()
        plan = _result(_call(c, tok, "tools/call", {"name": "ingest_file", "arguments": {
            "workspace": "northwind", "filename": "stores.csv", "content_base64": csv}}))
        assert plan["tables"][0]["rows"] == 2 and plan["status"] == "staged"
        loaded = _result(_call(c, tok, "tools/call", {"name": "ingest_file", "arguments": {
            "workspace": "northwind", "filename": "stores.csv", "content_base64": csv, "load": True}}))
        assert loaded["datasets"][0]["dataset"] == "stores"
        bad = _call(c, tok, "tools/call", {"name": "ingest_file", "arguments": {
            "workspace": "northwind", "filename": "x.csv", "content_base64": "%%%"}})
        assert bad["result"]["isError"] is True
        ev = _result(_call(c, tok, "tools/call", {"name": "events", "arguments": {"workspace": "northwind", "types": ["dataset.*"]}}))
        assert any(e["subject"]["id"] == "stores" for e in ev["events"])
    actions = {r["action"]: r["outcome"] for r in lyst.stores.control.query(
        "SELECT action, outcome FROM audit_log WHERE action LIKE 'mcp.%'")}
    assert actions["mcp.query"] == "ok" and actions["mcp.ingest_file"] in ("ok", "rejected")
