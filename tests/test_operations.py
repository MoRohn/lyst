# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Operations: priority and fairness, quotas and admission, run time across resumes, calendar schedules (DST, missed
runs, overlap, duplicate ticks, notification) and the supervisor. Scenarios C (recurring analysis), F (concurrent
workload) and G (recovery: budget across restarts) of the eighth iteration."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lyst import operations as ops
from lyst.api import create_app
from lyst.llm import LLMUnavailable


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# ----------------------------------------------------------------------------- calendar


def test_weekly_schedule_stays_at_local_time_across_daylight_saving():
    # Monday 08:00 London: 08:00 UTC in winter (GMT), 07:00 UTC in summer (BST). Clocks change on 29 March 2026.
    first = ops.next_calendar_run(utc(2026, 3, 20), "08:00", [0], "Europe/London")
    second = ops.next_calendar_run(first, "08:00", [0], "Europe/London")
    assert first == utc(2026, 3, 23, 8, 0) and second == utc(2026, 3, 30, 7, 0)
    # and back again on 25 October
    assert ops.next_calendar_run(utc(2026, 10, 20), "08:00", [0], "Europe/London") == utc(2026, 10, 26, 8, 0)


def test_nonexistent_and_repeated_local_times_are_handled():
    # 01:30 does not exist in London on 29 March 2026: it runs at the pre-jump offset (01:30 UTC = 02:30 BST)
    assert ops.next_calendar_run(utc(2026, 3, 28, 12), "01:30", None, "Europe/London") == utc(2026, 3, 29, 1, 30)
    # 01:30 happens twice on 25 October 2026: one run, at the first (BST) occurrence, then the next day's
    fall = ops.next_calendar_run(utc(2026, 10, 24, 12), "01:30", None, "Europe/London")
    assert fall == utc(2026, 10, 25, 0, 30)
    assert ops.next_calendar_run(fall, "01:30", None, "Europe/London") == utc(2026, 10, 26, 1, 30)


def test_bad_calendar_inputs_are_refused(seeded):
    lyst, owner = seeded
    with pytest.raises(ValueError, match="time zone"):
        lyst.runtime.create_schedule(owner, "northwind", "x", "q", at="08:00", timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="weekday"):
        lyst.runtime.create_schedule(owner, "northwind", "x", "q", at="08:00", weekdays=["funday"])
    with pytest.raises(ValueError, match="HH:MM"):
        lyst.runtime.create_schedule(owner, "northwind", "x", "q", at="eight")


# ----------------------------------------------------------------------------- recurring analysis (scenario C)


def _approved_weekly(lyst, owner, **kw):
    rt = lyst.runtime
    admin = lyst.guard.create_user("admin@northwind.test", "Admin")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    s = rt.create_schedule(owner, "northwind", "Weekly performance", "What was revenue last month?",
                           at="08:00", weekdays=["mon"], timezone="Europe/London", **kw)
    rt.decide(admin, s["approval_id"], True)
    return s


