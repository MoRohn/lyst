# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Approval summaries and the "where things stand" view (scenario F, the parts this layer owns): a pending decision
says what it would do, to what, how risky it is and how to undo it; a returning person sees what they asked for,
what finished, what runs, what needs them and what happens next."""

from __future__ import annotations

from fastapi.testclient import TestClient

from lyst.api import create_app
from tests.conftest import ask

WS = "northwind"


def test_approvals_explain_action_risk_effect_and_reversal(seeded):
    lyst, owner = seeded
    editor = lyst.guard.create_user("ed@northwind.test", "Ed")
    lyst.guard.add_member(owner, WS, editor.user_id, "editor")
    lyst.runtime.create_schedule(editor, WS, "Weekly revenue", "What was revenue last month?", 7 * 86400)
    lyst.monitors.create(editor, WS, metric="revenue")
    spec = lyst.semantic.get(owner, WS, "revenue").model_dump()
    lyst.collab.propose(editor, WS, "metric.define", object_kind="metric", object_id="revenue",
                        payload={**spec, "description": "Net of returns"}, summary="Revenue should be net of returns")
    items = lyst.standing.approvals(owner, WS)
    by = {i["action"]: i for i in items}
    assert {"schedule.activate", "monitor.activate", "metric.define"} <= set(by)
    assert by["metric.define"]["risk"] == "high" and "previous version" in by["metric.define"]["reversal"]
    assert by["monitor.activate"]["risk"] == "low"
    sched = by["schedule.activate"]
    assert sched["risk"] == "medium" and "every week" in sched["affected"][0] and "What was revenue last month?" in sched["expected"]
    assert all(i["can_decide"] for i in items) and items[0]["risk"] == "high"
    assert not any(i["can_decide"] for i in lyst.standing.approvals(editor, WS) if i["action"] == "metric.define")


def test_standing_summarizes_for_a_returning_person(seeded):
    lyst, owner = seeded
    ask(lyst, owner, "What was revenue in 2024?")
    lyst.runtime.submit(owner, WS, "What was operating margin last quarter?")  # queued, not drained
    lyst.runtime.create_schedule(owner, WS, "Monthly margin", "What was operating margin last month?", 30 * 86400)
    s = lyst.standing.standing(owner, WS)
    assert s["completed"] and s["running"] and len(s["requested"]) == 2
    assert any(a["kind"] == "approvals" for a in s["attention"])
    assert "running" in s["headline"] and "attention" in s["headline"]
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get(f"/v1/workspaces/{WS}/standing", headers=h).json()["headline"]
        assert c.get(f"/v1/workspaces/{WS}/approvals/explained", headers=h).json()[0]["reversal"]
