# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Decision intelligence and scenario analysis: scenario A (strategic decision support) and scenario E (scenario
simulation: recalculation, consistent state, reproducible outputs) of the eighth iteration, plus the engine's rules."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst import decisions, scenarios
from lyst.api import create_app
from lyst.scenarios import Model, ModelError

from .conftest import ask

QUESTION = "What happens to operating margin if labor costs increase 6% while revenue grows 3%?"


# ----------------------------------------------------------------------------- the model language


def test_formulas_are_arithmetic_only_and_the_graph_is_checked():
    base = {"inputs": [{"name": "a", "value": 2}], "outputs": ["b"]}
    for bad in ("__import__('os').system('x')", "a.real", "[a]", "a if a else 1", "open('x')", "lambda: a"):
        with pytest.raises(ModelError):
            Model({**base, "formulas": [{"name": "b", "expr": bad}]})
    with pytest.raises(ModelError, match="undefined"):
        Model({**base, "formulas": [{"name": "b", "expr": "a * c"}]})
    with pytest.raises(ModelError, match="circular"):
        Model({**base, "formulas": [{"name": "b", "expr": "c + 1"}, {"name": "c", "expr": "b * a"}]})
    m = Model({**base, "formulas": [{"name": "b", "expr": "max(a, 3) ** 2 / (a - 2)"}]})
    vals, _ = m.evaluate({"a": 2.0})
    assert vals["b"] == float("inf") or vals["b"] != vals["b"]  # division by zero is visible, not an exception


def test_only_downstream_nodes_are_recomputed():
    m = Model({"inputs": [{"name": "x", "value": 1}, {"name": "y", "value": 2}],
               "formulas": [{"name": "fx", "expr": "x * 10"}, {"name": "fy", "expr": "y * 10"}, {"name": "t", "expr": "fx + fy"}],
               "outputs": ["t"]})
    base = {"x": 1.0, "y": 2.0}
    first, computed = m.evaluate(base)
    assert len(computed) == 5 and first["t"] == 30
    second, computed = m.evaluate(base, {"y": {"value": 3}}, previous=first, changed={"y"})
    assert computed == ["y", "fy", "t"] and second["t"] == 40 and second["fx"] == 10


def test_sensitivity_cases_and_simulation_are_consistent_and_reproducible():
    m = Model({"inputs": [{"name": "price", "value": 10, "low": 8, "high": 12, "distribution": "triangular"},
                          {"name": "units", "value": 100, "low": 90, "high": 130, "distribution": "uniform"},
                          {"name": "unit_cost", "value": 6, "low": 5.5, "high": 7, "distribution": "normal", "sd": 0.3}],
               "formulas": [{"name": "profit", "expr": "(price - unit_cost) * units"}], "outputs": ["profit"]})
    base = {"price": 10.0, "units": 100.0, "unit_cost": 6.0}
    sens = scenarios.sensitivity(m, base, "profit")
    assert sens[0]["input"] == "price"  # the widest swing ranks first
    c = scenarios.cases(m, base, "profit")
    assert c["worst"]["value"] == (8 - 7) * 90 and c["best"]["value"] == (12 - 5.5) * 130 and c["base"]["value"] == 400
    a = scenarios.simulate(m, base, n=20_000, seed=3, thresholds={"profit": 300})
    b = scenarios.simulate(m, base, n=20_000, seed=3, thresholds={"profit": 300})
    assert a == b  # same seed, same distribution
    s = a["outputs"]["profit"]
    assert c["worst"]["value"] < s["p5"] < s["p50"] < s["p95"] < c["best"]["value"]
    assert 0.05 < s["p_below"]["probability"] < 0.5


def test_what_if_sentences_are_parsed_and_unknown_drivers_are_not_guessed():
    from lyst.demo import PNL_DRIVERS

    m = Model(PNL_DRIVERS)
    p = scenarios.parse_what_if(m, QUESTION)
    assert p["overrides"] == {"labor_costs": {"change_pct": 6.0}, "revenue": {"change_pct": 3.0}}
    assert p["target"] == "operating_margin" and p["unmatched"] == []
    p = scenarios.parse_what_if(m, "What if marketing spend rises 10% and sales fall by 2 percent?")
    assert p["overrides"] == {"revenue": {"change_pct": -2.0}} and p["unmatched"] == ["marketing spend"]


