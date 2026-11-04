# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Classification and policy-aware execution (scenario D): crossing a restricted boundary is refused or narrowed
in the services, with an explanation members can act on and administrators can diagnose."""

from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.governance import PolicyDenied
from lyst.llm import LLMUnavailable
from lyst.security import AccessDenied
from lyst.semantic import Measure, MetricSpec
from tests.conftest import ask

WS = "northwind"


def _csv(df):
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode()


def _payroll(lyst, owner):
    months = pd.date_range("2025-01-01", periods=18, freq="MS")
    df = pd.DataFrame([{"month": m.date(), "department": d, "salary": 100000.0 + 1000 * i}
                       for i, m in enumerate(months) for d in ("Plant", "Office")])
    lyst.data.ingest(owner, WS, "salaries", _csv(df), filename="salaries.csv", title="Salaries by department")
    lyst.governance.classify(owner, WS, "salaries", "restricted", tags=["personal", "financial"], reason="Compensation is restricted to HR")
    lyst.semantic.define(owner, WS, MetricSpec(name="payroll", title="Payroll", dataset="salaries", measure=Measure(agg="sum", column="salary"),
                                               time_column="month", dimensions=["department"], format="currency"))


def _viewer(lyst, owner, role="viewer"):
    v = lyst.guard.create_user(f"{role}@northwind.test", role.title())
    lyst.guard.add_member(owner, WS, v.user_id, role)
    return v


def test_classification_is_an_administrator_decision_with_a_reason_and_history(seeded):
    lyst, owner = seeded
    gov = lyst.governance
    with pytest.raises(ValueError):
        gov.classify(owner, WS, "pnl", "secret", reason="no such level")
    with pytest.raises(ValueError):
        gov.classify(owner, WS, "pnl", "restricted", reason="")
    with pytest.raises(ValueError):
        gov.classify(owner, WS, "pnl", "restricted", tags=["spicy"], reason="tag check")
    editor = _viewer(lyst, owner, "editor")
    with pytest.raises(AccessDenied):
        gov.classify(editor, WS, "pnl", "public", reason="make it public")
    gov.classify(owner, WS, "pnl", "restricted", tags=["financial"], reason="Board numbers before release")
    gov.classify(owner, WS, "pnl", "confidential", tags=["financial"], reason="Released")
    hist = gov.classification_history(owner, WS, "pnl")
    assert [(h["previous"], h["level"]) for h in hist] == [("restricted", "confidential"), ("confidential", "restricted")]
    assert {c["dataset"]: c["tags"] for c in gov.classifications(owner, WS)}["pnl"] == ["financial"]


def test_policy_must_stay_monotonic_and_is_versioned(seeded):
    lyst, owner = seeded
    gov = lyst.governance
    assert gov.policy(owner, WS)["version"] == 0
    with pytest.raises(ValueError):
        gov.set_policy(owner, WS, {"read_min_role": {"confidential": "admin", "restricted": "viewer"}})
    with pytest.raises(ValueError):
        gov.set_policy(owner, WS, {"export_max": "everything"})
    p = gov.set_policy(owner, WS, {"read_min_role": {"confidential": "editor"}, "export_max": "internal"})
    assert p["version"] == 1 and p["spec"]["read_min_role"]["restricted"] == "admin"


def test_scenario_d_restricted_data_is_refused_with_a_clear_explanation(seeded):
    lyst, owner = seeded
    _payroll(lyst, owner)
    viewer = _viewer(lyst, owner)
    with pytest.raises(PolicyDenied) as e:
        lyst.data.query(viewer, WS, "SELECT department, sum(salary) FROM salaries GROUP BY 1")
    assert "salaries is restricted data" in str(e.value) and "Avery" in str(e.value)
    assert "read_min_role" not in str(e.value)  # policy internals are not shown to the member
    hist = lyst.stores.workspace(WS).query("SELECT outcome, datasets FROM query_history ORDER BY id DESC LIMIT 1")[0]
    assert hist["outcome"] == "rejected" and "salaries" in hist["datasets"]
    audit = lyst.stores.control.query("SELECT outcome, detail FROM audit_log WHERE action = 'policy.read' ORDER BY id DESC LIMIT 1")[0]
    assert audit["outcome"] == "denied" and "\"needs\": \"admin\"" in audit["detail"]

    t = ask(lyst, viewer, "What was payroll last quarter?")
    assert t["status"] == "failed" and "restricted data" in (t["error"] or "")
    assert lyst.data.query(viewer, WS, "SELECT count(*) FROM pnl").rows  # other data is unaffected

    member_view = lyst.governance.evaluate(viewer, WS, metrics=["payroll"])
    assert member_view["outcome"] == "refused" and "rule" not in member_view["reasons"][0] and "levels" not in member_view
    admin_view = lyst.governance.evaluate(owner, WS, metrics=["payroll"], purpose="export")
    assert admin_view["outcome"] == "refused" and admin_view["levels"] == {"salaries": "restricted"}
    assert "export_max" in admin_view["reasons"][0]["rule"]


def test_restricted_results_never_reach_an_external_model_or_a_file(seeded):
    lyst, owner = seeded
    _payroll(lyst, owner)
    t = ask(lyst, owner, "What was payroll last quarter?")
    assert t["status"] == "done"
    lyst.governance.external_model = lambda: True  # as with OpenRouter connected
    reason = lyst.governance.model_gate(WS, t["id"])
    assert reason and "restricted data (salaries)" in reason
    with pytest.raises(LLMUnavailable, match="policy"):
        lyst.llm.chat(WS, [{"role": "user", "content": "word this"}], purpose="deliver", task_id=t["id"])
    assert any(e["kind"] == "policy" for e in lyst.runtime.get(owner, t["id"])["events"])
    narrowed = lyst.governance.evaluate(owner, WS, metrics=["payroll"])
    assert narrowed["outcome"] == "narrowed"
    with pytest.raises(PolicyDenied, match="exported files"):
        lyst.artifacts.build(owner, lyst.runtime.get(owner, t["id"]), "md")
    ok = ask(lyst, owner, "What was revenue in 2024?")  # confidential data stays within both defaults
    assert lyst.governance.model_gate(WS, ok["id"]) is None
    assert lyst.artifacts.build(owner, ok, "md")["id"]


def test_column_and_row_policies_show_as_narrowed(seeded):
    lyst, owner = seeded
    viewer = _viewer(lyst, owner)
    lyst.data.set_column_policy(owner, WS, "pnl", "opex", "financial", "admin")
    lyst.data.set_row_policy(owner, WS, "pnl", "region", principal_kind="role", principal="viewer", allowed_values=["North"])
    r = lyst.governance.evaluate(viewer, WS, datasets=["pnl"])
    assert r["outcome"] == "narrowed" and len(r["reasons"]) == 2
    assert all("columns" not in x for x in r["reasons"])  # which columns is for administrators


def test_suggestions_and_api(lyst):
    owner = lyst.guard.create_user("hr@acme.test", "HR")
    lyst.guard.create_workspace(owner, "acme", "Acme")
    df = pd.DataFrame({"employee_id": [f"E{i}" for i in range(30)], "email": [f"p{i}@acme.test" for i in range(30)],
                       "salary": [50000.0 + i for i in range(30)]})
    lyst.data.ingest(owner, "acme", "staff", _csv(df), filename="staff.csv")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        sug = c.get("/v1/workspaces/acme/classifications/suggestions", headers=h).json()
        assert sug[0]["dataset"] == "staff" and sug[0]["level"] == "confidential" and "personal" in sug[0]["tags"]
        r = c.put("/v1/workspaces/acme/datasets/staff/classification", json={"level": "restricted", "tags": ["personal"], "reason": "HR data"}, headers=h)
        assert r.status_code == 200, r.text
        assert c.get("/v1/workspaces/acme/datasets/staff/classification", headers=h).json()[0]["level"] == "restricted"
        assert c.put("/v1/workspaces/acme/data-policy", json={"export_max": "internal"}, headers=h).json()["version"] == 1
        chk = c.post("/v1/workspaces/acme/policy/check", json={"datasets": ["staff"], "purpose": "export"}, headers=h).json()
        assert chk["outcome"] == "refused"
