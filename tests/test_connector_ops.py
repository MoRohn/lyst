# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Connector operations: one error vocabulary, shared retries, events, health with required actions, schema changes
on sync. Acceptance scenario D (integration failure: an expired credential and a changed schema) runs here."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.connectors import ConnectorError, manifests
from lyst.connectors.reliability import ACTIONS, Attempted, RetryPolicy, classify, run_with_retry
from lyst.connectors.rest import RestConnector
from lyst.contracts import ContractSpec

WS = "northwind"
TOKEN = "tok-secret-0001"


@pytest.mark.parametrize(("message", "category"), [
    ("password authentication failed for user \"lyst\"", "auth"),
    ("could not connect: connection failed: FATAL:  role \"nobody\" does not exist", "auth"),
    ("could not connect: connection failed: FATAL:  database \"nope\" does not exist", "config"),
    ("https://x/v1 returned 401", "auth"),
    ("permission denied for table orders", "permission"),
    ("https://x/v1 returned 429", "rate_limit"),
    ("could not connect to server: Connection refused", "network"),
    ("canceling statement due to statement timeout", "timeout"),
    ("relation \"orders\" does not exist", "not_found"),
    ("column \"amount\" does not exist", "schema"),
    ("https://x/v1 returned 503", "source"),
    ("Host is required", "config"),
    ("something odd", "unknown"),
])
def test_errors_are_classified(message, category):
    assert classify(message) == category
    assert ACTIONS[category]


def test_retries_only_transient_failures_and_honour_retry_after():
    waits: list[float] = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectorError("slow down", category="rate_limit", retry_after=2)
        return "ok"

    rec = Attempted()
    assert run_with_retry(flaky, RetryPolicy(attempts=3), sleep=waits.append, record=rec) == "ok"
    assert waits == [2.0, 2.0] and rec.attempts == 3 and rec.failures == ["rate_limit", "rate_limit"]
    calls["n"] = 0

    def denied():
        calls["n"] += 1
        raise ConnectorError("returned 403")

    with pytest.raises(ConnectorError):
        run_with_retry(denied, RetryPolicy(attempts=5), sleep=waits.append)
    assert calls["n"] == 1  # a permission error is never retried


def test_manifests_declare_auth_and_retry_policy():
    m = {x["kind"]: x for x in manifests()}
    assert m["postgres"]["auth"] == "password" and m["rest"]["auth"] == "token" and m["files"]["auth"] == "none"
    assert all(x["read_only"] and x["retry_attempts"] >= 1 for x in m.values())


class Source:
    """A REST source whose behaviour a test changes between calls."""

    def __init__(self):
        self.records = [{"id": i, "updated_at": f"2026-01-{i:02d}", "amount": i * 10} for i in range(1, 6)]
        self.fail: list[int] = []      # status codes to return before succeeding, consumed one per request
        self.token = TOKEN
        self.retry_after = None

    def __call__(self, req: httpx.Request):
        if req.headers.get("x-api-key") != self.token:
            return httpx.Response(401, json={"error": "token expired"})
        if self.fail:
            code = self.fail.pop(0)
            return httpx.Response(code, headers={"retry-after": "1"} if code == 429 else {})
        since = req.url.params.get("updated_after")
        return httpx.Response(200, json={"data": [r for r in self.records if not since or r["updated_at"] > since]})


