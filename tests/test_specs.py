# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Living project specs (scenario G): a request becomes requirements grounded in approved metrics; requirements
are met by evidence from linked work; a spec cannot complete with a must-have open; changing requirements keeps
valid work, flags affected work, and keeps every version."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.security import AccessDenied
from lyst.specs import SpecIncomplete
from tests.conftest import ask

WS = "northwind"
REQUEST = ("We need to explain why operating margin fell last quarter for the CFO by Friday. "
           "Then build a dashboard of revenue by region. It would be nice to have a forecast of revenue if possible.")


def test_draft_is_grounded_in_the_workspace(seeded):
    lyst, owner = seeded
    d = lyst.specs.draft(owner, WS, REQUEST)
    assert {m["name"] for m in d["metrics"]} >= {"operating_margin", "revenue"} and "pnl" in d["datasets"]
    assert {x["kind"] for x in d["deliverables"]} >= {"dashboard", "forecast"}
    assert "CFO" in d["stakeholders"] and any("Friday" in c for c in d["constraints"])
    prios = [r["priority"] for r in d["requirements"]]
    assert prios[0] == "must" and prios[-1] == "could"
    assert any("Operating margin is computed" in a for a in d["requirements"][0]["acceptance"])
    with pytest.raises(ValueError):
        lyst.specs.draft(owner, WS, "hi")


def test_scenario_g_evolving_requirements_keep_valid_work_and_traceability(seeded):
    lyst, owner = seeded
    body = lyst.specs.draft(owner, WS, REQUEST)
    spec = lyst.specs.create(owner, WS, "Margin review", body)
    sid = spec["id"]
    assert spec["progress"] == {"met": 0, "total": 3, "must_open": 1} and not spec["can_complete"]

    t1 = ask(lyst, owner, "Why did operating margin change last quarter?")
    spec = lyst.specs.link(owner, WS, sid, "R1", "task", t1["id"])
    r1 = spec["requirements"][0]
    assert r1["status"] == "met" and "finished verified" in r1["why"]

    # R2 (a should) is still open, but only must-haves gate completion; first prove the gate with a new must
    spec = lyst.specs.revise(owner, WS, sid, {"requirements": [
        {**spec["body"]["requirements"][0]},
        {"id": "R2", "text": "Build a dashboard of revenue by region and product line", "priority": "must"},
        {"id": "R4", "text": "Share the result with the board", "priority": "should"},
    ]}, note="CFO wants product line too; forecast dropped")
    ch = spec["change"]
    assert ch["affected"] == ["R2"] and ch["added"] == ["R4"] and ch["dropped"] == ["R3"]
    reqs = {q["id"]: q for q in spec["requirements"]}
    assert reqs["R1"]["status"] == "met"  # unchanged requirement keeps its evidence
    assert reqs["R2"]["status"] == "recheck" and reqs["R2"]["previous_text"].startswith("Then build a dashboard")
    assert spec["body"]["dropped"][0]["id"] == "R3"

    with pytest.raises(SpecIncomplete) as e:
        lyst.specs.complete(owner, WS, sid)
    assert [q["id"] for q in e.value.unmet] == ["R2"]

    t2 = ask(lyst, owner, "What was revenue by region last quarter?")
    art = lyst.artifacts.build(owner, t2, "md")
    lyst.specs.link(owner, WS, sid, "R2", "artifact", art["id"])
    done = lyst.specs.complete(owner, WS, sid, note="delivered")
    assert done["status"] == "complete" and done["progress"]["must_open"] == 0
    hist = lyst.specs.history(owner, WS, sid)
    assert [h["version"] for h in hist][:1] == [len(hist)] and any("forecast dropped" in (h["note"] or "") for h in hist)
    with pytest.raises(ValueError):
        lyst.specs.revise(owner, WS, sid, {"objective": "x"}, note="too late")


def test_marking_and_waiving_need_notes_and_roles(seeded):
    lyst, owner = seeded
    editor = lyst.guard.create_user("ed@northwind.test", "Ed")
    lyst.guard.add_member(owner, WS, editor.user_id, "editor")
    viewer = lyst.guard.create_user("vi@northwind.test", "Vi")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    sid = lyst.specs.create(editor, WS, "Quick", {"requirements": [{"text": "Confirm revenue for 2024", "priority": "must"}]})["id"]
    with pytest.raises(ValueError):
        lyst.specs.mark(editor, WS, sid, "R1", status="met", note="")
    with pytest.raises(AccessDenied):
        lyst.specs.mark(editor, WS, sid, "R1", status="waived", note="not needed")
    with pytest.raises(AccessDenied):
        lyst.specs.link(viewer, WS, sid, "R1", "task", "t_nope")
    assert lyst.specs.mark(editor, WS, sid, "R1", status="met", note="checked against the ledger")["can_complete"]
    assert lyst.specs.mark(owner, WS, sid, "R1", status="waived", note="dropped by finance")["requirements"][0]["status"] == "waived"
    with pytest.raises(KeyError):
        lyst.specs.link(editor, WS, sid, "R1", "task", "t_missing")
    assert lyst.specs.list(viewer, WS)[0]["progress"]["met"] == 1


def test_specs_api(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        d = c.post(f"/v1/workspaces/{WS}/specs/draft", json={"request": REQUEST}, headers=h).json()
        s = c.post(f"/v1/workspaces/{WS}/specs", json={"title": "Margin review", "body": d}, headers=h).json()
        r = c.post(f"/v1/workspaces/{WS}/specs/{s['id']}/complete", headers=h)
        assert r.status_code == 409 and r.json()["unmet"][0]["id"] == "R1"
        assert c.get(f"/v1/workspaces/{WS}/specs", headers=h).json()[0]["id"] == s["id"]
        assert c.get(f"/v1/workspaces/{WS}/specs/{s['id']}", params={"history": True}, headers=h).json()[0]["version"] == 1
