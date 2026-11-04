# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario B (reusable analytical workflow) and the compiler's guarantees: only verified work compiles,
compiled runs give the same verified numbers, stale dependencies are never reused, and permissions and
workspace isolation hold through the whole lifecycle."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.config import LLMSettings
from lyst.llm import LLMClient
from lyst.security import AccessDenied
from lyst.semantic import MetricSpec
from lyst.workflows import WorkflowStale, pattern

from .conftest import ask


def _numbers(r):
    """What must not change between a planned and a compiled run: the answer, evidence, tables and checks."""
    return (r["answer"], r["evidence"], json.dumps(r["tables"], sort_keys=True), json.dumps(r["charts"], sort_keys=True),
            [(c["name"], c["passed"]) for c in r["confidence"]["checks"]])


def test_pattern_lifts_the_period_out():
    assert pattern("Why did profitability fall in Q1 2026?") == pattern("why did profitability fall in q3 2026") \
        == pattern("Why did profitability fall last quarter?") == "why did profitability fall {quarter}"
    assert pattern("What was revenue in Aug 2026 by region?") == "what was revenue {month} by region"
    # the slot is typed: a quarterly workflow is never reused for a year or a month
    assert pattern("What was revenue in 2025?") == pattern("What was revenue last year?") == "what was revenue {year}"
    assert pattern("What was revenue in 2025?") != pattern("What was revenue in Q2 2026?")
    assert pattern("Revenue by region") != pattern("Revenue by product line")


def test_verified_analysis_compiles_and_a_repeat_reuses_it_with_identical_results(seeded, monkeypatch):
    lyst, owner = seeded
    first = ask(lyst, owner, "Why did operating margin decline in Q2 2026?")["result"]
    compiled = first["supporting"]["workflow"]["compiled"]
    assert first["supporting"]["workflow"]["used"] is None and compiled["created"] and compiled["version"] == 1
    wf = lyst.services.workflows.get(owner, "northwind", compiled["id"])
    d = wf["definition"]
    assert d["inputs"] == {"period": {"type": "period", "grain": "quarter",
                                      "default": "the last complete quarter in the data"}}
    assert {"$period": "current"} in [s["args"].get("period") for s in d["steps"]]
    assert "Q2 2026" not in json.dumps(d["steps"])  # the period is a parameter, not baked in
    assert wf["deps"]["metrics"] == {"operating_margin": 1} and "pnl" in json.dumps(wf["deps"]["datasets"])
    assert "analyst.ask" in wf["permissions"] and "recompute" in d["validation"]["required_checks"]

    again = ask(lyst, owner, "Why did operating margin decline in Q3 2026?")
    r = again["result"]
    assert r["supporting"]["workflow"]["used"]["id"] == compiled["id"]
    assert r["supporting"]["workflow"]["strategy"] == "compiled"
    assert r["confidence"]["level"] == "verified" and "Q3 2026" in r["answer"]
    # the same question planned from scratch gives exactly the same verified result
    monkeypatch.setenv("LYST_WORKFLOWS", "0")
    planned = ask(lyst, owner, "Why did operating margin decline in Q3 2026?")["result"]
    assert planned["supporting"]["workflow"]["used"] is None
    assert _numbers(planned) == _numbers(r)
    wf = lyst.services.workflows.get(owner, "northwind", compiled["id"])
    assert wf["runs"] == 1 and wf["verified_runs"] == 2 and wf["failed_verifications"] == 0


def test_a_repeat_needs_no_model_to_read_the_request(seeded):
    lyst, owner = seeded
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append("classify" if body.get("response_format") else "narrative")
        text = json.dumps({"metrics": ["operating_margin"], "intent": "explain"}) if body.get("response_format") else "ok"
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    lyst.services.llm = LLMClient(LLMSettings("http://localhost:11434/v1", "local"), lyst.stores,
                                  transport=httpx.MockTransport(handler), credentials=lyst.credentials)
    first = ask(lyst, owner, "Why did profitability fall in Q2 2026?")["result"]
    assert calls.count("classify") == 1 and first["supporting"]["workflow"]["compiled"]
    calls.clear()
    r = ask(lyst, owner, "Why did profitability fall in Q3 2026?")["result"]
    assert "classify" not in calls  # the compiled understanding replaced the model's reading
    assert r["confidence"]["level"] == "verified" and r["supporting"]["workflow"]["used"]


