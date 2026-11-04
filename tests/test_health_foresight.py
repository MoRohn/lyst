# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data and integration health foresight. Acceptance scenarios C (an upstream schema change: affected work found, the
breakage predicted, a repair validated and proposed for approval) and F (a credential expiring before a scheduled run:
a clear renewal action, the secret never exposed)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.connectors.rest import RestConnector
from lyst.semantic import Measure, MetricSpec

WS = "northwind"
TOKEN = "tok-very-secret-0042"
NOW = lambda: datetime.now(UTC)  # noqa: E731


def _schedule(lyst, owner, title, question, due_in):
    s = lyst.runtime.create_schedule(owner, WS, title, question, 604800)
    lyst.runtime.decide(owner, s["approval_id"], True)
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", [NOW() + due_in, s["schedule_id"]])
    return s["schedule_id"]


@pytest.fixture
def api_source(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_REST_ALLOWED_HOSTS", "api.example.test")
    records = [{"id": i, "updated_at": f"2026-0{1 + i % 9}-01", "amount": i * 10} for i in range(1, 30)]
    RestConnector.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": records})))
    conn = lyst.connections.create(owner, WS, kind="rest", name="Orders API", values={
        "url": "https://api.example.test/v1/orders", "records_key": "data", "auth_header": "X-API-Key", "token": TOKEN})
    lyst.connections.sync(owner, WS, conn["id"], source_table="orders")
    lyst.semantic.define(owner, WS, MetricSpec(name="order_amount", title="Order amount", dataset="orders",
                                               measure=Measure(agg="sum", column="amount"), time_column="updated_at"))
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    yield lyst, owner, conn
    RestConnector.http = None


def _everything(lyst, item_id) -> str:
    db = lyst.stores.workspace(WS)
    rows = db.query("SELECT * FROM anticipations") + db.query("SELECT * FROM anticipation_events") + db.query("SELECT * FROM findings")
    return json.dumps(rows, default=str)


def test_f_expiring_credential_is_flagged_before_the_run_without_exposing_it(api_source):
    lyst, owner, conn = api_source
    cred = conn["credential_id"]
    expires = NOW() + timedelta(days=2)
    lyst.stores.control.execute("UPDATE credentials SET fields = ? WHERE id = ?", [json.dumps({"expires_at": expires.isoformat()}), cred])
    _schedule(lyst, owner, "Weekly orders", "Order amount by month", timedelta(days=3))
    out = lyst.anticipation.cycle(WS)
    [a] = [x for x in lyst.anticipation.list(owner, WS, kind="health_foresight") if x["subject_id"] == conn["id"]]
    assert a["objective"] == "Renew the credential for Orders API" and a["status"] == "surfaced" and out["surfaced"] >= 1
    assert "expires" in a["reasons"][0] and "'Weekly orders'" in a["reasons"][1] and "after it expires" in a["reasons"][1]
    assert a["actions"][0]["id"] == "renew" and a["actions"][0]["href"].endswith(cred)
    assert TOKEN not in _everything(lyst, a["id"])                                   # never the secret
    token = lyst.guard.issue_token(owner)
    with TestClient(create_app(lyst, start_workers=False)) as c:
        body = c.get(f"/v1/workspaces/{WS}/anticipations/{a['id']}", headers={"Authorization": f"Bearer {token}"}).text
        assert TOKEN not in body
    assert a["actions"][0]["credential_id"] == cred and a["actions"][0]["connection_id"] == conn["id"]
    assert lyst.anticipation.act(owner, WS, a["id"], "renew") == {"id": a["id"], "status": "accepted", "open": a["actions"][0]["href"],
                                                                  "credential_id": cred, "connection_id": conn["id"]}


def test_f_renewal_resolves_the_warning(api_source):
    lyst, owner, conn = api_source
    cred = conn["credential_id"]
    lyst.stores.control.execute("UPDATE credentials SET fields = ? WHERE id = ?",
                                [json.dumps({"expires_at": (NOW() + timedelta(days=5)).isoformat()}), cred])
    lyst.anticipation.cycle(WS)
    [a] = [x for x in lyst.anticipation.list(owner, WS, kind="health_foresight") if x["subject_id"] == conn["id"]]
    assert a["status"] == "prepared"                                                   # nothing runs before it expires: not pushed
    lyst.credentials.replace(owner, cred, "tok-new-0043", fields={"expires_at": (NOW() + timedelta(days=90)).isoformat()})
    out = lyst.anticipation.cycle(WS)
    assert out["resolved"] == 1
    assert lyst.stores.workspace(WS).scalar("SELECT status FROM anticipations WHERE id = ?", [a["id"]]) == "resolved"


def test_intermittent_failures_and_rate_limits_are_seen_before_they_stop_a_run(api_source):
    lyst, owner, conn = api_source
    for i, (outcome, cat, retried) in enumerate([("ok", None, ["rate_limit"]), ("failed", "network", None), ("ok", None, ["rate_limit"]),
                                                 ("failed", "timeout", None), ("ok", None, ["rate_limit"])]):
        lyst.stores.control.execute(
            "INSERT INTO connection_events (id, connection_id, workspace_id, action, outcome, category, retried, ms, occurred_at) "
            "VALUES (?, ?, ?, 'read', ?, ?, ?, 300, ?)",
            ["ce_" + uuid.uuid4().hex[:8], conn["id"], WS, outcome, cat, json.dumps(retried) if retried else None,
             NOW() - timedelta(minutes=10 * (5 - i))])
    _schedule(lyst, owner, "Weekly orders", "Order amount by month", timedelta(hours=20))
    lyst.anticipation.cycle(WS)
    items = {x["objective"]: x for x in lyst.anticipation.list(owner, WS, kind="health_foresight")}
    flaky = items["Check the reliability of Orders API"]
    assert "2 of the last" in flaky["reasons"][0] and flaky["status"] == "surfaced"   # a run depends on it within 48 h
    assert "Ease rate limits on Orders API" in items
    r = lyst.anticipation.act(owner, WS, flaky["id"], "test_connection")
    assert r["test"]["ok"] is True


def test_c_schema_change_predicts_breakage_and_proposes_a_validated_fix_for_approval(seeded):
    lyst, owner = seeded
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    _schedule(lyst, owner, "Monthly finance review", "Review revenue for the latest month", timedelta(hours=30))
    before = lyst.stores.workspace(WS).scalar("SELECT current_version FROM metrics WHERE name = 'revenue'")
    frame = demo.pnl().rename(columns={"revenue": "net_revenue"})
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(frame), filename="pnl.csv")     # the upstream rename lands
    [a] = [x for x in lyst.anticipation.list(owner, WS, kind="health_foresight") if x["subject_id"] == "pnl"]
    assert a["status"] == "surfaced" and "removed revenue" in a["reasons"][0]
    assert "revenue uses revenue" in a["reasons"][1] and "'Monthly finance review'" in a["reasons"][1]
    full = lyst.anticipation.get(owner, WS, a["id"])
    fixes = {f["metric"]: f for f in full["prepared"]["fixes"]}
    assert fixes["revenue"]["with"] == "net_revenue" and fixes["revenue"]["validated_rows"] > 0
    assert set(fixes) >= {"revenue", "operating_margin", "cogs_ratio"}
    assert a["actions"][0]["id"] == "propose_fix"
    # nothing changed on its own
    assert lyst.stores.workspace(WS).scalar("SELECT current_version FROM metrics WHERE name = 'revenue'") == before
    r = lyst.anticipation.act(owner, WS, a["id"], "propose_fix")
    assert r["needs_approval"] and len(r["reviews"]) == len(fixes)
    reviews = lyst.collab.reviews(owner, WS, status="pending")
    assert {rv["object_id"] for rv in reviews} >= set(fixes)
    assert lyst.stores.workspace(WS).scalar("SELECT current_version FROM metrics WHERE name = 'revenue'") == before
    admin = lyst.guard.create_user("adm@northwind.test", "Adm")
    lyst.guard.add_member(owner, WS, admin.user_id, "admin")
    rv = next(rv for rv in reviews if rv["object_id"] == "revenue")
    lyst.collab.decide(admin, WS, rv["id"], approve=True)
    assert lyst.stores.workspace(WS).scalar("SELECT current_version FROM metrics WHERE name = 'revenue'") == before + 1
    v = lyst.data.query(owner, WS, "SELECT 1").rows
    assert v


def test_unrelated_schema_change_raises_nothing(seeded):
    lyst, owner = seeded
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    frame = demo.pnl()
    frame["note"] = "x"
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(frame), filename="pnl.csv")     # an added column breaks nothing
    assert lyst.anticipation.list(owner, WS, kind="health_foresight") == []
