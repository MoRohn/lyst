# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Edge cases from docs/GAPS.md: rotating a connection's secret in place, a revoked credential behind a
connection, personal model keys, row-level security on every SQL path, retiring a dashboards, clean CLI
behaviour in setup mode, and the audit chain verified at startup."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.connectors import CONNECTORS, Connector, ConnectorError, Field, Manifest
from lyst.data.sqlguard import QueryRejected
from lyst.security import AccessDenied

from .conftest import ask


class Fake(Connector):
    manifest = Manifest(kind="fake", label="Fake", fields=(Field("url", "URL"), Field("token", "Token", secret=True)), capabilities=("full",))
    accepted = {"tok-first-0001"}

    def test(self):
        if self.secrets.get("token") not in Fake.accepted:
            raise ConnectorError("401 from the API")
        return {"ok": True}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setitem(CONNECTORS, "fake", Fake)
    Fake.accepted = {"tok-first-0001"}
    return Fake


def test_a_connection_secret_rotates_in_place_through_settings(seeded, fake):
    lyst, owner = seeded
    conn = lyst.connections.create(owner, "northwind", kind="fake", name="Fake API", values={"url": "https://x", "token": "tok-first-0001"})
    cred = conn["credential_id"]
    # the new value is tested through the connector before it replaces anything
    with pytest.raises(ValueError, match="did not pass"):
        lyst.credentials.replace(owner, cred, "tok-wrong-00001")
    assert lyst.connections._load(owner, "northwind", conn["id"])[1].secrets["token"] == "tok-first-0001"
    fake.accepted = {"tok-second-0002"}
    out = lyst.credentials.replace(owner, cred, "tok-second-0002")
    assert out["version"] == 2 and out["test"]["outcome"] == "connection tested"
    assert lyst.connections._load(owner, "northwind", conn["id"])[1].secrets == {"token": "tok-second-0002"}  # payload shape kept
    assert lyst.stores.control.scalar("SELECT status FROM connections WHERE id = ?", [conn["id"]]) == "ok"
    # Settings lists it under the workspace with its connection label, value never included
    rows = lyst.credentials.list(owner, scope_kind="workspace", scope_id="northwind")
    assert rows[0]["provider"] == "connector:fake" and "tok-" not in json.dumps(rows, default=str)


def test_a_revoked_credential_marks_the_connection_in_error_not_a_crash(seeded, fake):
    lyst, owner = seeded
    conn = lyst.connections.create(owner, "northwind", kind="fake", name="Fake API", values={"url": "https://x", "token": "tok-first-0001"})
    lyst.credentials.revoke(owner, conn["credential_id"])
    with pytest.raises(ConnectorError, match="credential unavailable"):
        lyst.connections.test(owner, "northwind", conn["id"])
    row = lyst.connections.list(owner, "northwind")[0]
    assert row["status"] == "error" and "revoked" in row["last_error"]
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post(f"/v1/workspaces/northwind/connections/{conn['id']}/test", headers=h)
        assert r.status_code == 400 and "credential unavailable" in r.json()["detail"]


def test_personal_keys_apply_only_to_their_owners_tasks(seeded):
    lyst, owner = seeded
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})

    from lyst.llm import LLMClient

    client = LLMClient(lyst.settings.llm, lyst.stores, transport=httpx.MockTransport(handler), credentials=lyst.credentials)
    lyst.services.llm = lyst.llm = client
    lyst.credentials.set_policy(owner, "openrouter", user_credentials=True)
    other = lyst.guard.create_user("pat@northwind.test", "Pat")
    lyst.guard.add_member(owner, "northwind", other.user_id, "editor")
    lyst.credentials.connect(other, scope_kind="user", scope_id=other.user_id, provider="openrouter", name="mine",
                             secret="or-test-personal-key-0001", test_first=False)
    lyst.stores.control.execute("UPDATE workspaces SET allow_external_llm = true WHERE id = 'northwind'")
    # Pat's task uses Pat's key; the owner has no key anywhere, so their task runs on rules
    t_pat = lyst.runtime.submit(other, "northwind", "Why did operating margin decline last quarter?")
    t_own = lyst.runtime.submit(owner, "northwind", "Why did operating margin decline last quarter?")
    lyst.runtime.drain()
    assert lyst.runtime.get(other, t_pat)["status"] == "done" and lyst.runtime.get(owner, t_own)["status"] == "done"
    assert sent and set(sent) == {"Bearer or-test-personal-key-0001"}
    assert lyst.stores.control.scalar("SELECT count(*) FROM llm_calls WHERE task_id = ?", [t_own]) == 0