def test_weekly_investigation_triggers_once_runs_and_notifies(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    s = _approved_weekly(lyst, owner)
    assert s["cadence"] == "every Monday at 08:00 (Europe/London)"
    row = rt.db.query("SELECT next_run_at FROM schedules WHERE id = ?", [s["schedule_id"]])[0]
    due = row["next_run_at"] + timedelta(minutes=1)
    started = rt.tick_schedules(due)
    assert len(started) == 1
    assert rt.tick_schedules(due) == []  # a second tick in the same minute starts nothing
    rt.drain()
    t = rt.get(owner, started[0])
    assert t["status"] == "done" and t["kind"] == "scheduled" and t["priority"] == ops.PRIORITY["scheduled"]
    assert t["outcome"] == "complete" and t["result"]["answer"] and t["result"]["confidence"]
    nxt = rt.db.query("SELECT next_run_at, last_task_id FROM schedules WHERE id = ?", [s["schedule_id"]])[0]
    assert nxt["last_task_id"] == started[0] and nxt["next_run_at"] - row["next_run_at"] == timedelta(days=7)
    # the owner is told in the inbox
    inbox = lyst.stores.workspace("northwind").query(
        "SELECT f.kind, f.label FROM notifications n JOIN findings f ON f.id = n.finding_id WHERE n.user_id = ?",
        [owner.user_id])
    assert {"kind": "scheduled_done", "label": "Weekly performance: ready"} in inbox


def test_after_downtime_a_schedule_runs_once_and_records_what_it_missed(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    s = _approved_weekly(lyst, owner)
    first = rt.db.scalar("SELECT next_run_at FROM schedules WHERE id = ?", [s["schedule_id"]])
    started = rt.tick_schedules(first + timedelta(days=21, hours=1))  # down for three weeks
    assert len(started) == 1
    sched = rt.db.query("SELECT missed, next_run_at FROM schedules WHERE id = ?", [s["schedule_id"]])[0]
    assert sched["missed"] == 3 and sched["next_run_at"] > first + timedelta(days=21, hours=1)
    assert any(e["kind"] == "catch_up" for e in rt.get(owner, started[0])["events"])


def test_a_run_due_while_the_previous_one_is_still_going_is_skipped(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    s = _approved_weekly(lyst, owner)
    first = rt.db.scalar("SELECT next_run_at FROM schedules WHERE id = ?", [s["schedule_id"]])
    t1 = rt.tick_schedules(first + timedelta(minutes=1))
    assert len(t1) == 1  # not drained: still queued a week later
    assert rt.tick_schedules(first + timedelta(days=7, minutes=1)) == []
    assert rt.db.scalar("SELECT skipped FROM schedules WHERE id = ?", [s["schedule_id"]]) == 1
    # with overlap=queue the next run is queued anyway
    rt.db.execute("UPDATE schedules SET overlap = 'queue' WHERE id = ?", [s["schedule_id"]])
    assert len(rt.tick_schedules(first + timedelta(days=14, minutes=1))) == 1


# ----------------------------------------------------------------------------- priority, fairness, quotas (scenario F)


def test_people_come_before_background_and_workspaces_share_workers(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    lyst.guard.create_workspace(owner, "acme", "Acme")
    sched = rt.submit(owner, "northwind", "What was revenue last month?", kind="scheduled")
    a1 = rt.submit(owner, "northwind", "What was revenue last quarter?")
    a2 = rt.submit(owner, "northwind", "What was revenue last year?")
    b1 = rt.submit(owner, "acme", "anything")
    assert rt.claim() == a1  # interactive before the older scheduled task
    # northwind now has one running; acme's newer task goes next instead of northwind's second
    assert rt.claim() == b1
    assert rt.claim() == a2
    assert rt.claim() == sched


def test_background_work_never_takes_the_last_worker(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    rt.workers = 2
    s1 = rt.submit(owner, "northwind", "q1", kind="scheduled")
    rt.submit(owner, "northwind", "q2", kind="scheduled")
    assert rt.claim() == s1
    assert rt.claim() is None  # one worker held back for people
    a = rt.submit(owner, "northwind", "What was revenue last quarter?")
    assert rt.claim() == a


def test_max_running_quota_holds_a_workspace_back(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    ops.set_quotas(rt.db, "northwind", owner.user_id, {"max_running": 1})
    a = rt.submit(owner, "northwind", "q1")
    rt.submit(owner, "northwind", "q2")
    assert rt.claim() == a and rt.claim() is None


def test_a_full_queue_refuses_new_work_with_429_but_repeats_still_return_the_first_task(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    ops.set_quotas(rt.db, "northwind", owner.user_id, {"max_queued": 2})
    first = rt.submit(owner, "northwind", "q1", idempotency_key="k1")
    rt.submit(owner, "northwind", "q2")
    with pytest.raises(ops.QuotaExceeded):
        rt.submit(owner, "northwind", "q3")
    assert rt.submit(owner, "northwind", "q1", idempotency_key="k1") == first
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post("/v1/workspaces/northwind/tasks", json={"question": "q4"}, headers=h)
        assert r.status_code == 429 and r.headers["Retry-After"] and r.json()["error"] == "quota_exceeded"


def test_quota_caps_a_tasks_time_budget(seeded):
    lyst, owner = seeded
    ops.set_quotas(lyst.runtime.db, "northwind", owner.user_id, {"task_budget_seconds": 120})
    tid = lyst.runtime.submit(owner, "northwind", "q", budget_seconds=600)
    assert lyst.runtime.db.scalar("SELECT budget_seconds FROM tasks WHERE id = ?", [tid]) == 120
    with pytest.raises(ValueError):
        ops.set_quotas(lyst.runtime.db, "northwind", owner.user_id, {"max_running": 0})


def test_monthly_model_budget_stops_model_calls(seeded, monkeypatch):
    lyst, owner = seeded
    db = lyst.runtime.db
    db.execute("INSERT INTO llm_calls (id, workspace_id, model, input_tokens, output_tokens, outcome, cost_usd) "
               "VALUES (nextval('llm_call_seq'), 'northwind', 'm', 1000, 500, 'ok', 5.0)")
    assert ops.model_budget_block(db, "northwind") is None
    ops.set_quotas(db, "northwind", owner.user_id, {"monthly_spend_usd": 5.0})
    assert "monthly model budget" in ops.model_budget_block(db, "northwind")
    llm = lyst.services.llm
    monkeypatch.setattr(llm, "allowed_for", lambda ws: True)
    with pytest.raises(LLMUnavailable, match="monthly model budget"):
        llm.chat("northwind", [{"role": "user", "content": "hi"}], purpose="test")
    ops.set_quotas(db, "northwind", owner.user_id, {"monthly_spend_usd": None, "monthly_tokens": 1500})
    assert "token budget" in ops.model_budget_block(db, "northwind")
    # the analysis still answers deterministically while the model is refused
    tid = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?")
    lyst.runtime.drain()
    assert lyst.runtime.get(owner, tid)["status"] == "done"


# ----------------------------------------------------------------------------- run time across resumes (scenario G)


def test_time_budget_counts_run_time_from_earlier_runs(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    tid = rt.submit(owner, "northwind", "What was revenue last quarter?", budget_seconds=600)
    # as if earlier runs (before a pause or an engine restart) had already used the whole budget
    rt.db.execute("UPDATE tasks SET active_seconds = 600 WHERE id = ?", [tid])
    rt.drain()
    t = rt.get(owner, tid)
    assert t["status"] == "failed" and "time budget" in t["error"]
    # a normal run records its run time
    ok = rt.submit(owner, "northwind", "What was revenue last quarter?")
    rt.drain()
    assert rt.db.scalar("SELECT active_seconds FROM tasks WHERE id = ?", [ok]) > 0
    # a retry starts a fresh budget
    rt.retry(owner, tid)
    rt.drain()
    assert rt.get(owner, tid)["status"] == "done"


# ----------------------------------------------------------------------------- supervisor and report


def test_supervisor_flags_stalls_expires_late_work_and_reports(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    now = datetime.now(UTC)
    stuck = rt.submit(owner, "northwind", "stuck")
    rt.db.execute("UPDATE tasks SET status = 'running', lease_owner = 'w_x', lease_until = ? WHERE id = ?",
                  [now + timedelta(minutes=5), stuck])
    rt.db.execute("INSERT INTO task_steps (task_id, seq, phase, capability, agent, status, started_at) "
                  "VALUES (?, 0, 'understand', 'understand', 'analyst', 'running', ?)", [stuck, now - timedelta(minutes=20)])
    late = rt.submit(owner, "northwind", "late", deadline_at=now - timedelta(minutes=1))
    rt.request_approval("northwind", owner.user_id, "x", "old approval", {})
    rt.db.execute("UPDATE approvals SET created_at = ? WHERE summary = 'old approval'", [now - timedelta(hours=30)])
    found = ops.supervise(rt, now)
    assert [s["task_id"] for s in found["stalled"]] == [stuck]
    assert found["expired"] == [late] and rt.get(owner, late)["status"] == "failed"
    assert found["stale_approvals"][0]["summary"] == "old approval"
    ops.supervise(rt, now)  # the stall event is recorded once, not every pass
    assert sum(e["kind"] == "stalled" for e in rt.get(owner, stuck)["events"]) == 1
    assert rt.get(owner, stuck)["status"] == "running"  # flagged, never marked done or killed by the supervisor
    rep = ops.report(rt, "northwind", now)
    assert {"priority": 0, "status": "running", "count": 1} in rep["queue"]
    assert rep["failures_24h"].get("deadline") == 1 and rep["usage"][0]["workspace_id"] == "northwind"


def test_operations_and_quota_endpoints_are_admin_only(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    admin = lyst.guard.create_user("a@n.test", "A")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    ho, hv, ha = ({"Authorization": f"Bearer {lyst.guard.issue_token(u)}"} for u in (owner, viewer, admin))
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get("/v1/operations", headers=ho).status_code == 200
        assert c.get("/v1/operations", headers=ha).status_code == 403
        assert c.get("/v1/workspaces/northwind/operations", headers=ha).status_code == 200
        assert c.get("/v1/workspaces/northwind/operations", headers=hv).status_code == 403
        # a workspace admin cannot raise their own quota; the installation admin can
        assert c.put("/v1/workspaces/northwind/quotas", json={"max_running": 9}, headers=ha).status_code == 403
        r = c.put("/v1/workspaces/northwind/quotas", json={"max_running": 3, "monthly_spend_usd": 20}, headers=ho)
        assert r.status_code == 200 and r.json()["max_running"] == 3
        r = c.post("/v1/workspaces/northwind/schedules", headers=ho, json={
            "title": "Weekly", "question": "What was revenue last month?", "at": "08:00", "weekdays": ["mon"],
            "timezone": "America/New_York"})
        assert r.status_code == 200 and r.json()["cadence"] == "every Monday at 08:00 (America/New_York)"
