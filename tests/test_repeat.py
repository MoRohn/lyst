# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A first question that says when to repeat ("Send me the KPI brief every Monday") is answered now and asks for the
schedule, which starts only once an admin approves it. "for each region" is a breakdown, not a schedule."""

from __future__ import annotations

import pytest

from lyst.analyst import repeat

from .conftest import ask


def _schedules(lyst, owner):
    return lyst.runtime.schedules(owner, "northwind")


def test_a_brief_asked_every_monday_is_answered_and_asks_for_the_schedule(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Send me the KPI brief every Monday")["result"]
    assert r["status"] == "complete" and r["mode"] == "brief"
    assert r["actions"][0] == "This repeats every Monday once an admin approves it under Approvals."
    (s,) = _schedules(lyst, owner)
    assert s["state"] == "awaiting_approval" and s["weekdays"] == "mon" and s["at_time"] == "08:00"
    assert r["supporting"]["schedule"]["schedule_id"] == s["id"]
    assert r["supporting"]["schedule"]["question"] == "Send me the KPI brief"
    # asking again does not make a second schedule or a second approval
    ask(lyst, owner, "Send me the KPI brief every Monday")
    assert len(_schedules(lyst, owner)) == 1
    pending = [a for a in lyst.runtime.approvals(owner, "northwind") if a["action"] == "schedule.activate"]
    assert len(pending) == 1


def test_the_scheduled_run_does_not_schedule_itself_again(seeded):
    lyst, owner = seeded
    admin = lyst.guard.create_user("admin@northwind.test", "Admin")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    ask(lyst, owner, "Revenue by region every day at 9am")
    (s,) = _schedules(lyst, owner)
    lyst.runtime.decide(admin, s["approval_id"], True)
    tid = lyst.runtime.submit(owner, "northwind", "Revenue by region", kind="scheduled", schedule_id=s["id"])
    lyst.runtime.drain()
    r = lyst.runtime.get(owner, tid)["result"]
    assert "schedule" not in r["supporting"] and len(_schedules(lyst, owner)) == 1


def test_a_viewer_is_told_who_can_schedule(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    r = ask(lyst, viewer, "What was revenue last quarter? Send it every Monday")["result"]
    assert r["actions"][0].startswith("Ask an editor to repeat this every Monday")
    assert not _schedules(lyst, owner)


@pytest.mark.parametrize(("q", "rest", "when"), [
    ("Send me the KPI brief every Monday", "Send me the KPI brief", "every Monday"),
    ("Revenue by region every day at 9am", "Revenue by region", "every day at 9am"),
    ("Every Friday, give me the close checks", "give me the close checks", "Every Friday"),
])
def test_requested(q, rest, when):
    got = repeat.requested(q)
    assert got and got[0] == rest and got[2] == when


@pytest.mark.parametrize("q", ["Revenue for each region last quarter", "Give me a weekly KPI brief",
                               "What was revenue every quarter this year?"])
def test_not_requested(q):
    assert repeat.requested(q) is None