def test_row_policies_hold_on_every_sql_path(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@northwind.test", "Vic")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.data.set_row_policy(viewer, "northwind", "pnl", "region", principal_kind="user", principal=viewer.user_id, allowed_values=["EMEA"])
    lyst.data.set_row_policy(owner, "northwind", "pnl", "region", principal_kind="role", principal="viewer", allowed_values=["EMEA", "APAC"])
    lyst.data.set_row_policy(owner, "northwind", "pnl", "region", principal_kind="user", principal=viewer.user_id, allowed_values=["EMEA"])
    # a user policy wins over the role policy; every shape of SQL is filtered, including aliases, joins and CTEs
    for sql in ("SELECT DISTINCT region FROM pnl", "SELECT DISTINCT p.region FROM pnl p",
                "WITH x AS (SELECT * FROM pnl) SELECT DISTINCT region FROM x",
                "SELECT DISTINCT a.region FROM pnl a JOIN pnl b ON a.month = b.month"):
        assert [r[0] for r in lyst.data.query(viewer, "northwind", sql).rows] == ["EMEA"], sql
    assert len({r[0] for r in lyst.data.query(owner, "northwind", "SELECT DISTINCT region FROM pnl").rows}) == 4
    # the analyst's answer for the viewer is the EMEA number, the owner's is the whole company
    full = ask(lyst, owner, "What was revenue last quarter?")["result"]
    part = ask(lyst, viewer, "What was revenue last quarter?")["result"]
    assert full["status"] == "complete" and part["status"] == "complete" and part["answer"] != full["answer"]
    rows_full = lyst.data.query(owner, "northwind", "SELECT sum(revenue) FROM pnl WHERE month >= '2026-07-01' AND month < '2026-10-01'").rows[0][0]
    rows_part = lyst.data.query(viewer, "northwind", "SELECT sum(revenue) FROM pnl WHERE month >= '2026-07-01' AND month < '2026-10-01'").rows[0][0]
    assert rows_part < rows_full
    # a policy with no allowed values means no rows, and a column filter cannot be escaped with functions
    lyst.data.set_row_policy(owner, "northwind", "pnl", "region", principal_kind="user", principal=viewer.user_id, allowed_values=[])
    assert lyst.data.query(viewer, "northwind", "SELECT count(*) FROM pnl").rows[0][0] == 0
    assert lyst.data.query(viewer, "northwind", "SELECT count(*) FROM pnl WHERE upper(region) = 'EMEA' OR 1 = 1").rows[0][0] == 0
    with pytest.raises(QueryRejected):
        lyst.data.query(viewer, "northwind", "SELECT * FROM data.pnl_v1")
    # listing and removal are owner-only and audited
    assert len(lyst.data.row_policies(owner, "northwind", "pnl")) == 2
    assert lyst.data.delete_row_policy(owner, "northwind", "pnl", "region", principal_kind="user", principal=viewer.user_id)
    assert {r[0] for r in lyst.data.query(viewer, "northwind", "SELECT DISTINCT region FROM pnl").rows} == {"EMEA", "APAC"}
    audit = lyst.stores.control.query("SELECT action FROM audit_log WHERE action LIKE 'policy.rows%'")
    assert len(audit) == 4
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post("/v1/workspaces/northwind/datasets/pnl/row-policies", headers=h,
                   json={"column": "region", "principal_kind": "role", "principal": "editor", "allowed_values": ["LATAM"]})
        assert r.status_code == 200 and len(c.get("/v1/workspaces/northwind/datasets/pnl/row-policies", headers=h).json()) == 2
        assert c.post("/v1/workspaces/northwind/datasets/nope/row-policies", headers=h,
                      json={"column": "region", "principal_kind": "role", "principal": "editor", "allowed_values": []}).status_code == 404


def test_a_dashboard_can_be_retired(seeded):
    lyst, owner = seeded
    ask(lyst, owner, "Build a revenue dashboard by region in Streamlit")
    d = lyst.studio.list(owner, "northwind")[0]
    sched = lyst.runtime.schedules(owner, "northwind")
    out = lyst.studio.retire(owner, "northwind", d["id"])
    assert out["status"] == "retired" and lyst.studio.list(owner, "northwind") == []
    assert lyst.studio.list(owner, "northwind", include_retired=True)[0]["status"] == "retired"
    if sched:
        assert lyst.runtime.schedules(owner, "northwind")[0]["enabled"] is False
    with pytest.raises(ValueError, match="retired"):
        lyst.studio.refresh(owner, "northwind", d["id"])
    assert lyst.studio.bundle(owner, "northwind", d["id"])  # the bundle is still there
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.studio.retire(viewer, "northwind", d["id"])


def test_cli_in_setup_mode_says_what_to_do(tmp_path, monkeypatch, capsys):
    from lyst import cli

    monkeypatch.delenv("LYST_MASTER_KEY", raising=False)
    monkeypatch.setenv("LYST_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["prune"])
    assert "lyst init" in str(exc.value) and "Traceback" not in capsys.readouterr().err


def test_audit_chain_is_verified_at_startup(tmp_path, monkeypatch):
    from lyst import observability as obs
    from lyst.app import Lyst
    from lyst.config import load_settings

    monkeypatch.setenv("LYST_MASTER_KEY", "55" * 32)
    app = Lyst.build(load_settings(tmp_path / "h"))
    owner = app.guard.create_user("o@x.test", "O")
    app.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="custom", name="t", secret="value-value-1")
    assert obs.AUDIT_CHAIN._value.get() == 1
    app.stores.control.execute("UPDATE credential_events SET outcome = 'denied' WHERE action = 'credential.connect'")
    app.close()
    app = Lyst.build(load_settings(tmp_path / "h"))
    try:
        assert obs.AUDIT_CHAIN._value.get() == 0
    finally:
        app.close()


def test_a_truncated_reasoning_answer_is_retried_with_a_larger_budget(seeded):
    """A reasoning model can spend the whole token budget before the JSON: the gateway retries once with more room."""
    from lyst.llm import LLMClient

    lyst, owner = seeded
    budgets = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        budgets.append(body["max_tokens"])
        if body["max_tokens"] < 1000:
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"question": "What moved', "reasoning": "..."},
                                                          "finish_reason": "length"}], "usage": {"prompt_tokens": 50, "completion_tokens": 400}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"question": "What moved margin?", "subtitle": "Monthly"}'},
                                                      "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 30}})

    client = LLMClient(lyst.settings.llm, lyst.stores, transport=httpx.MockTransport(handler), credentials=lyst.credentials)
    lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="openrouter", name="OpenRouter", secret="or-test-key-000001",
                             test_first=False)
    lyst.stores.control.execute("UPDATE workspaces SET allow_external_llm = true WHERE id = 'northwind'")
    out = client.chat_json("northwind", [{"role": "user", "content": "name it"}], purpose="studio_design", workload="structured", max_tokens=400)
    assert out == {"question": "What moved margin?", "subtitle": "Monthly"} and budgets == [400, 1200]
    outcomes = [r["outcome"] for r in lyst.stores.control.query("SELECT outcome FROM llm_calls ORDER BY id")]
    assert outcomes[-2:] == ["truncated", "ok"]


