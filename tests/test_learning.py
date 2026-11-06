# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Learning from people: a verdict on an answer changes what Lyst reuses, reversibly, and only for roles that govern.

The loop before this lane learned only from Lyst's own checks: a wrong answer a person flagged kept being recalled,
cited as a prior answer, and run again as a verified workflow. These tests pin the opposite."""

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.feedback import disputed_reason

from .conftest import ask

Q2 = "Why did operating margin decline in Q2 2026?"
Q3 = "Why did operating margin decline in Q3 2026?"


def _analysis(task):
    return "a_" + task["id"]


def _episodic(lyst, aid):
    return lyst.stores.workspace("northwind").query(
        "SELECT id, trust, disputed_at FROM memory WHERE layer = 'episodic' AND provenance->>'analysis_id' = ?", [aid])[0]


def _recalled(lyst, owner, mid, text):
    return mid in {m["id"] for m in lyst.memory.recall(owner, "northwind", text, limit=50)}


def test_an_incorrect_verdict_stops_every_reuse_of_the_wrong_answer(seeded):
    lyst, owner = seeded
    first = ask(lyst, owner, Q2)
    aid, wf = _analysis(first), first["result"]["supporting"]["workflow"]["compiled"]
    mem = _episodic(lyst, aid)
    assert wf["created"] and _recalled(lyst, owner, mem["id"], Q2)
    assert any(p["analysis_id"] == aid for p in lyst.knowledge.prior_answers(owner, "northwind", Q2))

    out = lyst.feedback.give(owner, "northwind", aid, "incorrect", note="EMEA was a reclassification, not a decline")
    assert out["governs"] is True and out["analysis"]["status"] == "disputed"
    assert {"id": wf["id"], "version": wf["version"]} in out["analysis"]["workflows"]
    # memory: out of recall, still stored (nothing is deleted)
    assert _episodic(lyst, aid)["disputed_at"] is not None and not _recalled(lyst, owner, mem["id"], Q2)
    # prior answers: never cited again
    assert all(p["analysis_id"] != aid for p in lyst.knowledge.prior_answers(owner, "northwind", Q2))
    # the workflow: stale with the reason, so the next question is planned afresh
    w = lyst.services.workflows.get(owner, "northwind", wf["id"])
    assert w["status"] == "stale" and w["invalid_reason"] == disputed_reason(aid)
    again = ask(lyst, owner, Q3)["result"]
    assert again["supporting"]["workflow"]["used"] is None
    # the same steps on the same data are not recompiled: Lyst does not relearn the answer a person rejected
    assert again["supporting"]["workflow"]["compiled"]["blocked"]
    assert lyst.services.workflows.get(owner, "northwind", wf["id"])["version"] == wf["version"]


def test_withdrawing_the_verdict_undoes_every_effect(seeded):
    lyst, owner = seeded
    first = ask(lyst, owner, Q2)
    aid, wf = _analysis(first), first["result"]["supporting"]["workflow"]["compiled"]
    fb = lyst.feedback.give(owner, "northwind", aid, "incorrect")
    out = lyst.feedback.withdraw(owner, "northwind", fb["id"])
    assert out["withdrawn_at"] is not None and out["analysis"]["status"] == "open"
    assert _episodic(lyst, aid)["disputed_at"] is None
    assert lyst.services.workflows.get(owner, "northwind", wf["id"])["status"] == "active"
    assert ask(lyst, owner, Q3)["result"]["supporting"]["workflow"]["used"]["id"] == wf["id"]


def test_a_correct_verdict_validates_the_memory_and_a_correction_is_recalled(seeded):
    lyst, owner = seeded
    aid = _analysis(ask(lyst, owner, Q2))
    out = lyst.feedback.give(owner, "northwind", aid, "correct")
    assert out["analysis"]["status"] == "confirmed" and _episodic(lyst, aid)["trust"] == "user_validated"
    # a later verdict by the same person replaces the earlier one
    out = lyst.feedback.give(owner, "northwind", aid, "partly",
                             correction="Operating margin in Q2 2026 also fell because of a one-off freight charge")
    assert out["analysis"]["status"] == "open" and _episodic(lyst, aid)["trust"] == "observed"
    standing = [f for f in lyst.feedback.for_analysis(owner, "northwind", aid)["feedback"] if f["withdrawn_at"] is None]
    assert [f["verdict"] for f in standing] == ["partly"]
    hits = lyst.memory.recall(owner, "northwind", "operating margin freight charge")
    assert any(m["id"] == out["correction_memory_id"] and m["trust"] == "user_validated" for m in hits)


def test_a_viewers_verdict_is_counted_but_changes_nothing_reused(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("vic@northwind.test", "Vic")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    first = ask(lyst, owner, Q2)
    aid, wf = _analysis(first), first["result"]["supporting"]["workflow"]["compiled"]
    out = lyst.feedback.give(viewer, "northwind", aid, "incorrect", correction="should not be stored")
    assert out["governs"] is False and out["correction_memory_id"] is None and out["analysis"]["status"] == "open"
    assert lyst.services.workflows.get(owner, "northwind", wf["id"])["status"] == "active"
    s = lyst.feedback.summary(owner, "northwind")
    assert s["verdicts"]["incorrect"] == 1 and s["analyses"]["disputed"] == 0
    # only the giver or an admin may take a verdict back
    other = lyst.guard.create_user("eve@northwind.test", "Eve")
    lyst.guard.add_member(owner, "northwind", other.user_id, "editor")
    with pytest.raises(PermissionError):
        lyst.feedback.withdraw(other, "northwind", out["id"])
    assert lyst.feedback.withdraw(owner, "northwind", out["id"])["withdrawn_at"] is not None


def test_a_workflow_stays_held_back_while_any_dispute_stands(seeded):
    lyst, owner = seeded
    first = ask(lyst, owner, Q2)
    a1, wf = _analysis(first), first["result"]["supporting"]["workflow"]["compiled"]
    a2 = _analysis(ask(lyst, owner, Q3))  # ran on the compiled workflow
    fb1 = lyst.feedback.give(owner, "northwind", a1, "incorrect")
    lyst.feedback.give(owner, "northwind", a2, "incorrect")
    lyst.feedback.withdraw(owner, "northwind", fb1["id"])
    w = lyst.services.workflows.get(owner, "northwind", wf["id"])
    assert w["status"] == "stale" and w["invalid_reason"] == disputed_reason(a2)


def test_a_crash_between_the_verdict_and_its_effects_is_repaired_by_reconcile(seeded, monkeypatch):
    """Fault injection: the verdict is stored, then the process dies before any effect is applied."""
    lyst, owner = seeded
    first = ask(lyst, owner, Q2)
    aid, wf = _analysis(first), first["result"]["supporting"]["workflow"]["compiled"]
    real = lyst.feedback.reconcile

    def crash(*a, **k):
        raise RuntimeError("worker died")

    monkeypatch.setattr(lyst.feedback, "reconcile", crash)
    with pytest.raises(RuntimeError):
        lyst.feedback.give(owner, "northwind", aid, "incorrect")
    assert lyst.feedback.for_analysis(owner, "northwind", aid)["status"] == "open"  # half done
    monkeypatch.setattr(lyst.feedback, "reconcile", real)
    out = lyst.feedback.reconcile(owner, "northwind", aid)
    assert out["status"] == "disputed"
    assert lyst.services.workflows.get(owner, "northwind", wf["id"])["status"] == "stale"
    assert lyst.feedback.reconcile(owner, "northwind", aid)["changed"] == []  # idempotent


def test_feedback_over_http(seeded):
    lyst, owner = seeded
    aid = _analysis(ask(lyst, owner, Q2))
    token = lyst.guard.issue_token(owner, "t")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        h = {"Authorization": f"Bearer {token}"}
        r = c.post(f"/v1/workspaces/northwind/analyses/{aid}/feedback", json={"verdict": "wrong"}, headers=h)
        assert r.status_code == 400
        r = c.post(f"/v1/workspaces/northwind/analyses/{aid}/feedback", json={"verdict": "incorrect"}, headers=h)
        assert r.status_code == 200 and r.json()["analysis"]["status"] == "disputed"
        listed = c.get("/v1/workspaces/northwind/analyses", headers=h).json()
        assert next(a for a in listed if a["id"] == aid)["status"] == "disputed"
        fid = r.json()["id"]
        assert c.post(f"/v1/workspaces/northwind/feedback/{fid}/withdraw", headers=h).json()["analysis"]["status"] == "open"
        assert c.get(f"/v1/workspaces/northwind/analyses/{aid}/feedback", headers=h).json()["feedback"][0]["id"] == fid
        assert c.post("/v1/workspaces/northwind/analyses/a_missing/feedback", json={"verdict": "correct"},
                      headers=h).status_code == 404


# -- outcomes reach reuse and rules (phase 2) ------------------------------------------------------------------------


def test_a_workflow_whose_runs_keep_failing_verification_is_held_back(seeded):
    lyst, owner = seeded
    wf = ask(lyst, owner, Q2)["result"]["supporting"]["workflow"]["compiled"]
    wfs = lyst.services.workflows
    assert wfs.record_run("northwind", wf["id"], wf["version"], verified=True) is None
    assert wfs.record_run("northwind", wf["id"], wf["version"], verified=False) is None  # once is not a pattern
    reason = wfs.record_run("northwind", wf["id"], wf["version"], verified=False)
    assert reason and "failed verification" in reason
    w = wfs.get(owner, "northwind", wf["id"])
    assert w["status"] == "stale" and w["invalid_reason"] == reason
    # the next question is planned afresh; its verified run compiles a new version with fresh counts
    again = ask(lyst, owner, Q3)["result"]["supporting"]["workflow"]
    assert again["used"] is None and again["compiled"]["created"] and again["compiled"]["version"] == wf["version"] + 1
    held = lyst.learning.report(owner, "northwind")["workflows"]
    assert held["active"] == 1 and held["held_back"] == {}  # the held-back version is superseded by the new one
    assert held["compiled_runs"] == 3 and held["compiled_runs_verified_rate"] == round(1 / 3, 3)


def _failing(lyst, monkeypatch, capability, exc):
    real = lyst.services.registry.run

    def run(name, ctx, args):
        if name == capability:
            raise exc
        return real(name, ctx, args)

    monkeypatch.setattr(lyst.services.registry, "run", run)
    return real


def _approve_rule(lyst, owner, rule_id):
    import json  # noqa: PLC0415

    a = next(a for a in lyst.runtime.approvals(owner, "northwind")
             if a["action"] == "harness.update" and json.loads(a["payload"])["rule_id"] == rule_id)
    lyst.runtime.decide(owner, a["id"], True)


def test_an_approved_rule_is_measured_and_flagged_when_the_failure_keeps_happening(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_HARNESS_EFFECT_MIN_RUNS", "2")
    q = "What is churn rate?"
    _failing(lyst, monkeypatch, "plan", KeyError("no metric named churn_rate"))
    for _ in range(2):
        ask(lyst, owner, q)
    [rule] = lyst.harness.rules(owner, "northwind")
    assert rule["effect"] is None  # not in force yet
    _approve_rule(lyst, owner, rule["id"])
    ask(lyst, owner, q)
    assert lyst.harness.rules(owner, "northwind", status="active")[0]["effect"]["verdict"] == "too few runs yet"
    ask(lyst, owner, q)
    eff = lyst.harness.rules(owner, "northwind", status="active")[0]["effect"]
    assert eff["ineffective"] and eff["rate_before"] == 1.0 and eff["rate_after"] == 1.0 and eff["runs_after"] == 2
    # a person is told once; retiring it stays their decision
    assert lyst.stores.workspace("northwind").scalar(
        "SELECT count(*) FROM findings WHERE dedupe_key = ?", [f"harness-ineffective:{rule['id']}"]) == 1
    rep = lyst.learning.report(owner, "northwind")["harness"]
    assert rep["in_force"] == 1 and rep["ineffective"] == 1 and rep["rules"][0]["id"] == rule["id"]


def test_a_rule_that_stops_its_failure_reads_as_working(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_HARNESS_EFFECT_MIN_RUNS", "2")
    real = _failing(lyst, monkeypatch, "validate", ValueError("cannot compare 'x' with 3"))
    for _ in range(2):
        ask(lyst, owner, Q2)
    [rule] = lyst.harness.rules(owner, "northwind")
    _approve_rule(lyst, owner, rule["id"])
    monkeypatch.setattr(lyst.services.registry, "run", real)  # the cause is gone
    for _ in range(2):
        ask(lyst, owner, Q2)
    eff = lyst.harness.rules(owner, "northwind", status="active")[0]["effect"]
    assert eff["verdict"] == "working" and eff["rate_after"] == 0.0


def test_a_proposal_whose_approval_was_never_requested_is_asked_on_the_next_failure(seeded, monkeypatch):
    """Fault injection: the rule row was written, then the process died before the approval was requested."""
    lyst, owner = seeded
    _failing(lyst, monkeypatch, "validate", ValueError("cannot compare 'x' with 3"))
    for _ in range(2):
        ask(lyst, owner, Q2)
    [rule] = lyst.harness.rules(owner, "northwind")
    ctl = lyst.stores.control
    ctl.execute("DELETE FROM approvals WHERE id = ?", [rule["approval_id"]])
    ctl.execute("UPDATE harness_rules SET approval_id = NULL WHERE id = ?", [rule["id"]])
    ask(lyst, owner, Q2)
    [again] = lyst.harness.rules(owner, "northwind")  # still one rule: never proposed twice
    assert again["id"] == rule["id"] and again["approval_id"] and again["approval_id"] != rule["approval_id"]
    _approve_rule(lyst, owner, rule["id"])
    assert lyst.harness.rules(owner, "northwind")[0]["status"] == "active"


def test_the_learning_report_over_http(seeded):
    lyst, owner = seeded
    aid = _analysis(ask(lyst, owner, Q2))
    lyst.feedback.give(owner, "northwind", aid, "incorrect")
    token = lyst.guard.issue_token(owner, "t")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.get("/v1/workspaces/northwind/learning", headers={"Authorization": f"Bearer {token}"}).json()
    assert r["answers"]["verdicts"]["incorrect"] == 1 and r["answers"]["analyses"]["disputed"] == 1
    assert r["workflows"]["held_back"] == {"disputed by a person": 1}
    assert r["memory"]["held_back_disputed"] == 1 and r["harness"]["in_force"] == 0


# -- the evaluation gate measures learning (phase 3) -----------------------------------------------------------------


def test_the_learning_scenarios_pass_and_catch_a_broken_loop(seeded, monkeypatch):
    from lyst import evals

    lyst, owner = seeded
    rows = evals.learning_rows(lyst, owner, "northwind")
    assert [r["case"] for r in rows] == ["learning-reuse", "learning-dispute", "learning-withdraw"]
    assert all(r["passed"] for r in rows), [c for r in rows for c in r["checks"] if not c["passed"]]
    assert evals.dimensions(rows)["learning"]["passed"] == evals.dimensions(rows)["learning"]["checks"]


def test_the_gate_fails_when_a_dispute_stops_holding_anything_back(lyst, monkeypatch):
    """Fault injection: a verdict is recorded but its effects never apply. The gate must see it."""
    from lyst import demo, evals

    owner = lyst.guard.create_user("avery@northwind.test", "Avery")
    demo.seed(lyst, owner)
    monkeypatch.setattr(lyst.feedback, "reconcile", lambda *a, **k: {"status": "open", "workflows": [], "changed": []})
    rows = {r["case"]: r for r in evals.learning_rows(lyst, owner, "northwind")}
    failed = {c["name"] for c in rows["learning-dispute"]["checks"] if not c["passed"]}
    assert {"not_reused", "not_recalled", "not_cited", "not_recompiled"} <= failed
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    base = json.loads((Path(__file__).parents[1] / "docs/quality/eval-baseline.json").read_text())
    cmp_ = evals.compare({"results": list(rows.values())}, base)
    assert not cmp_["ok"] and any(r["case"] == "learning-dispute" for r in cmp_["regressions"])


# -- memory governance and scale (phase 4) ---------------------------------------------------------------------------


def _member(lyst, owner, email, role):
    u = lyst.guard.create_user(email, email.split("@")[0])
    lyst.guard.add_member(owner, "northwind", u.user_id, role)
    return u


def test_only_an_admin_takes_back_approved_memory_and_private_memory_stays_its_owners(seeded):
    lyst, owner = seeded
    mem = lyst.memory
    editor = _member(lyst, owner, "ed@northwind.test", "editor")
    approved = mem.remember(owner, "northwind", "organization", "Fiscal year starts in April", provenance={},
                            trust="approved")
    with pytest.raises(PermissionError):
        mem.revoke(editor, "northwind", approved)
    private = mem.remember(owner, "northwind", "user", "I prefer charts in EUR", provenance={})
    with pytest.raises(PermissionError):
        mem.revoke(editor, "northwind", private)
    with pytest.raises(PermissionError):  # nor can an admin publish someone else's private note
        mem.promote(_member(lyst, owner, "ad@northwind.test", "admin"), "northwind", private)
    team = mem.remember(editor, "northwind", "team", "Region names follow the 2026 reorganisation", provenance={})
    mem.revoke(editor, "northwind", team)  # what an editor may write, an editor may take back
    mem.revoke(owner, "northwind", approved)
    with pytest.raises(KeyError):  # a revoked or unknown memory is not silently "promoted"
        mem.promote(owner, "northwind", approved)
    with pytest.raises(KeyError):
        mem.revoke(owner, "northwind", "m_missing")


def test_list_hides_expired_memory_like_recall_does(seeded):
    lyst, owner = seeded
    mid = lyst.memory.remember(owner, "northwind", "team", "Temporary freeze on hiring", provenance={})
    lyst.stores.workspace("northwind").execute("UPDATE memory SET expires_at = now() - INTERVAL 1 DAY WHERE id = ?", [mid])
    assert mid not in {m["id"] for m in lyst.memory.list(owner, "northwind")}
    assert mid not in {m["id"] for m in lyst.memory.recall(owner, "northwind", "freeze on hiring")}


def test_recall_scores_a_bounded_set_with_approved_knowledge_first(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_MEMORY_RECALL_SCAN", "100")
    db = lyst.stores.workspace("northwind")
    db.execute("INSERT INTO memory (id, layer, scope, owner, content, provenance, trust, created_at) "
               "SELECT 'm_bulk' || i, 'episodic', 'northwind', ?, 'Q: freight cost ' || i || ' A: noted', '{}', "
               "'observed', now() - to_seconds(CAST(i AS BIGINT)) FROM range(20000) t(i)", [owner.user_id])
    approved = lyst.memory.remember(owner, "northwind", "organization", "Freight cost is booked under COGS",
                                    provenance={}, trust="approved")
    old = db.scalar("SELECT id FROM memory WHERE id = 'm_bulk19999'")
    import time  # noqa: PLC0415

    start = time.perf_counter()
    hits = lyst.memory.recall(owner, "northwind", "freight cost", limit=5)
    took = time.perf_counter() - start
    assert hits[0]["id"] == approved and old not in {h["id"] for h in hits}
    assert took < 2.0, f"recall over 20,000 memories took {took:.2f}s"


def test_agent_observed_memory_ages_out_after_365_days_by_default(seeded, monkeypatch):
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from lyst.memory import retention_days  # noqa: PLC0415

    monkeypatch.delenv("LYST_RETENTION_DAYS_MEMORY", raising=False)
    assert retention_days() == 365
    monkeypatch.setenv("LYST_RETENTION_DAYS_MEMORY", "0")
    assert retention_days() is None  # an operator can still keep everything
    lyst, owner = seeded
    db = lyst.stores.workspace("northwind")
    old = lyst.memory.remember(owner, "northwind", "episodic", "an old observation", provenance={})
    young = lyst.memory.remember(owner, "northwind", "episodic", "a recent observation", provenance={})
    kept = lyst.memory.remember(owner, "northwind", "episodic", "validated long ago", provenance={},
                                trust="user_validated")
    db.execute("UPDATE memory SET created_at = ? WHERE id IN (?, ?)", [datetime.now(UTC) - timedelta(days=400), old, kept])
    monkeypatch.delenv("LYST_RETENTION_DAYS_MEMORY")
    lyst.stores.prune(memory_days=retention_days())
    left = {r["id"] for r in db.query("SELECT id FROM memory")}
    assert old not in left and young in left and kept in left
