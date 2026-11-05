# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The task harness: contracts, change receipts, bounded loops, durable state, failures that upgrade the harness."""

import json

import pytest
from fastapi.testclient import TestClient

from lyst import harness
from lyst.api import create_app

Q = "What was revenue last quarter?"


def _fail_with(lyst, monkeypatch, capability, exc):
    real = lyst.services.registry.run

    def run(name, ctx, args):
        if name == capability:
            raise exc
        return real(name, ctx, args)

    monkeypatch.setattr(lyst.services.registry, "run", run)


def _approval_for(lyst, owner, rule_id):
    return next(a for a in lyst.runtime.approvals(owner, "northwind")
                if a["action"] == "harness.update" and json.loads(a["payload"])["rule_id"] == rule_id)


def test_every_task_has_a_frozen_contract_and_each_run_a_receipt(seeded):
    lyst, owner = seeded
    tid = lyst.runtime.submit(owner, "northwind", Q)
    c = lyst.harness.contract(owner, tid)["contract"]
    assert c["goal"] == Q and c["intact"] and len(c["sha256"]) == 64
    assert set(harness.DEFAULT_DONE_WHEN) <= set(c["done_when"])
    assert c["constraints"]["budget_usd"] == 1.0 and c["constraints"]["max_retries"] == 3
    lyst.runtime.drain()
    out = lyst.harness.contract(owner, tid)
    assert out["met"] is True and all(r["passed"] is not False for r in out["checks"])
    [r] = lyst.harness.receipts(owner, tid)
    assert r["run"] == 1 and r["state"] == "done" and r["contract"]["met"] is True
    assert "contextualize" in r["tools_used"] and "deliver" in r["tools_used"]
    assert any(s["kind"] == "metric" and s["dataset_version"] for s in r["context_sources"])
    assert r["policy_version"]["harness_rules"] == "0"
    assert r["accepted_artifact"]["kind"] == "answer"
    assert r["rollback_point"]["changed"] == [] and r["rollback_point"]["dataset_versions_read"]
    st = lyst.harness.state(owner, tid)
    assert st["status"] == "done" and st["resume"]["resumable"] is False and st["artifacts"]
    assert st["budgets"]["usd"]["limit"] == 1.0


def test_a_tampered_contract_reads_as_not_intact(seeded):
    lyst, owner = seeded
    tid = lyst.runtime.submit(owner, "northwind", Q)
    lyst.stores.control.execute("UPDATE task_contracts SET contract = json_merge_patch(contract, '{\"goal\": \"x\"}') "
                                "WHERE task_id = ?", [tid])
    assert harness.contract_of(lyst.stores.control, tid)["intact"] is False


def test_the_cost_budget_stops_a_run_through_fail(seeded):
    lyst, owner = seeded
    tid = lyst.runtime.submit(owner, "northwind", Q, budget_usd=0.01)
    lyst.stores.control.execute(
        "INSERT INTO llm_calls (id, workspace_id, task_id, model, purpose, outcome, cost_usd) "
        "VALUES (nextval('llm_call_seq'), 'northwind', ?, 'm', 'narrate', 'ok', 0.5)", [tid])
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "failed" and "cost budget of $0.01" in t["error"]
    [r] = lyst.harness.receipts(owner, tid)
    assert r["contract"]["met"] is False and r["cost_usd"] == 0.5
    f = lyst.harness.failures(owner, "northwind")
    assert f[0]["failure_class"] == "repeated_loop" and f[0]["fix_kind"] == "budget"