# ----------------------------------------------------------------------------- scenario E


def test_scenario_e_what_if_from_home_is_correct_hypothetical_and_traceable(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, QUESTION)
    assert t["status"] == "done" and t["outcome"] == "complete"
    plan = [s["capability"] for s in t["steps"]]
    assert "what_if" in plan and "driver_analysis" not in plan
    r = t["result"]
    assert "hypothetical" in r["answer"] and "Operating margin would go from" in r["answer"]
    # independent recomputation from raw rows with pandas
    from lyst import demo

    df = demo.pnl()
    q = df[(pd.to_datetime(df.month) >= "2026-04-01") & (pd.to_datetime(df.month) < "2026-07-01")]
    rev, cogs, opex = q.revenue.sum(), q.cogs.sum(), q.opex.sum()
    rev2 = rev * 1.03
    labor2 = opex * 0.55 * 1.06
    oi2 = rev2 - rev2 * (cogs / rev) - labor2 - opex * 0.45
    sc = lyst.services.scenarios.ask(owner, "northwind", QUESTION)
    out = {o["name"]: o for o in sc["outputs"]}
    assert out["operating_margin"]["scenario"] == pytest.approx(oi2 / rev2, rel=1e-9)
    assert out["operating_margin"]["base"] == pytest.approx((rev - cogs - opex) / rev, rel=1e-9)
    assert sc["kind"] == "hypothetical" and set(sc["observed"]) == {"revenue", "cogs_ratio", "opex"}
    assert all(v["period"]["label"] == "Q2 2026" and v["dataset_version"] == 1 for v in sc["observed"].values())
    assert len(r["supporting"]["queries"]) == 3  # each observed input's query is evidence


def test_scenario_e_adjust_save_reproduce_and_compare(seeded):
    lyst, owner = seeded
    sc = lyst.services.scenarios
    first = sc.evaluate(owner, "northwind", "pnl_drivers", {"revenue": {"change_pct": 3}})
    # adjusting one assumption recomputes only what depends on it
    second = sc.evaluate(owner, "northwind", "pnl_drivers", {"revenue": {"change_pct": 3}, "labor_share": {"value": 0.6}},
                         previous=first)
    assert second["recomputed"] == ["labor_share", "labor_costs", "other_opex", "operating_income", "operating_margin"]
    full = sc.evaluate(owner, "northwind", "pnl_drivers", {"revenue": {"change_pct": 3}, "labor_share": {"value": 0.6}})
    assert second["values"] == full["values"]  # incremental and full recalculation agree
    s1 = sc.save(owner, "northwind", "pnl_drivers", "Growth plan", {"revenue": {"change_pct": 3}})
    s2 = sc.save(owner, "northwind", "pnl_drivers", "Growth plan", {"revenue": {"change_pct": 5}}, note="stretch")
    assert (s1["version"], s2["version"]) == (1, 2) and s1["digest"] != s2["digest"]
    rep = sc.reproduce(owner, "northwind", s1["id"])
    assert rep["same"] and rep["observed_changed"] == []
    cmp_ = sc.compare_saved(owner, "northwind", [s1["id"], s2["id"]])
    m1, m2 = (x["outputs"]["operating_income"] for x in cmp_["scenarios"])
    assert m2 > m1
    a = sc.analyze(owner, "northwind", "pnl_drivers", {"revenue": {"change_pct": 3}}, draws=5000, seed=1)
    assert a["sensitivity"][0]["swing"] >= a["sensitivity"][-1]["swing"]
    assert a["cases"]["worst"]["value"] < a["cases"]["base"]["value"] < a["cases"]["best"]["value"]
    sim = a["simulation"]["outputs"]["operating_margin"]
    assert sim["p5"] < sim["p50"] < sim["p95"]
    assert a == sc.analyze(owner, "northwind", "pnl_drivers", {"revenue": {"change_pct": 3}}, draws=5000, seed=1)


