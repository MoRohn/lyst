# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Collaboration: comments, change requests, decisions, assignments, reviews that gate shared definitions, sharing,
ownership and personal notifications. Acceptance scenario E (shared team investigation) runs here."""

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.contracts import ContractSpec
from lyst.security import AccessDenied

from .conftest import ask

WS = "northwind"


@pytest.fixture
def team(seeded):
    lyst, owner = seeded
    analyst = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, WS, analyst.user_id, "editor")
    viewer = lyst.guard.create_user("vic@northwind.test", "Vic")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    return lyst, owner, analyst, viewer


def kinds(lyst, who):
    return [n["kind"] for n in lyst.collab.notifications(who, WS)]


def test_e_two_people_collaborate_on_an_investigation(team):
    lyst, owner, analyst, viewer = team
    # Avery investigates and hands the report to Sam for review
    task = ask(lyst, owner, "What was operating margin last quarter?")
    report = lyst.artifacts.build(owner, task, "pdf")
    aid = report["asset_id"]
    lyst.collab.share(owner, WS, aid, visibility="private", users=[analyst.user_id])
    assert aid not in {a["id"] for a in lyst.assets.list(viewer, WS)}  # private: Vic does not see it
    assert aid in {a["id"] for a in lyst.assets.list(analyst, WS)}     # shared with Sam
    asg = lyst.collab.assign(owner, WS, "asset", aid, analyst.user_id, note="Check the EMEA driver before Friday", due="2026-11-06")
    assert "assignment" in kinds(lyst, analyst)
    # Sam asks for a change and mentions Avery; Avery owns the report, so one notification
    cr = lyst.collab.comment(analyst, WS, "asset", aid, "Please split EMEA by product line, @avery@northwind.test", kind="change_request")
    assert cr["status"] == "open" and cr["mentions"] == [owner.user_id]
    assert kinds(lyst, owner).count("mention") == 1 and "comment_on_owned" not in kinds(lyst, owner)
    with pytest.raises(KeyError):
        lyst.collab.comments(viewer, WS, "asset", aid)  # the thread is as private as the asset
    # Avery resolves it with a reply, records a decision, Sam completes the assignment
    lyst.collab.resolve(owner, WS, cr["id"], note="Done in the appendix.")
    lyst.collab.comment(owner, WS, "asset", aid, "We report EMEA margin with the product-line split from now on.", kind="decision")
    lyst.collab.complete(analyst, WS, asg["id"], note="Driver confirmed: hardware COGS.")
    assert "assignment_done" in kinds(lyst, owner) and "review_decided" in kinds(lyst, analyst)
    act = lyst.collab.activity(owner, WS, "asset", aid)
    assert [c["kind"] for c in act["comments"]] == ["change_request", "comment", "decision", "comment"]
    assert act["open_change_requests"] == 0 and act["assignments"][0]["status"] == "done"
    assert lyst.collab.decisions(owner, WS)[0]["body"].startswith("We report EMEA margin")
    assert lyst.collab.decisions(viewer, WS) == []  # Vic cannot see the asset, so not its decisions
    # ownership moves to Sam; Avery keeps nothing she must not
    out = lyst.collab.transfer(owner, WS, "asset", aid, analyst.user_id, note="Yours from here")
    assert out["owner"] == analyst.user_id and lyst.assets.get(analyst, WS, aid)["owner"] == analyst.user_id
    with pytest.raises(ValueError):
        lyst.collab.transfer(analyst, WS, "asset", aid, viewer.user_id)  # a viewer cannot own an asset
    # the investigation task itself is discussable by everyone in the workspace
    lyst.collab.comment(viewer, WS, "task", task["id"], "Thanks, this answers the board's question.")
    audit = {r["action"] for r in lyst.stores.control.query("SELECT action FROM audit_log WHERE workspace_id = ?", [WS])}
    assert {"assignment.create", "assignment.complete", "ownership.transfer", "comment.decision", "comment.resolve", "asset.share"} <= audit


def test_metric_and_contract_changes_need_an_admin_who_is_not_the_proposer(team):
    lyst, owner, analyst, viewer = team
    spec = lyst.semantic.get(owner, WS, "revenue").model_dump()
    spec["description"] = "Booked revenue net of credits."
    with pytest.raises(AccessDenied):
        lyst.semantic.define(analyst, WS, lyst.semantic.get(owner, WS, "revenue").model_copy(update={"description": "x"}))
    rv = lyst.collab.propose(analyst, WS, "metric.define", object_kind="metric", object_id="revenue", payload=spec,
                             summary="Revenue: net of credits")
    assert "review_requested" in kinds(lyst, owner)
    assert lyst.semantic.history(owner, WS, "revenue")[0]["version"] == 1  # nothing applied yet
    with pytest.raises(AccessDenied):
        lyst.collab.decide(analyst, WS, rv["id"], approve=True)  # proposer cannot approve
    with pytest.raises(AccessDenied):
        lyst.collab.decide(viewer, WS, rv["id"], approve=True)   # below admin
    assert [r["id"] for r in lyst.collab.reviews(owner, WS, for_me=True)] == [rv["id"]]
    out = lyst.collab.decide(owner, WS, rv["id"], approve=True, note="Matches the finance policy")
    assert out["applied"] == {"metric": "revenue", "version": 2}
    hist = lyst.semantic.history(owner, WS, "revenue")[0]
    assert hist["created_by"] == owner.user_id and rv["id"] in hist["note"]
    assert "review_decided" in kinds(lyst, analyst)
    with pytest.raises(ValueError):
        lyst.collab.decide(owner, WS, rv["id"], approve=True)  # decided once
    # a data contract proposed by the editor, changes requested, nothing applied
    draft = lyst.contracts.draft(analyst, WS, "pnl")
    rc = lyst.collab.propose(analyst, WS, "contract.define", object_kind="dataset", object_id="pnl", payload=draft, summary="Contract for pnl")
    assert lyst.collab.decide(owner, WS, rc["id"], approve=False, note="Add freshness first")["status"] == "changes_requested"
    assert lyst.contracts.get(owner, WS, "pnl") is None
    rc2 = lyst.collab.propose(analyst, WS, "contract.define", object_kind="dataset", object_id="pnl",
                              payload=ContractSpec.model_validate(draft).model_dump(), summary="Contract for pnl, v2")
    assert lyst.collab.decide(owner, WS, rc2["id"], approve=True)["applied"]["check"] == "pass"
    with pytest.raises(ValueError):
        lyst.collab.propose(analyst, WS, "metric.define", object_kind="metric", object_id="x", payload={"name": "x"}, summary="bad")


def test_publishing_a_private_asset_by_review_and_a_plain_request_to_look(team):
    lyst, owner, analyst, viewer = team
    task = ask(lyst, analyst, "What was operating margin last quarter?")
    aid = lyst.artifacts.build(analyst, task, "md")["asset_id"]
    lyst.collab.share(analyst, WS, aid, visibility="private")
    rv = lyst.collab.propose(analyst, WS, "asset.publish", object_kind="asset", object_id=aid, payload={}, summary="Publish the margin briefing")
    lyst.collab.decide(owner, WS, rv["id"], approve=True)
    assert aid in {a["id"] for a in lyst.assets.list(viewer, WS)}
    look = lyst.collab.propose(analyst, WS, "look", object_kind="asset", object_id=aid, payload={}, summary="Sanity-check the numbers",
                               reviewer=viewer.user_id)
    with pytest.raises(AccessDenied):
        lyst.collab.decide(owner, WS, look["id"], approve=True)  # asked of Vic
    assert lyst.collab.decide(viewer, WS, look["id"], approve=True)["status"] == "approved"


def test_notification_preferences_apply_to_people_and_platform_notices(team):
    lyst, owner, analyst, viewer = team
    lyst.collab.set_preferences(owner, WS, {"mention": False, "contract_breaking": False})
    assert lyst.collab.preferences(owner, WS)["mention"] is False
    lyst.collab.comment(analyst, WS, "dataset", "pnl", "Is this the audited P&L, @avery@northwind.test?")
    assert "mention" not in kinds(lyst, owner) and "comment_on_owned" not in kinds(lyst, owner)
    lyst.contracts.define(owner, WS, "pnl", ContractSpec.model_validate(lyst.contracts.draft(owner, WS, "pnl")))
    from lyst import demo
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(demo.pnl().drop(columns=["opex"])), filename="pnl.csv")
    assert "contract_breaking" not in kinds(lyst, owner)
    with pytest.raises(ValueError):
        lyst.collab.set_preferences(owner, WS, {"nonsense": True})
    n = lyst.collab.notifications(analyst, WS)
    assert n == [] or all(x["status"] == "unread" for x in n)


def test_collaboration_routes(team):
    lyst, owner, analyst, viewer = team
    task = ask(lyst, owner, "What was operating margin last quarter?")
    aid = lyst.artifacts.build(owner, task, "pdf")["asset_id"]
    ho = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    ha = {"Authorization": f"Bearer {lyst.guard.issue_token(analyst)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post(f"/v1/workspaces/{WS}/objects/asset/{aid}/comments", json={"body": "Looks right", "kind": "comment"}, headers=ha)
        assert r.status_code == 200
        assert c.get(f"/v1/workspaces/{WS}/objects/asset/{aid}/activity", headers=ho).json()["comments"][0]["body"] == "Looks right"
        a = c.post(f"/v1/workspaces/{WS}/assignments", json={"object_kind": "task", "object_id": task["id"], "assignee": analyst.user_id},
                   headers=ho).json()
        assert c.get(f"/v1/workspaces/{WS}/assignments", params={"mine": True}, headers=ha).json()[0]["id"] == a["id"]
        assert c.post(f"/v1/workspaces/{WS}/assignments/{a['id']}/complete", json={}, headers=ha).json()["status"] == "done"
        notes = c.get(f"/v1/workspaces/{WS}/notifications", headers=ho).json()
        assert {"comment_on_owned", "assignment_done"} <= {n["kind"] for n in notes}
        assert c.post(f"/v1/workspaces/{WS}/notifications/{notes[0]['id']}/read", headers=ho).json() == {"ok": True}
        assert c.put(f"/v1/workspaces/{WS}/notification-preferences", json={"assignment": False}, headers=ha).json()["assignment"] is False
        spec = lyst.semantic.get(owner, WS, "tickets").model_dump()
        rv = c.post(f"/v1/workspaces/{WS}/reviews", json={"action": "metric.define", "object_kind": "metric", "object_id": "tickets",
                                                          "payload": spec, "summary": "Tickets: unchanged"}, headers=ha).json()
        assert c.post(f"/v1/workspaces/{WS}/reviews/{rv['id']}/decide", json={"approve": True}, headers=ha).status_code == 403
        assert c.post(f"/v1/workspaces/{WS}/reviews/{rv['id']}/decide", json={"approve": True}, headers=ho).json()["status"] == "approved"
        assert c.post(f"/v1/workspaces/{WS}/assets/{aid}/share", json={"visibility": "private"}, headers=ha).status_code == 403
        assert c.post(f"/v1/workspaces/{WS}/ownership", json={"object_kind": "metric", "object_id": "tickets", "to_user": analyst.user_id},
                      headers=ho).json()["owner"] == analyst.user_id
        assert c.get(f"/v1/workspaces/{WS}/objects/widget/x/comments", headers=ho).status_code == 400