def test_retries_are_capped(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_TASK_MAX_RETRIES", "1")
    _fail_with(lyst, monkeypatch, "deliver", RuntimeError("renderer down"))
    tid = lyst.runtime.submit(owner, "northwind", Q)
    lyst.runtime.drain()
    lyst.runtime.retry(owner, tid)
    lyst.runtime.drain()
    assert lyst.runtime.get(owner, tid)["status"] == "failed"
    with pytest.raises(ValueError, match="retried 1 time"):
        lyst.runtime.retry(owner, tid)
    assert [r["run"] for r in lyst.harness.receipts(owner, tid)] == [1, 2]
    # a transient failure teaches nothing: no failure class, no proposal
    assert lyst.harness.failures(owner, "northwind") == []


def test_a_repeated_wrong_tool_becomes_a_route_only_after_a_person_approves(seeded, monkeypatch):
    lyst, owner = seeded
    _fail_with(lyst, monkeypatch, "validate", ValueError("cannot compare 'x' with 3"))
    first = lyst.runtime.submit(owner, "northwind", Q)
    lyst.runtime.drain()
    assert lyst.harness.rules(owner, "northwind") == []  # once is not a pattern
    second = lyst.runtime.submit(owner, "northwind", Q)
    lyst.runtime.drain()
    [rule] = lyst.harness.rules(owner, "northwind")
    assert rule["kind"] == "tool" and rule["capability"] == "validate" and rule["status"] == "proposed"
    assert len(rule["source_failures"]) == 2
    events = [e["kind"] for e in lyst.runtime.get(owner, second)["events"]]
    assert "harness_failure" in events and "harness_proposal" in events
    # proposed is not in force: the next run is not routed
    third = lyst.runtime.submit(owner, "northwind", Q)
    assert harness.contract_of(lyst.stores.control, third)["constraints"]["refused_tools"] == []
    lyst.runtime.decide(owner, _approval_for(lyst, owner, rule["id"])["id"], True)
    assert lyst.harness.rules(owner, "northwind", status="active")[0]["id"] == rule["id"]
    fourth = lyst.runtime.submit(owner, "northwind", Q)
    c = harness.contract_of(lyst.stores.control, fourth)
    assert c["constraints"]["refused_tools"][0]["capability"] == "validate" and rule["id"] in c["rules_applied"]
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, fourth)
    assert t["status"] == "failed" and f"harness rule {rule['id']}" in t["error"]
    # a routed run is the rule working, not a new failure; the receipt names the rules in force
    assert lyst.harness.receipts(owner, fourth)[0]["policy_version"]["harness_rules"].startswith("1:")
    assert lyst.harness.rules(owner, "northwind", status="active")[0]["hits"] >= 1
    # a person retires it and later runs are no longer routed
    lyst.harness.retire(owner, "northwind", rule["id"])
    assert harness.contract_of(lyst.stores.control, lyst.runtime.submit(owner, "northwind", Q))["rules_applied"] == []
    assert first != second


def test_missing_context_becomes_a_guide_in_memory(seeded, monkeypatch):
    lyst, owner = seeded
    _fail_with(lyst, monkeypatch, "plan", KeyError("no metric named churn_rate"))
    for _ in range(2):
        lyst.runtime.submit(owner, "northwind", "What is churn rate?")
        lyst.runtime.drain()
    [rule] = lyst.harness.rules(owner, "northwind")
    assert rule["kind"] == "guide"
    lyst.harness.edit(owner, "northwind", rule["id"], "Churn rate is not defined yet; use the retention metric.")
    lyst.runtime.decide(owner, _approval_for(lyst, owner, rule["id"])["id"], True)
    active = lyst.harness.rules(owner, "northwind", status="active")[0]
    assert active["memory_id"]
    mem = lyst.memory.list(owner, "northwind")
    assert any(m["id"] == active["memory_id"] and "retention metric" in m["content"] for m in mem)
    lyst.harness.retire(owner, "northwind", rule["id"])
    assert not any(m["id"] == active["memory_id"] and m.get("revoked_at") is None
                   for m in lyst.memory.list(owner, "northwind"))


def test_a_rejected_rule_is_never_proposed_again(seeded, monkeypatch):
    lyst, owner = seeded
    _fail_with(lyst, monkeypatch, "validate", ValueError("cannot compare 'x' with 3"))
    for _ in range(2):
        lyst.runtime.submit(owner, "northwind", Q)
        lyst.runtime.drain()
    [rule] = lyst.harness.rules(owner, "northwind")
    lyst.runtime.decide(owner, _approval_for(lyst, owner, rule["id"])["id"], False)
    lyst.runtime.submit(owner, "northwind", Q)
    lyst.runtime.drain()
    assert [r["status"] for r in lyst.harness.rules(owner, "northwind")] == ["rejected"]


