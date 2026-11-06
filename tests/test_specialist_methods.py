# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Specialist methods: known answers on planted data, end-to-end runs through the core, access rules, evidence."""

import pandas as pd
import pytest

from lyst.analytics import methods as M
from lyst.specialists import methods as SM
from lyst.specialists import packs, representative, samples
from lyst.specialists.core import AnalystRequest


def test_process_map_finds_the_planted_bottleneck_and_rework():
    out = M.process_map(samples.order_events(), "order_id", "activity", "event_time")
    assert out["metrics"]["cases"] == 60 and out["metrics"]["rework_cases"] == 10
    assert "created → approved" in out["summary"]


def test_log_patterns_flags_only_the_bursting_account():
    out = M.log_patterns(samples.logins(), ts="occurred_at", event="event", entity="username")
    assert out["metrics"]["bursting_entities"] == 1
    assert out["tables"][1]["rows"][0][0] == "user07"
    assert out["metrics"]["rare_types"] >= 1  # the single password reset


def test_distances_are_great_circle_and_thresholded():
    out = M.distances(samples.stores(), samples.warehouses(), lat="lat", lon="lon", tlat="lat", tlon="lon", label="store",
                      tlabel="warehouse", within_km=300)
    assert out["metrics"]["beyond"] == 2
    far = {r[0] for r in out["tables"][0]["rows"][:2]}
    assert far == {"Munich", "Freiburg"}
    # Berlin to Hamburg is about 255 km on the great circle
    d = M._haversine_km(52.520, 13.405, 53.551, 9.994)
    assert 250 < d < 260
    with pytest.raises(M.MethodError):
        M.distances(pd.DataFrame({"lat": [200], "lon": [0]}), samples.warehouses(), lat="lat", lon="lon", tlat="lat", tlon="lon")


def test_reconcile_finds_every_planted_break():
    erp, bank = samples.ledgers()
    out = M.reconcile(erp, bank, key="invoice_id", value="amount", names=("erp", "bank"))
    m = out["metrics"]
    assert (m["only_left"], m["only_right"], m["breaks"], m["totals_agree"]) == (1, 1, 1, False)
    keys = {r[0] for r in out["tables"][0]["rows"]}
    assert keys == {"INV010", "INV999", "INV005"}
    same = M.reconcile(erp, erp.copy(), key="invoice_id", value="amount")
    assert same["metrics"]["totals_agree"] and same["metrics"]["breaks"] == 0


def test_graph_components_and_degree():
    out = M.graph(samples.supplier_links(), "source", "target")
    assert out["metrics"] == {"nodes": 7, "edges": 7, "components": 2, "largest": 4}
    assert out["tables"][0]["rows"][0][0] in ("Acme", "Dyna")


def test_segment_recovers_three_planted_groups():
    out = M.segment(samples.customers(), ["annual_spend", "orders", "support_tickets"])
    assert out["metrics"]["k"] == 3 and out["metrics"]["silhouette"] > 0.5
    assert sorted(r[1] for r in out["tables"][0]["rows"]) == [80, 80, 80]


def test_classify_finds_the_planted_driver_and_beats_the_baseline():
    out = M.classify(samples.customers(), "churned", ["annual_spend", "orders", "support_tickets"], order_by="signup_date")
    assert out["metrics"]["auc"] > 0.85 and out["metrics"]["accuracy"] > out["metrics"]["baseline"]
    assert out["tables"][0]["rows"][0][0] == "support_tickets"
    with pytest.raises(M.MethodError):
        M.classify(samples.customers().head(10), "churned", ["orders"])


def test_choose_needs_a_named_or_unambiguous_dataset():
    cat = [{"name": "order_events", "title": "Order Events", "schema": [
        {"name": "order_id", "type": "VARCHAR"}, {"name": "activity", "type": "VARCHAR"}, {"name": "event_time", "type": "TIMESTAMP"}]},
           {"name": "pnl", "title": "P&L", "schema": [{"name": "month", "type": "DATE"}, {"name": "revenue", "type": "DOUBLE"}]}]
    pick = SM.choose("Where do orders get stuck in the order events process?", ["process_analysis"], cat)
    assert pick["method"] == "process_map" and pick["params"] == {"case": "order_id", "activity": "activity", "ts": "event_time"}
    assert SM.choose("Why did revenue fall?", ["variance_explanation"], cat) is None


@pytest.fixture
def sampled(seeded):
    lyst, owner = seeded
    samples.load_all(lyst, owner, "northwind")
    return lyst, owner


