# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The embedded autonomy layer's backend: object actions (offered and enforced), canonical lifecycle states tied to real
events, work started from anywhere landing in Work and as an artifact, and anticipative maintenance gated on a person."""

from fastapi.testclient import TestClient

from lyst import demo
from lyst.ambient import STATES, lifecycle, timeline
from lyst.api import create_app

from .conftest import ask


def ids(actions):
    return [a["id"] for a in actions]


def test_actions_follow_role_object_and_state(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    amb = lyst.ambient
    owner_ds = ids(amb.actions(owner, "northwind", "dataset", "pnl"))
    viewer_ds = ids(amb.actions(viewer, "northwind", "dataset", "pnl"))
    assert "dataset.contract" in owner_ds and "dataset.contract" not in viewer_ds  # admins only
    assert "dataset.profile" in viewer_ds
    m = amb.actions(owner, "northwind", "metric", "operating_margin")
    assert {"metric.explain", "metric.forecast", "metric.what_if", "metric.lineage"} <= set(ids(m))
    assert all(set(a) >= {"id", "label", "kind", "needs_approval", "reason"} and a["kind"] in ("navigate", "ask", "run", "approve") for a in m)
    t = ask(lyst, owner, "What was revenue last quarter?")
    ta = ids(amb.actions(owner, "northwind", "task", t["id"]))
    assert {"task.breakdown.region", "task.compare_year", "task.why", "task.schedule", "task.briefing"} <= set(ta)
    assert "task.retry" not in ta  # only for failed tasks
    sched = next(a for a in amb.actions(owner, "northwind", "task", t["id"]) if a["id"] == "task.schedule")
    assert sched["kind"] == "approve" and sched["needs_approval"]


def test_execution_is_checked_again_on_the_server(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    import pytest

    from lyst.ambient import ActionRefused

    with pytest.raises(ActionRefused):
        lyst.ambient.execute(viewer, "northwind", "dataset", "pnl", "dataset.contract")
    t = ask(lyst, owner, "What was revenue last quarter?")
    with pytest.raises(ActionRefused):
        lyst.ambient.execute(owner, "northwind", "task", t["id"], "task.retry")  # not failed: not offered, not run
    audited = lyst.stores.control.scalar("SELECT count(*) FROM audit_log WHERE action = 'action.refused'")
    assert audited == 2


def test_work_started_from_a_dataset_shows_in_work_with_lifecycle_and_is_kept_as_an_artifact(seeded):
    lyst, owner = seeded
    out = lyst.ambient.execute(owner, "northwind", "metric", "revenue", "metric.value")
    assert out["lifecycle"]["state"] == "Preparing"
    listed = {t["id"]: t for t in lyst.runtime.list(owner, "northwind")}
    assert listed[out["task_id"]]["lifecycle"]["state"] == "Preparing"
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, out["task_id"])
    assert t["request"]["initiated_from"]["object"] == {"kind": "metric", "id": "revenue"}
    assert t["lifecycle"]["state"] == "Completed"
    arts = lyst.stores.control.query("SELECT kind FROM artifacts WHERE task_id = ?", [out["task_id"]])
    assert [a["kind"] for a in arts] == ["md"]
    assert any(a["task_id"] == out["task_id"] for a in lyst.assets.list(owner, "northwind"))
    # a drill-down from the task is also recorded
    fu = lyst.ambient.execute(owner, "northwind", "task", out["task_id"], "task.breakdown.region")
    lyst.runtime.drain()
    child = lyst.runtime.get(owner, fu["task_id"])
    assert child["request"]["derived_from"]["task_id"] == out["task_id"] and child["request"]["initiated_from"]["action"] == "task.breakdown.region"


def test_lifecycle_states_come_from_recorded_state():
    assert lifecycle({"status": "queued"}, [])["state"] == "Preparing"
    assert lifecycle({"status": "running"}, [{"status": "running", "phase": "understand", "capability": "understand"}])["state"] == "Preparing"
    assert lifecycle({"status": "running"}, [{"status": "running", "phase": "execute", "capability": "metric_value"}])["state"] == "Investigating"
    assert lifecycle({"status": "running"}, [{"status": "running", "phase": "execute", "capability": "build_dashboard"}])["state"] == "Executing"
    assert lifecycle({"status": "running"}, [{"status": "running", "phase": "validate", "capability": "validate"}])["state"] == "Verifying"
    assert lifecycle({"status": "awaiting_approval"}, [])["state"] == "Awaiting Approval"
    assert lifecycle({"status": "done", "outcome": "complete"}, [])["state"] == "Completed"
    for bad in ({"status": "done", "outcome": "needs_input"}, {"status": "done", "outcome": "unverified"}, {"status": "failed", "error": "x"},
                {"status": "paused"}):
        assert lifecycle(bad, [])["state"] == "Needs Attention"
    assert lifecycle({"status": "running"}, [{"status": "running", "phase": "execute", "capability": "metric_value"}],
                     [{"kind": "stalled"}])["state"] == "Needs Attention"
    assert set(STATES) == {"Preparing", "Investigating", "Executing", "Verifying", "Awaiting Approval", "Completed", "Needs Attention"}


def test_timeline_ties_each_state_to_an_event(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "What was revenue last quarter?")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.get(f"/v1/tasks/{t['id']}/lifecycle", headers=h).json()
    states = [x["state"] for x in r["timeline"]]
    assert states[0] == "Preparing" and "Investigating" in states and "Verifying" in states and states[-1] == "Completed"
    assert all(x["event"] for x in r["timeline"]) and r["lifecycle"]["state"] == "Completed"
    assert timeline([], []) == []


def test_schema_change_offers_a_repair_that_waits_for_a_person(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "What was revenue last quarter?")
    lyst.ambient.execute(owner, "northwind", "task", t["id"], "task.briefing")
    asset = next(a for a in lyst.assets.list(owner, "northwind") if a["task_id"] == t["id"])
    assert asset["health"] == "current"
    lyst.data.ingest(owner, "northwind", "pnl", demo.csv_bytes(demo.pnl().iloc[:-4]), filename="pnl.csv")  # new data
    a = lyst.assets.get(owner, "northwind", asset["id"])
    assert a["health"] in ("stale", "invalidated")
    acts = {x["id"]: x for x in lyst.ambient.actions(owner, "northwind", "dataset", "pnl")}
    assert acts["dataset.repair_affected"]["kind"] == "approve" and acts["dataset.repair_affected"]["needs_approval"]
    assert "asset.repair" in ids(lyst.ambient.actions(owner, "northwind", "asset", asset["id"]))
    # nothing was repaired on its own: the asset is still out of date until a person takes the action
    lyst.runtime.drain()
    assert lyst.assets.get(owner, "northwind", asset["id"])["health"] in ("stale", "invalidated")
    out = lyst.ambient.execute(owner, "northwind", "asset", asset["id"], "asset.repair")
    assert out["status"] == "repairing"
    lyst.runtime.drain()
    assert lyst.assets.get(owner, "northwind", asset["id"])["health"] == "current"


def test_object_action_endpoints(seeded):
    lyst, owner = seeded
    other = lyst.guard.create_user("x@other.test", "X")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    hx = {"Authorization": f"Bearer {lyst.guard.issue_token(other)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.get("/v1/workspaces/northwind/objects/metric/revenue/actions", headers=h)
        assert r.status_code == 200 and "metric.explain" in ids(r.json())
        assert c.get("/v1/workspaces/northwind/objects/metric/revenue/actions", headers=hx).status_code == 403
        assert c.get("/v1/workspaces/northwind/objects/rocket/x/actions", headers=h).status_code == 404
        r = c.post("/v1/workspaces/northwind/objects/metric/revenue/actions/metric.explain", headers=h,
                   json={"page": "brain", "objective": "understand the change"})
        assert r.status_code == 200 and r.json()["task_id"]
        nav = c.post("/v1/workspaces/northwind/objects/metric/revenue/actions/metric.lineage", headers=h, json={}).json()
        assert nav["navigate"] == "/brain?metric=revenue"
        assert c.post("/v1/workspaces/northwind/objects/metric/revenue/actions/metric.delete", headers=h, json={}).status_code == 403