@pytest.fixture
def rest(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_REST_ALLOWED_HOSTS", "api.example.test")
    src = Source()
    RestConnector.http = httpx.Client(transport=httpx.MockTransport(src))
    waits: list[float] = []
    lyst.connections.sleep = waits.append
    conn = lyst.connections.create(owner, WS, kind="rest", name="Orders API", values={
        "url": "https://api.example.test/v1/orders", "records_key": "data", "auth_header": "X-API-Key", "token": TOKEN,
        "since_param": "updated_after"})
    yield lyst, owner, conn, src, waits
    RestConnector.http = None


def test_rate_limits_are_retried_and_reported_in_health(rest):
    lyst, owner, conn, src, waits = rest
    src.fail = [429]
    r = lyst.connections.sync(owner, WS, conn["id"], source_table="orders", cursor_column="updated_at")
    assert r.rows_added == 5 and waits == [1.0]
    ev = lyst.connections.events(owner, WS, conn["id"])[0]
    assert ev["action"] == "read" and ev["outcome"] == "ok" and ev["attempts"] == 2 and ev["retried"] == ["rate_limit"]
    h = lyst.connections.health(owner, WS, conn["id"])[0]
    assert h["state"] == "degraded" and h["last_24h"]["rate_limited"] == 1 and h["required_action"] == ACTIONS["rate_limit"]
    assert h["tables"][0]["dataset"] == "orders" and h["tables"][0]["mode"] == "incremental"
    assert h["tables"][0]["data_freshness"]["latest"] == "2026-01-05"


def test_repeated_network_failures_mark_the_connection_failing_and_notify(rest):
    lyst, owner, conn, src, waits = rest
    for _ in range(3):
        src.fail = [503, 503, 503]
        with pytest.raises(ConnectorError):
            lyst.connections.test(owner, WS, conn["id"])
    assert len(waits) == 6  # two waits per operation, three attempts each
    h = lyst.connections.health(owner, WS, conn["id"])[0]
    assert h["state"] == "failing" and h["consecutive_failures"] == 3 and h["failure_category"] == "source"
    inbox = lyst.monitors.inbox(owner, WS)
    assert inbox[0]["kind"] == "connection_needs_action" and "Orders API" in inbox[0]["summary"]
    lyst.connections.test(owner, WS, conn["id"])
    h = lyst.connections.health(owner, WS, conn["id"])[0]
    assert h["consecutive_failures"] == 0 and h["state"] == "healthy"  # recovered; the day's errors stay in the counts
    assert h["last_24h"]["errors"] == 3


def test_d_expired_credential_needs_action_and_replacing_it_recovers(rest):
    lyst, owner, conn, src, waits = rest
    lyst.connections.sync(owner, WS, conn["id"], source_table="orders", cursor_column="updated_at")
    src.token = "tok-rotated-0002"  # the provider expired the old token
    with pytest.raises(ConnectorError) as exc:
        lyst.connections.sync(owner, WS, conn["id"], source_table="orders", cursor_column="updated_at")
    assert exc.value.category == "auth" and waits == []  # never retried
    h = lyst.connections.health(owner, WS, conn["id"])[0]
    assert h["state"] == "needs_action" and h["required_action"] == ACTIONS["auth"] and "401" in " ".join(h["reasons"])
    assert any(n["kind"] == "connection_needs_action" for n in lyst.monitors.inbox(owner, WS))
    blob = json.dumps(h, default=str) + json.dumps(lyst.connections.events(owner, WS, conn["id"]), default=str)
    assert TOKEN not in blob and "tok-rotated" not in blob
    # recovery: the owner replaces the credential in Settings (rotation re-tests through the connector)
    out = lyst.credentials.replace(owner, conn["credential_id"], "tok-rotated-0002")
    assert out.get("test", out).get("ok", True)
    r = lyst.connections.sync(owner, WS, conn["id"], source_table="orders", cursor_column="updated_at")
    assert r.mode == "incremental"
    assert lyst.connections.health(owner, WS, conn["id"])[0]["state"] in ("healthy", "degraded")
    assert lyst.connections.health(owner, WS, conn["id"])[0]["consecutive_failures"] == 0


def test_revoked_credential_is_an_auth_failure(rest):
    lyst, owner, conn, src, waits = rest
    lyst.credentials.revoke(owner, conn["credential_id"])
    with pytest.raises(ConnectorError):
        lyst.connections.test(owner, WS, conn["id"])
    h = lyst.connections.health(owner, WS, conn["id"])[0]
    assert h["state"] == "needs_action" and h["failure_category"] == "auth" and h["credential"]["status"] == "revoked"


def test_d_schema_change_on_incremental_sync_reloads_and_breaks_the_contract(rest):
    lyst, owner, conn, src, waits = rest
    lyst.connections.sync(owner, WS, conn["id"], source_table="orders", cursor_column="updated_at")
    lyst.contracts.define(owner, WS, "orders", ContractSpec.model_validate(lyst.contracts.draft(owner, WS, "orders")))
    # the source starts sending amount as text and adds a currency column
    src.records = [{**r, "amount": f"{r['amount']} USD", "currency": "USD"} for r in src.records]
    src.records.append({"id": 6, "updated_at": "2026-01-06", "amount": "60 USD", "currency": "USD"})
    r = lyst.connections.sync(owner, WS, conn["id"], source_table="orders", cursor_column="updated_at")
    change = r.extra["schema_change"]
    assert change["retyped"] == ["amount"] and change["added"] == ["currency"]
    assert r.mode == "full" and r.rows_total == 6  # reloaded in full rather than mixing types
    kinds = [e["action"] for e in lyst.connections.events(owner, WS, conn["id"])]
    assert "schema_change" in kinds
    check = lyst.contracts.checks(owner, WS, "orders")[0]
    assert check["outcome"] == "breaking" and any(x["kind"] == "type_changed" for x in check["results"])
    h = lyst.connections.health(owner, WS, conn["id"])[0]
    assert h["state"] == "degraded" and any("contract" in x for x in h["reasons"]) and h["required_action"] == ACTIONS["schema"]


def test_health_and_events_over_the_api(rest):
    lyst, owner, conn, src, waits = rest
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(viewer)}"}
    lyst.connections.sync(owner, WS, conn["id"], source_table="orders")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        allh = c.get(f"/v1/workspaces/{WS}/connections/health", headers=h).json()
        assert allh[0]["id"] == conn["id"] and allh[0]["state"] == "healthy"
        one = c.get(f"/v1/workspaces/{WS}/connections/{conn['id']}/health", headers=h).json()
        assert one["tables"][0]["mode"] == "full"
        ev = c.get(f"/v1/workspaces/{WS}/connections/{conn['id']}/events", headers=h).json()
        assert {e["action"] for e in ev} >= {"read"}
        assert TOKEN not in json.dumps(allh) + json.dumps(ev)
        assert c.get(f"/v1/workspaces/{WS}/connections/c_nope/health", headers=h).status_code == 404
