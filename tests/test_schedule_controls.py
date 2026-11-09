# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A schedule can be stopped as easily as it was started: pausing ends new runs at once, withdraws an approval still
waiting to start it, and resuming asks for approval again and picks up from the next occurrence, not the missed ones."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.security import AccessDenied

WS = "northwind"


def _admin(lyst, owner):
    admin = lyst.guard.create_user("adm@northwind.test", "Adm")
    lyst.guard.add_member(owner, WS, admin.user_id, "admin")
    return admin


def _row(lyst, sid):
    return lyst.runtime.db.query("SELECT * FROM schedules WHERE id = ?", [sid])[0]


def test_pause_stops_new_runs_and_resume_needs_approval_again(seeded):
    lyst, owner = seeded
    admin = _admin(lyst, owner)
    s = lyst.runtime.create_schedule(owner, WS, "KPI brief", "Give me a weekly KPI brief", 3600)
    lyst.runtime.decide(admin, s["approval_id"], True)
    lyst.runtime.db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                            [datetime.now(UTC) - timedelta(minutes=1), s["schedule_id"]])
    out = lyst.runtime.pause_schedule(owner, WS, s["schedule_id"])
    assert out["enabled"] is False and not _row(lyst, s["schedule_id"])["enabled"]
    assert lyst.runtime.tick_schedules() == []  # a paused schedule starts nothing, even when due
    audit = lyst.stores.control.query("SELECT outcome FROM audit_log WHERE action = 'schedule.pause' AND target = ?",
                                      [s["schedule_id"]])
    assert audit and audit[0]["outcome"] == "ok"

    res = lyst.runtime.resume_schedule(owner, WS, s["schedule_id"])
    assert res["enabled"] is False and res["approval_id"] != s["approval_id"]
    row = _row(lyst, s["schedule_id"])
    assert not row["enabled"] and row["approval_id"] == res["approval_id"]
    nxt = row["next_run_at"] if row["next_run_at"].tzinfo else row["next_run_at"].replace(tzinfo=UTC)
    assert nxt > datetime.now(UTC)  # it does not catch up on the runs it missed while paused
    pending = [a for a in lyst.runtime.approvals(owner, WS) if a["id"] == res["approval_id"]]
    assert pending and pending[0]["summary"].startswith("Resume 'KPI brief'")
    # asking twice does not open a second approval
    assert lyst.runtime.resume_schedule(owner, WS, s["schedule_id"])["approval_id"] == res["approval_id"]
    lyst.runtime.decide(admin, res["approval_id"], True)
    assert _row(lyst, s["schedule_id"])["enabled"]


def test_pausing_before_approval_withdraws_the_request(seeded):
    lyst, owner = seeded
    admin = _admin(lyst, owner)
    s = lyst.runtime.create_schedule(owner, WS, "Brief", "What changed this month?", 3600)
    lyst.runtime.pause_schedule(owner, WS, s["schedule_id"])
    assert not [a for a in lyst.runtime.approvals(owner, WS) if a["id"] == s["approval_id"]]
    with pytest.raises(ValueError):  # a withdrawn request cannot be approved later to switch it back on
        lyst.runtime.decide(admin, s["approval_id"], True)
    assert not _row(lyst, s["schedule_id"])["enabled"]


def test_only_editors_control_schedules_and_only_in_their_workspace(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    s = lyst.runtime.create_schedule(owner, WS, "Brief", "What changed this month?", 3600)
    with pytest.raises(AccessDenied):
        lyst.runtime.pause_schedule(viewer, WS, s["schedule_id"])
    with pytest.raises(AccessDenied):
        lyst.runtime.resume_schedule(viewer, WS, s["schedule_id"])
    with pytest.raises(KeyError):
        lyst.runtime.pause_schedule(owner, WS, "s_missing")


def test_schedule_controls_over_http(seeded):
    lyst, owner = seeded
    token = lyst.guard.issue_token(owner)
    with TestClient(create_app(lyst, start_workers=False)) as client:
        h = {"Authorization": f"Bearer {token}"}
        s = client.post(f"/v1/workspaces/{WS}/schedules", headers=h,
                        json={"title": "Brief", "question": "Give me a weekly KPI brief", "every_seconds": 3600}).json()
        r = client.post(f"/v1/workspaces/{WS}/schedules/{s['schedule_id']}/pause", headers=h)
        assert r.status_code == 200 and r.json()["enabled"] is False
        r = client.post(f"/v1/workspaces/{WS}/schedules/{s['schedule_id']}/resume", headers=h)
        assert r.status_code == 200 and r.json()["approval_id"]
        assert client.post(f"/v1/workspaces/{WS}/schedules/s_missing/pause", headers=h).status_code == 404