def test_number_verification_checks_magnitude_not_sign_and_still_rejects_invented_numbers():
    from lyst.analyst.engine import unbacked_numbers

    allowed = [0.325, 32.5, -0.035, -3.5, 1.07, 107.0]
    assert unbacked_numbers("Operating margin fell 3.5 points to 32.5%; EMEA explains 107% of the move.", allowed) == []
    assert unbacked_numbers("Margin fell 4.5 points to 32.5%.", allowed) == [0.045]  # "points" scale like pts since N1
    assert unbacked_numbers("a change of -0.034 against a year ago", [-0.03435]) == []  # the model's rounding of a tool value
    assert unbacked_numbers("a change of -0.03 against a year ago", [-0.03435]) == []
    assert unbacked_numbers("a change of -0.036 against a year ago", [-0.03435]) == [-0.036]
    assert unbacked_numbers("Revenue was $25.4M in Q3 2026.", allowed) == [25.4e6]


def test_model_summary_may_reuse_any_number_lyst_wrote(seeded):
    """The executive summary is kept when its numbers are Lyst's own, in any of the forms Lyst wrote them."""
    from lyst.llm import LLMClient

    lyst, owner = seeded
    text = ("Operating margin fell 3.5 points to 32.5% in Q3 2026 and sits -0.034 below a year earlier. "
            "EMEA's own rate explains 107% of the move, falling from 36.8% to 24.0%.")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = "{}" if body.get("response_format") else text
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}})

    client = LLMClient(lyst.settings.llm, lyst.stores, transport=httpx.MockTransport(handler), credentials=lyst.credentials)
    lyst.services.llm = lyst.llm = client
    lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="openrouter", name="OpenRouter", secret="or-test-key-000001",
                             test_first=False)
    lyst.stores.control.execute("UPDATE workspaces SET allow_external_llm = true WHERE id = 'northwind'")
    r = ask(lyst, owner, "Why did operating margin decline last quarter?")["result"]
    assert r["supporting"]["narrative_by"] == "model (numbers verified)", r["supporting"]["narrative_by"]
    assert r["answer"] == text


def test_an_empty_model_reply_never_replaces_the_computed_answer(seeded):
    from lyst.llm import LLMClient

    lyst, owner = seeded

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = "{}" if body.get("response_format") else ""
        return httpx.Response(200, json={"choices": [{"message": {"content": content, "reasoning": "thinking..."}, "finish_reason": "stop"}],
                                         "usage": {}})

    client = LLMClient(lyst.settings.llm, lyst.stores, transport=httpx.MockTransport(handler), credentials=lyst.credentials)
    lyst.services.llm = lyst.llm = client
    lyst.credentials.connect(owner, scope_kind="installation", scope_id=None, provider="openrouter", name="OpenRouter", secret="or-test-key-000001",
                             test_first=False)
    lyst.stores.control.execute("UPDATE workspaces SET allow_external_llm = true WHERE id = 'northwind'")
    r = ask(lyst, owner, "Why did operating margin decline last quarter?")["result"]
    assert r["answer"].startswith("Operating margin was 32.5%") and r["supporting"]["narrative_by"] == "template (model returned no wording)"