def test_scenario_validation_flags_ranges_large_changes_and_impossible_results(seeded):
    lyst, owner = seeded
    sc = lyst.services.scenarios
    r = sc.evaluate(owner, "northwind", "pnl_drivers", {"labor_share": {"value": 0.9}, "revenue": {"change_pct": -100}})
    checks = {c["check"]: c["status"] for c in r["validation"]}
    assert checks["range:labor_share"] == "warn" and checks["change:revenue"] == "warn"
    assert checks["finite:operating_margin"] == "fail"  # revenue of zero: margin undefined, said plainly
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    from lyst.security import AccessDenied

    with pytest.raises(AccessDenied):
        sc.define(viewer, "northwind", "mine", {"inputs": [{"name": "a", "value": 1}], "outputs": ["a"]})
    assert sc.evaluate(viewer, "northwind", "pnl_drivers")["outputs"]  # but may explore


# ----------------------------------------------------------------------------- decisions (scenario A)

COST_OPTIONS = {
    "question": "Which cost-reduction option should we choose to cut operating costs by 5% within $400k?",
    "alternatives": [
        {"name": "Renegotiate suppliers", "values": {"savings": {"low": 0.6, "base": 0.9, "high": 1.1}, "cost": 0.15,
                                                     "risk": 2, "months": {"low": 3, "base": 4, "high": 6}},
         "risks": ["supplier pushback on delivery terms"]},
        {"name": "Automate invoicing", "values": {"savings": {"low": 0.4, "base": 0.7, "high": 0.8}, "cost": 0.35,
                                                  "risk": 1, "months": {"low": 5, "base": 6, "high": 9}},
         "risks": ["integration delays"]},
        {"name": "Close regional office", "values": {"savings": {"low": 1.0, "base": 1.4, "high": 1.6}, "cost": 0.9,
                                                     "risk": 4, "months": 9},
         "risks": ["attrition", "customer disruption"]},
    ],
    "criteria": [{"name": "savings", "direction": "max", "weight": 0.5}, {"name": "cost", "direction": "min", "weight": 0.2},
                 {"name": "risk", "direction": "min", "weight": 0.2}, {"name": "months", "direction": "min", "weight": 0.1}],
    "constraints": [{"criterion": "cost", "op": "<=", "value": 0.4}],
    "evidence": ["Supplier contract review, Q3 2026", "Invoicing time study (AP team)"],
    "assumptions": ["Savings are annual run-rate in $M"],
    "expected_impact": {"metric": "operating_income", "value": 0.9, "unit": "$M per year"},
    "outcome_metric": "operating_income",
}


def test_framing_separates_questions_from_decisions_and_lists_whats_missing():
    f = decisions.frame("Should we choose between renegotiating suppliers, automating invoicing or closing an office "
                        "to cut operating costs within $400k?")
    assert f["kind"] == "decision" and f["requires_authorization"]
    assert f["alternatives"] == ["renegotiating suppliers", "automating invoicing", "closing an office"]
    assert f["objective"] == "cut operating costs" and f["constraints"] == ["$400k"]
    q = decisions.frame("What was revenue last quarter?")
    assert q["kind"] == "analysis" and not q["requires_authorization"] and q["missing"]


def test_scenario_a_compares_alternatives_with_uncertainty_and_explains_tradeoffs():
    a = decisions.analyze(COST_OPTIONS)
    assert "Close regional office" in a["infeasible"] and "cost" in a["infeasible"]["Close regional office"][0]
    assert a["ranking"][0]["name"] == "Renegotiate suppliers"
    u = a["uncertainty"]
    assert set(u["p_best"]) == {"Renegotiate suppliers", "Automate invoicing"} and abs(sum(u["p_best"].values()) - 1) < 1e-9
    assert a == decisions.analyze(COST_OPTIONS)  # seeded: reproducible
    flips = {f["criterion"]: f for f in a["weight_sensitivity"]}
    assert flips["risk"]["new_leader"] == "Automate invoicing"  # weighting risk enough changes the choice
    rec = decisions.recommendation(COST_OPTIONS, a)
    assert rec["status"] == "recommended" and rec["choice"] == "Renegotiate suppliers" and rec["requires_human_decision"]
    for field in ("evidence", "assumptions", "expected_impact", "uncertainty", "alternatives", "risks", "implementation",
                  "monitoring"):
        assert rec[field], field