def test_methods_run_end_to_end_through_the_core(sampled):
    lyst, owner = sampled
    out = lyst.analyst_core.handle(owner, AnalystRequest(workspace_id="northwind", text="Reconcile ledger erp against ledger bank"))
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, out.task_id)
    assert t["outcome"] == "complete", t["result"]["answer"]
    assert t["result"]["answer"].startswith("Totals do not agree: ledger_erp")
    assert any(s["capability"] == "run_method" for s in t["steps"])
    assert any("SELECT" in q["sql"] for q in t["result"]["supporting"]["queries"])  # reads went through the SQL guard
    checks = {c["name"]: c["passed"] for c in t["result"]["confidence"]["checks"]}
    assert checks["method_ran"] and checks.get("fits_request", True)


def test_tasks_without_expertise_never_use_specialist_methods(sampled):
    lyst, owner = sampled
    tid = lyst.runtime.submit(owner, "northwind", "Reconcile ledger erp against ledger bank", extra={"expertise": None})
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, tid)
    assert not any(s["capability"] == "run_method" for s in t["steps"])


def test_method_reads_respect_data_policy(sampled):
    lyst, owner = sampled
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    lyst.governance.classify(owner, "northwind", "login_events", "restricted", reason="security telemetry")
    out = lyst.analyst_core.handle(viewer, AnalystRequest(workspace_id="northwind", text="Analyze the login events for suspicious patterns"))
    lyst.runtime.drain()
    t = lyst.runtime.get(viewer, out.task_id)
    assert t["status"] == "failed" or t["outcome"] != "complete"
    assert "user07" not in str(t.get("result"))  # the restricted rows never reached the viewer


def test_representative_scenarios_still_pass_and_match_recorded_evidence(sampled):
    from lyst.specialists.maturity import evidence

    lyst, owner = sampled
    packs.load_all(lyst, owner, "northwind")
    results = representative.run(lyst, owner, "northwind")
    failed = {pid: [c["check"] for s in r["scenarios"] for c in s["checks"] if not c["passed"]]
              for pid, r in results.items() if not r["passed"]}
    assert not failed, failed
    assert set(evidence()) == {pid for pid, r in results.items() if r["passed"]}


def test_raw_logs_in_four_formats_parse_and_the_guessed_account_is_found():
    parsed = M.parse_logs(samples.raw_auth_logs()["line"])
    assert int(parsed["_unparsed"].sum()) == 1  # the garbage line is counted, not guessed
    seq = M.failure_then_success(parsed.dropna(subset=["ts"]), ts="ts", event="event", entity="entity")
    assert list(seq["entity"]) == ["admin"] and int(seq.iloc[0]["failures"]) == 8
    fw = M.parse_logs(samples.firewall_logs()["line"])
    out = M.log_patterns(fw, ts="ts", event="event", entity="entity")
    assert out["tables"][1]["rows"][0][0] == "10.0.0.66"


def test_graph_finds_the_broker_communities_and_a_path():
    out = M.graph(samples.service_dependencies(), "source", "target", query="path from web to ledger")
    assert "auth-service is the main broker" in out["summary"]
    assert any("Shortest path: web → auth-service → checkout → payments → ledger" in f for f in out["findings"])
    adj = M._adjacency(samples.wallet_transfers())
    bc = M.betweenness(adj)
    assert max(bc, key=bc.get) == "0xHUB"


def test_optimization_is_exact_and_respects_limits():
    out = M.optimize_allocation(samples.channel_options(), option="channel", cost="cost_per_unit", value="return_per_unit",
                                budget=100_000, max_units="max_units")
    plan = {r[0]: r[1] for r in out["tables"][0]["rows"]}
    assert plan["Search"] == 600 and plan["Email"] == 2000  # best returns per cost first, at their limits
    assert out["metrics"]["spend"] <= 100_000
    with pytest.raises(M.MethodError):
        M.optimize_allocation(samples.channel_options(), option="channel", cost="cost_per_unit", value="return_per_unit", budget=0)


def test_text_themes_find_the_planted_theme_and_negative_share():
    out = M.text_themes(samples.ticket_comments(), "comment", group="channel")
    assert "refund" in out["summary"] and out["metrics"]["negative_share"] > 0.3
    assert any("order number" in f for f in out["findings"])


def test_multiclass_classification_beats_the_baseline():
    out = M.classify(samples.maintenance_history(), "failure_mode", ["vibration", "temperature", "current_amps"],
                     order_by="recorded_at")
    assert out["metrics"]["classes"] == 3 and out["metrics"]["accuracy"] > out["metrics"]["baseline"] + 0.2


def test_evaluate_names_the_weakest_segment():
    out = M.evaluate_predictions(samples.regression_results("rig_tests", "load_case", ["bending", "torsion", "thermal"],
                                                            "torsion"), "actual", "predicted", segment="load_case")
    assert "torsion is the weakest segment" in out["summary"]
    ev = M.evaluate_predictions(samples.eval_results("llm_eval_results", "model", ["model-a", "model-b", "model-c"], "model-b"),
                                "passed", passed="passed", segment="model")
    assert "model-b is weakest" in ev["summary"]
