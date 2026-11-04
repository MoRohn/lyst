# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Capability-gap forecasting. Acceptance scenario G: an upcoming approved assignment needs a method Lyst lacks; Lyst
finds it before the run, reports the installed open-source options and a bounded proposal, tests a sandboxed candidate,
and never promotes it without an owner's approval."""

from datetime import UTC, date, datetime, timedelta

WS = "northwind"


def _schedule(lyst, owner, question, due_in=timedelta(days=2)):
    s = lyst.runtime.create_schedule(owner, WS, "Concentration review", question, 2592000)
    lyst.runtime.decide(owner, s["approval_id"], True)
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", [datetime.now(UTC) + due_in, s["schedule_id"]])
    return s["schedule_id"]


def test_g_upcoming_method_gap_is_prepared_and_tested_but_not_promoted(seeded):
    lyst, owner = seeded
    sid = _schedule(lyst, owner, "Compute the Herfindahl index of revenue by region for the latest quarter")
    lyst.anticipation.set_settings(owner, WS, {"level": 3})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="capability_gap")
    assert a["subject_id"] == sid and "herfindahl" in a["reasons"][0].lower()
    full = lyst.anticipation.get(owner, WS, a["id"])
    prop = full["prepared"]["proposal"]
    assert prop["skill"] == "hhi_concentration" and prop["metric"] == "revenue" and "region" in prop["input_sql"]
    assert any(x["library"] == "numpy" and x["installed"] for x in prop["libraries"])      # checked locally, not fetched
    cand = prop["candidate"]
    assert cand["status"] == "tested" and cand["representative"] and cand["adversarial"] is not None
    skill = lyst.skills.get(owner, WS, "hhi_concentration")
    assert skill["status"] == "tested"                                                     # sandboxed, not approved
    assert lyst.skills.match(owner, WS, "herfindahl") is None
    assert not [x for x in lyst.runtime.approvals(owner, WS) if x["action"] == "skill.register"]   # not even requested yet
    r = lyst.anticipation.act(owner, WS, a["id"], "request_registration")
    assert r["needs_approval"]
    [ap] = [x for x in lyst.runtime.approvals(owner, WS) if x["action"] == "skill.register"]
    import pytest

    from lyst.security import AccessDenied

    with pytest.raises(AccessDenied):
        lyst.runtime.decide(owner, ap["id"], True)                                          # the proposer cannot approve
    other = lyst.guard.create_user("own2@northwind.test", "Second owner")
    lyst.guard.add_member(owner, WS, other.user_id, "owner")
    lyst.runtime.decide(other, ap["id"], True)
    assert lyst.skills.match(owner, WS, "herfindahl")["name"] == "hhi_concentration"
    lyst.anticipation.cycle(WS)
    assert lyst.anticipation.list(owner, WS, kind="capability_gap") == []                   # the gap closed itself


def test_g_at_prepare_level_only_a_proposal_no_sandbox_run(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Compute the Gini coefficient of revenue across regions")
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="capability_gap")
    prop = lyst.anticipation.get(owner, WS, a["id"])["prepared"]["proposal"]
    assert "candidate" not in prop and a["verdict"] == "proposal" and [x["id"] for x in a["actions"]] == ["dismiss", "snooze"]
    assert lyst.skills.list(owner, WS) == [] or all(s["name"] != "gini_inequality" for s in lyst.skills.list(owner, WS))


def test_g_method_without_a_template_gets_contract_and_local_options(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Calculate a Theil index of revenue across regions")
    lyst.anticipation.set_settings(owner, WS, {"level": 3})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="capability_gap")
    full = lyst.anticipation.get(owner, WS, a["id"])
    prop = full["prepared"]["proposal"]
    assert prop["contract"].startswith("run(df[member, value]") and "candidate" not in prop
    assert {x["library"] for x in prop["libraries"]} >= {"numpy", "scipy"}
    assert {c["check"]: c["status"] for c in full["verification"]["checks"]}["template"] == "warn"


def test_g_assignment_due_soon_and_repeated_limitations(seeded):
    lyst, owner = seeded
    peer = lyst.guard.create_user("pat@northwind.test", "Pat")
    lyst.guard.add_member(owner, WS, peer.user_id, "editor")
    t = lyst.runtime.submit(owner, WS, "Why did revenue change in Q3 2026?")
    lyst.runtime.drain()
    lyst.collab.assign(owner, WS, "task", t, peer.user_id, note="Compute the Pareto share of revenue across regions",
                       due=(date.today() + timedelta(days=3)).isoformat())
    lyst.anticipation.cycle(WS)
    gaps = lyst.anticipation.list(peer, WS, kind="capability_gap")
    assert gaps and gaps[0]["subject_kind"] == "assignment" and "assignment due" in gaps[0]["reasons"][0]
    for _ in range(2):
        lyst.runtime.submit(owner, WS, "Calculate a Theil index of revenue across regions")
    lyst.runtime.drain()
    lyst.anticipation.cycle(WS)
    rep = [g for g in lyst.anticipation.list(owner, WS, kind="capability_gap") if g["subject_kind"] == "method"]
    assert rep and "2 times in 30 days" in rep[0]["reasons"][0]