def test_no_false_certainty_without_evidence_or_separation():
    no_evidence = {**COST_OPTIONS, "evidence": []}
    rec = decisions.recommendation(no_evidence, decisions.analyze(no_evidence))
    assert rec["status"] == "insufficient_evidence" and rec["choice"] is None
    close = {**COST_OPTIONS, "constraints": [], "alternatives": [
        {"name": "A", "values": {"savings": {"low": 0.5, "base": 1.0, "high": 1.5}, "cost": 0.3, "risk": 2, "months": 4}},
        {"name": "B", "values": {"savings": {"low": 0.5, "base": 1.0, "high": 1.5}, "cost": 0.3, "risk": 2, "months": 4}}]}
    rec = decisions.recommendation(close, decisions.analyze(close))
    assert rec["status"] == "no_clear_winner" and rec["choice"] is None


def test_allocation_under_a_budget_is_optimal():
    items = [{"name": "a", "cost": 5, "value": 10}, {"name": "b", "cost": 4, "value": 7}, {"name": "c", "cost": 3, "value": 6},
             {"name": "d", "cost": 6, "value": 11, "group": "g"}, {"name": "e", "cost": 2, "value": 3, "group": "g"}]
    r = decisions.allocate(items, 12, groups={"g": 1})
    assert r["status"] == "optimal" and r["total_value"] == 23 and r["total_cost"] <= 12
    # brute force agrees
    from itertools import combinations

    best = max(sum(i["value"] for i in combo) for k in range(6) for combo in combinations(items, k)
               if sum(i["cost"] for i in combo) <= 12 and sum(1 for i in combo if i.get("group") == "g") <= 1)
    assert r["total_value"] == best


def test_decision_record_needs_a_person_and_is_reviewed_against_its_outcome(seeded):
    lyst, owner = seeded
    admin = lyst.guard.create_user("admin@northwind.test", "Admin")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    ds = lyst.services.decisions
    d = ds.create(owner, "northwind", "Q4 cost reduction", COST_OPTIONS)
    assert d["status"] == "analyzed" and d["recommendation"]["choice"] == "Renegotiate suppliers"
    with pytest.raises(ValueError):
        ds.submit(owner, "northwind", d["id"], "Something else")
    with pytest.raises(ValueError):
        ds.review(owner, "northwind", d["id"], 1.0)  # not approved yet
    d = ds.submit(owner, "northwind", d["id"], "Automate invoicing", "Lower delivery risk this year")
    assert d["status"] == "submitted" and d["chosen"] == "Automate invoicing"  # a person may choose differently
    lyst.runtime.decide(admin, d["approval_id"], True)
    d = ds.get(owner, "northwind", d["id"])
    assert d["status"] == "approved" and d["decided_by"] == admin.user_id and d["review_at"] is not None
    d = ds.review(owner, "northwind", d["id"], 0.95, "Run-rate after two quarters")
    assert d["status"] == "reviewed" and d["outcome"]["verdict"] == "met"
    assert [x["id"] for x in ds.list(owner, "northwind")] == [d["id"]]


