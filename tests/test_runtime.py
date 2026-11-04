# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
from datetime import UTC, datetime, timedelta

import pytest

from .conftest import ask


def test_pause_resume_cancel(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    tid = rt.submit(owner, "northwind", "What was revenue last quarter?")
    rt.pause(owner, tid)
    assert rt.run_once() is None
    rt.resume(owner, tid)
    assert rt.run_once() == tid and rt.get(owner, tid)["status"] == "done"
    t2 = rt.submit(owner, "northwind", "forecast revenue")
    rt.cancel(owner, t2)
    assert rt.run_once() is None and rt.get(owner, t2)["status"] == "cancelled"
    with pytest.raises(ValueError):
        rt.resume(owner, t2)


def test_step_budget(seeded):
    lyst, owner = seeded
    tid = lyst.runtime.submit(owner, "northwind", "Why did operating margin decline last quarter?", max_steps=4)
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "failed" and "step budget" in t["error"]


def test_schedule_needs_approval_then_runs(seeded):
    lyst, owner = seeded
    rt = lyst.runtime
    admin = lyst.guard.create_user("admin@northwind.test", "Admin")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    s = rt.create_schedule(owner, "northwind", "Weekly revenue", "What was revenue last month?", 7 * 86400)
    later = datetime.now(UTC) + timedelta(days=8)
    assert rt.tick_schedules(later) == []  # not approved yet
    rt.decide(admin, s["approval_id"], True)
    started = rt.tick_schedules(later)
    assert len(started) == 1
    rt.drain()
    t = rt.get(owner, started[0])
    assert t["status"] == "done" and t["kind"] == "scheduled" and t["autonomy"] == "autonomous"
    assert rt.tick_schedules(later) == []  # next run moved forward


def test_failed_step_is_recorded_and_retry_works(seeded, monkeypatch):
    lyst, owner = seeded
    real = lyst.services.registry.run
    state = {"fail": True}

    def broken(name, ctx, args):
        if name == "deliver" and state["fail"]:
            raise RuntimeError("renderer down")
        return real(name, ctx, args)

    monkeypatch.setattr(lyst.services.registry, "run", broken)
    tid = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?")
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "failed" and "renderer down" in t["error"]
    state["fail"] = False
    lyst.runtime.retry(owner, tid)
    lyst.runtime.drain()
    assert lyst.runtime.get(owner, tid)["status"] == "done"


def test_trend_and_forecast(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Forecast revenue")
    r = t["result"]
    assert t["status"] == "done" and r["charts"][0]["type"] == "forecast" and len(r["charts"][0]["y"]) == 6
    t = ask(lyst, owner, "Show the operating margin trend over time")
    assert t["result"]["charts"][0]["type"] == "line"


def test_long_requests_get_a_whole_word_title_and_keep_the_full_question(seeded):
    from lyst.runtime import short_title

    lyst, owner = seeded
    q = ("Investigate our declining operating margins across the last six quarters, isolate the strongest contributing "
         "factors, test alternative explanations, and prepare an executive briefing.")
    t = short_title(q)
    assert len(t) <= 120 and t.endswith("…") and not t.endswith("te…") and q.startswith(t[:-1])
    assert short_title("What was revenue last quarter?") == "What was revenue last quarter?"
    tid = lyst.runtime.submit(owner, "northwind", q)
    task = lyst.runtime.get(owner, tid)
    assert task["title"] == t and task["request"]["question"] == q