def test_unverified_work_is_never_compiled(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was revenue in Q1 2019?")["result"]  # before the data starts: coverage fails
    assert r["confidence"]["level"] != "verified"
    assert r["supporting"]["workflow"]["compiled"] is None
    assert lyst.services.workflows.list(owner, "northwind") == []


def test_a_changed_definition_makes_the_workflow_stale_and_the_next_verified_run_recompiles(seeded):
    lyst, owner = seeded
    wf_id = ask(lyst, owner, "What was revenue in Q2 2026 by region?")["result"]["supporting"]["workflow"]["compiled"]["id"]
    spec = lyst.semantic.get(owner, "northwind", "revenue")
    lyst.semantic.define(owner, "northwind", MetricSpec.model_validate(spec.model_dump() | {"description": "Net revenue."}))
    with pytest.raises(WorkflowStale, match="revenue is now v2"):
        lyst.services.workflows.for_run(owner, "northwind", {"id": wf_id})
    r = ask(lyst, owner, "What was revenue in Q3 2026 by region?")["result"]
    assert r["supporting"]["workflow"]["used"] is None  # planned afresh, not run on the old definition
    assert r["supporting"]["workflow"]["compiled"] == {"id": wf_id, "version": 2, "created": True}
    hist = {h["version"]: h["status"] for h in lyst.services.workflows.get(owner, "northwind", wf_id)["history"]}
    assert hist == {2: "active", 1: "superseded"}


def test_run_a_workflow_directly_over_http_with_permissions_and_isolation(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    mallory = lyst.guard.create_user("mallory@else.test", "Mallory")
    lyst.guard.create_workspace(mallory, "elsewhere", "Elsewhere")
    wf_id = ask(lyst, owner, "Why did operating margin decline in Q2 2026?")["result"]["supporting"]["workflow"]["compiled"]["id"]
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(viewer)}"}
    hm = {"Authorization": f"Bearer {lyst.guard.issue_token(mallory)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        listed = c.get("/v1/workspaces/northwind/workflows", headers=h).json()
        assert [w["id"] for w in listed] == [wf_id]
        assert c.get("/v1/workspaces/northwind/workflows", headers=hm).status_code == 403
        assert c.post(f"/v1/workspaces/northwind/workflows/{wf_id}/run", json={}, headers=hm).status_code == 403
        assert c.post(f"/v1/workspaces/northwind/workflows/{wf_id}/run", json={"period": "drop table x;"},
                      headers=h).status_code == 400
        r = c.post(f"/v1/workspaces/northwind/workflows/{wf_id}/run", json={"period": "Q4 2025"}, headers=h)
        assert r.status_code == 202
        lyst.runtime.drain()
        t = lyst.runtime.get(viewer, r.json()["task_id"])
        assert t["kind"] == "workflow" and t["status"] == "done"
        assert "Q4 2025" in t["result"]["answer"] and t["result"]["supporting"]["workflow"]["used"]["id"] == wf_id
        assert t["result"]["confidence"]["level"] == "verified"
        # a viewer may run it but not retire it; an editor or above can
        assert c.post(f"/v1/workspaces/northwind/workflows/{wf_id}/retire", headers=h).status_code == 403
    lyst.services.workflows.retire(owner, "northwind", wf_id)
    with pytest.raises(WorkflowStale, match="retired"):
        lyst.services.workflows.for_run(owner, "northwind", {"id": wf_id})
    with pytest.raises(AccessDenied):
        lyst.services.workflows.get(mallory, "northwind", wf_id)


def test_a_quarterly_workflow_is_not_reused_for_a_year_and_a_direct_run_checks_the_grain(seeded):
    lyst, owner = seeded
    wf_id = ask(lyst, owner, "What was revenue in Q2 2026?")["result"]["supporting"]["workflow"]["compiled"]["id"]
    year = ask(lyst, owner, "What was revenue in 2025?")
    assert year["status"] == "done" and year["result"]["supporting"]["workflow"]["used"] is None
    assert year["result"]["confidence"]["level"] == "verified"
    tid = lyst.services.workflows.run(owner, "northwind", wf_id, lyst.runtime, period="2025")
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "failed" and "takes a quarter" in t["error"]


def test_a_newly_registered_skill_wins_over_an_older_workflow(seeded):
    """Reuse must not hide a better capability that was registered after the workflow compiled
    (the full proposal-to-registration path is test_skills.test_f_...)."""
    lyst, owner = seeded
    ask(lyst, owner, "What was revenue in Q2 2026 by region?")
    lyst.services.skills.match = lambda *a, **k: {"name": "revenue_by_region_skill"}
    from lyst.analyst.engine import _compiled_understanding
    from lyst.analyst.registry import Context

    ctx = Context("northwind", owner, None, lyst.services)
    assert _compiled_understanding(ctx, "What was revenue in Q3 2026 by region?") is None


def test_compilation_can_be_turned_off(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_WORKFLOWS", "0")
    r = ask(lyst, owner, "Why did operating margin decline in Q2 2026?")["result"]
    assert r["supporting"]["workflow"] == {"used": None, "strategy": "planned", "compiled": None}