def test_scenario_and_decision_endpoints(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        models = c.get("/v1/workspaces/northwind/scenario-models", headers=h).json()
        assert [m["name"] for m in models] == ["pnl_drivers"] and models[0]["metric"] == "operating_margin"
        assert {i["name"]: (i["observed"], i["unit"]) for i in models[0]["inputs"]}["labor_share"] == (False, "ratio")
        assert c.get("/v1/workspaces/northwind/metrics/operating_margin/scenario-models", headers=h).json() == ["pnl_drivers"]
        r = c.post("/v1/workspaces/northwind/what-if", headers=h, json={"question": QUESTION}).json()
        assert r["kind"] == "hypothetical" and r["summary"].endswith("not observed results.")
        bad = c.post("/v1/workspaces/northwind/scenario-models", headers=h,
                     json={"name": "x", "spec": {"inputs": [{"name": "a", "value": 1}], "formulas": [{"name": "b", "expr": "a.__class__"}],
                                                 "outputs": ["b"]}})
        assert bad.status_code == 400
        tid = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?")
        r = c.post("/v1/workspaces/northwind/decision-records", headers=h, json={"title": "Costs", "spec": COST_OPTIONS, "task_id": tid})
        assert r.status_code == 200 and r.json()["recommendation"]["status"] == "recommended"
        assert r.json()["kind"] == "decision_record" and r.json()["task_id"] == tid
        r = c.post("/v1/workspaces/northwind/decision-records/allocate", headers=h,
                   json={"items": [{"name": "a", "cost": 2, "value": 3}, {"name": "b", "cost": 2, "value": 4}], "budget": 2})
        assert [x["name"] for x in r.json()["chosen"]] == ["b"]


def test_points_changes_apply_and_unapplied_changes_are_stated_never_dropped(seeded):
    from lyst.demo import PNL_DRIVERS

    m = Model(PNL_DRIVERS)
    q = "What if revenue falls 5% and the COGS ratio rises 2 points next quarter?"
    p = scenarios.parse_what_if(m, q)
    assert p["overrides"] == {"revenue": {"change_pct": -5.0}, "cogs_ratio": {"change_abs": 0.02}} and p["unapplied"] == []
    assert p["described"]["cogs_ratio"] == "COGS ratio +2 points"
    p = scenarios.parse_what_if(m, "What if revenue falls 5% and opex rises by $200k?")
    assert p["overrides"] == {"revenue": {"change_pct": -5.0}}
    assert p["unapplied"] and "opex rises by $200k" in p["unapplied"][0]["phrase"]
    lyst, owner = seeded
    t = ask(lyst, owner, q)
    r = t["result"]
    assert r["status"] == "complete" and "COGS ratio +2 points" in r["answer"]
    t = ask(lyst, owner, "What happens to operating margin if revenue falls 5% and opex rises by $200k?")
    r = t["result"]
    assert r["status"] == "unverified" and "I couldn't apply: opex rises by $200k" in r["answer"]
    assert any("Not applied" in f for f in r["evidence"])
    for q_ in r["supporting"]["queries"]:  # the same keys as every other query in Supporting work
        assert {"purpose", "sql", "query_id", "rows", "ms", "datasets"} <= set(q_)


def test_ranges_may_be_given_as_lists_and_bad_values_are_400s(seeded):
    spec = {**COST_OPTIONS, "alternatives": [
        {**a, "values": {k: ([v["low"], v["base"], v["high"]] if isinstance(v, dict) else v) for k, v in a["values"].items()}}
        for a in COST_OPTIONS["alternatives"]]}
    assert decisions.analyze(spec) == decisions.analyze(COST_OPTIONS)
    assert decisions.recommendation(spec, decisions.analyze(spec))["choice"] == "Renegotiate suppliers"
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post("/v1/workspaces/northwind/decision-records", headers=h, json={"title": "Lists", "spec": spec})
        assert r.status_code == 200 and r.json()["recommendation"]["choice"] == "Renegotiate suppliers"
        bad = {**COST_OPTIONS, "alternatives": [{**COST_OPTIONS["alternatives"][0], "values": {
            **COST_OPTIONS["alternatives"][0]["values"], "cost": ["cheap"]}}, COST_OPTIONS["alternatives"][1]]}
        assert c.post("/v1/workspaces/northwind/decision-records", headers=h, json={"title": "Bad", "spec": bad}).status_code == 400