def test_approved_test_budget_and_permission_rules_tighten_the_contract(seeded):
    lyst, owner = seeded
    db = lyst.stores.control
    key = harness.job_key(Q)
    for rid, kind, cap, spec in (("hr_t", "test", None, {"checks": ["has_sources"],
                                                          "validation_checks": ["coverage:current"]}),
                                 ("hr_b", "budget", None, {"max_steps": 30, "budget_usd": 0.5, "max_retries": 0}),
                                 ("hr_p", "permission", "deliver", {})):
        db.execute("INSERT INTO harness_rules (id, workspace_id, kind, job_key, capability, signature, text, spec, "
                   "status, source_failures) VALUES (?, 'northwind', ?, ?, ?, 's', 'a rule text', ?, 'active', '[]')",
                   [rid, kind, key, cap, json.dumps(spec)])
    tid = lyst.runtime.submit(owner, "northwind", Q, autonomy="autonomous")
    c = harness.contract_of(db, tid)
    assert "has_sources" in c["done_when"] and "check:coverage:current" in c["done_when"]
    assert c["constraints"]["max_steps"] == 30 and c["constraints"]["budget_usd"] == 0.5
    assert c["constraints"]["max_retries"] == 0
    assert db.scalar("SELECT max_steps FROM tasks WHERE id = ?", [tid]) == 30
    # the permission rule gates deliver even in autonomous mode
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "awaiting_approval"
    st = lyst.harness.state(owner, tid)
    assert st["pending"] and "harness rule hr_p" in st["pending"][0]["summary"]
    assert st["resume"]["resumable"] and st["resume"]["from_step"] is not None
    lyst.runtime.decide(owner, st["pending"][0]["approval_id"], True)
    lyst.runtime.drain()
    out = lyst.harness.contract(owner, tid)
    assert lyst.runtime.get(owner, tid)["status"] == "done"
    assert {r["id"]: r["passed"] for r in out["checks"]}["has_sources"] is True
    with pytest.raises(ValueError):
        lyst.runtime.retry(owner, tid)  # not failed, and the budget rule allows no retries anyway


def test_a_rejected_step_ends_with_a_receipt_and_proposes_a_gate(seeded):
    lyst, owner = seeded
    db = lyst.stores.control
    db.execute("INSERT INTO harness_rules (id, workspace_id, kind, job_key, capability, signature, text, spec, status, "
               "source_failures) VALUES ('hr_p', 'northwind', 'permission', ?, 'deliver', 's', 'ask first', '{}', "
               "'active', '[]')", [harness.job_key(Q)])
    tid = lyst.runtime.submit(owner, "northwind", Q)
    lyst.runtime.drain()
    ap = lyst.harness.state(owner, tid)["pending"][0]["approval_id"]
    lyst.runtime.decide(owner, ap, False)
    [r] = lyst.harness.receipts(owner, tid)
    assert r["state"] == "cancelled" and r["human_corrections"]["approvals"][0]["status"] == "rejected"
    f = lyst.harness.failures(owner, "northwind")[0]
    assert f["failure_class"] == "unsafe_action" and f["capability"] == "deliver"


def test_needs_input_is_a_lost_decision(seeded):
    lyst, owner = seeded
    task = {"id": "t", "status": "done", "error": None}
    got = harness.classify(task, [], [], {"status": "needs_input", "answer": "Which region?"}, [])
    assert got["class"] == "lost_decision" and harness.FIX_FOR[got["class"]] == "state"


def test_harness_routes_and_checklist(seeded):
    lyst, owner = seeded
    tid = lyst.runtime.submit(owner, "northwind", Q)
    lyst.runtime.drain()
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst)) as c:
        assert c.get(f"/v1/tasks/{tid}/contract", headers=h).json()["met"] is True
        assert c.get(f"/v1/tasks/{tid}/receipts", headers=h).json()[0]["run"] == 1
        assert c.get(f"/v1/tasks/{tid}/state", headers=h).json()["status"] == "done"
        rep = c.get("/v1/workspaces/northwind/harness", headers=h).json()
        assert len(rep["checklist"]) == 12 and {i["answer"] for i in rep["checklist"]} <= {"yes", "partly", "no"}
        byid = {i["id"]: i for i in rep["checklist"]}
        assert byid["success_defined"]["answer"] == "yes" and byid["explainable"]["answer"] == "yes"
        assert c.get("/v1/workspaces/northwind/harness/rules", headers=h).json() == []
        assert c.get("/v1/workspaces/northwind/harness/failures", headers=h).json() == []
        assert c.put("/v1/workspaces/northwind/harness/rules/nope", headers=h, json={"text": "abcdefghij"}).status_code == 404
        assert c.get("/v1/tasks/nope/state", headers=h).status_code == 404
