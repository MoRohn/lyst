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
