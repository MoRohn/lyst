# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Continuous evaluation: baselines, regression detection (scenario H: a controlled analytical defect is caught before
release), quality dimensions, and model routing from measured outcomes (scenario B)."""

import json
from pathlib import Path

import httpx
import pytest

from lyst import demo, evals
from lyst.llm import policy, scorecard

BASELINE = Path(__file__).resolve().parents[1] / "docs" / "quality" / "eval-baseline.json"


def _bank(ids):
    return [c for c in evals.demo_bank() if c.id in ids]


def test_checked_in_baseline_covers_the_whole_bank():
    base = json.loads(BASELINE.read_text())
    assert set(base["cases"]) == {c.id for c in evals.demo_bank()}
    assert all(c["passed"] for c in base["cases"].values())


def test_scenario_h_a_planted_analytical_defect_is_caught_as_a_regression(seeded, monkeypatch):
    lyst, owner = seeded
    cases = _bank({"revenue-last-quarter", "margin-why", "what-if-margin"})
    good = evals.run(lyst, owner, "northwind", cases, label="before")
    assert good["passed"] == 3
    base = evals.baseline_of(good)
    assert evals.compare(good, base)["ok"]
    # the defect: metric SQL silently scales every value by 0.97 (as a units or filter bug would)
    from lyst.analyst import engine
    from lyst.semantic import compile_metric as real

    def broken(spec, **kw):
        sql = real(spec, **kw)
        return sql.replace("SELECT SUM(", "SELECT 0.97 * SUM(", 1)

    monkeypatch.setattr(engine, "compile_metric", broken)
    bad = evals.run(lyst, owner, "northwind", cases, label="after")
    cmp_ = evals.compare(bad, base)
    assert not cmp_["ok"]
    hit = {(r["case"], r["check"]) for r in cmp_["regressions"] if r["kind"] == "accuracy"}
    assert ("revenue-last-quarter", "value") in hit


def test_compare_flags_latency_resources_and_missing_cases_but_not_noise():
    def summary(**over):
        row = {"case": "c1", "passed": True, "seconds": 0.2, "steps": 8, "queries": 6, "model_calls": 0, "tokens": 0,
               "checks": [{"name": "value", "passed": True, "detail": ""}, {"name": "latency", "passed": True, "detail": ""}]}
        row.update(over)
        return {"results": [row]}

    base = evals.baseline_of(summary())
    assert evals.compare(summary(seconds=0.9), base)["ok"]  # under baseline + 1 s floor: machine noise
    r = evals.compare(summary(seconds=2.5), base)["regressions"]
    assert [x["kind"] for x in r] == ["latency"]
    r = evals.compare(summary(queries=20, model_calls=5), base)["regressions"]
    assert {x["check"] for x in r} == {"queries", "model_calls"}
    assert evals.compare({"results": []}, base)["missing"] == ["c1"]
    r = evals.compare(summary(checks=[{"name": "value", "passed": False, "detail": "expected 1; got 2"}]), base)
    assert r["regressions"][0]["kind"] == "accuracy"


def test_runs_report_quality_dimensions_categories_and_resources(seeded):
    lyst, owner = seeded
    s = evals.run(lyst, owner, "northwind", _bank({"ambiguous", "revenue-last-quarter", "what-if-margin"}))
    assert set(s["categories"]) == {"adversarial", "financial", "scenario"}
    assert {"completion", "correctness", "efficiency"} <= set(s["dimensions"])
    r = {x["case"]: x for x in s["results"]}
    assert r["revenue-last-quarter"]["queries"] >= 3 and r["revenue-last-quarter"]["steps"] >= 6


# ----------------------------------------------------------------------------- measured routing (scenario B)


def _calls(db, model, workload, ok, failed):
    for i in range(ok + failed):
        db.execute("INSERT INTO llm_calls (id, workspace_id, model, workload, outcome, latency_ms, input_tokens, output_tokens, "
                   "cost_usd) VALUES (nextval('llm_call_seq'), 'northwind', ?, ?, ?, ?, 100, 20, 0.0001)",
                   [model, workload, "ok" if i < ok else "upstream_error", 400 + i])


def test_scorecard_and_measured_order(lyst):
    db = lyst.stores.control
    _calls(db, "a/flaky", "classify", 3, 9)
    _calls(db, "b/steady", "classify", 12, 0)
    _calls(db, "a/flaky", "chat", 10, 0)  # fine on another workload
    card = {(r["model"], r["workload"]): r for r in scorecard.scorecard(db)}
    assert card[("a/flaky", "classify")]["success_rate"] == 0.25 and card[("b/steady", "classify")]["success_rate"] == 1.0
    order, bad = scorecard.measured_order(db, "classify", ["a/flaky", "b/steady"])
    assert order == ["b/steady", "a/flaky"] and bad["a/flaky"]["calls"] == 12
    assert scorecard.measured_order(db, "chat", ["a/flaky", "b/steady"])[0] == ["a/flaky", "b/steady"]
    # too few calls is not evidence
    _calls(db, "c/new", "extract", 0, 3)
    assert scorecard.measured_order(db, "extract", ["c/new", "b/steady"])[0] == ["c/new", "b/steady"]


def test_routing_skips_a_failing_model_first_and_never_reorders_a_pinned_one(tmp_path, monkeypatch):
    from lyst.app import Lyst
    from lyst.config import load_settings
    from lyst.llm import LLMClient

    monkeypatch.setenv("LYST_MASTER_KEY", "cd" * 32)
    monkeypatch.setenv("LYST_OPENROUTER_API_KEY", "sk-or-test")
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    app = Lyst.build(load_settings(tmp_path / "h"))
    app.services.llm = llm = LLMClient(app.settings.llm, app.stores, transport=httpx.MockTransport(handler), credentials=app.credentials)
    try:
        owner = app.guard.create_user("o@x.test", "O")
        demo.seed(app, owner)
        app.stores.control.execute("UPDATE workspaces SET llm_tier = 'balanced' WHERE id = 'northwind'")
        tier = [m["modelId"] for m in policy.tier_models("balanced")]
        if len(tier) < 2:
            pytest.skip("the bundled balanced tier has one model")
        _calls(app.stores.control, tier[0], "chat", 2, 10)
        llm.chat("northwind", [{"role": "user", "content": "x" * 50}], purpose="test", max_tokens=4000)
        assert seen[-1] != tier[0]  # measured: the failing first choice moved behind a healthy one
        app.stores.control.execute("UPDATE workspaces SET llm_model = ? WHERE id = 'northwind'", [tier[0]])
        llm.chat("northwind", [{"role": "user", "content": "x" * 50}], purpose="test", max_tokens=4000)
        assert seen[-1] == tier[0]  # a pinned model is the person's choice and is not reordered
    finally:
        app.close()


def test_model_registry_shows_capabilities_and_measured_performance(seeded):
    from fastapi.testclient import TestClient

    from lyst.api import create_app

    lyst, owner = seeded
    _calls(lyst.stores.control, "x/local-model", "classify", 4, 1)
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.get("/v1/workspaces/northwind/model-registry", headers=h).json()
    row = next(m for m in r["models"] if m["model"] == "x/local-model")
    assert row["measured"][0]["workload"] == "classify" and row["measured"][0]["success_rate"] == 0.8
    assert r["policy"]["allow_external_llm"] is False
