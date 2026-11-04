# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Anticipation consumes the unified event model: a relevant event brings the next cycle forward for its workspace only,
bursts collapse into one cycle, and anticipation's own actions become events."""

from datetime import UTC, datetime, timedelta

WS = "northwind"


def test_events_bring_a_cycle_forward_and_bursts_collapse(seeded, monkeypatch):
    lyst, owner = seeded
    svc = lyst.anticipation
    assert svc.on_event in lyst.events.subscribers
    svc.every, svc.min_gap = 3600, 30
    svc.tick_all(force=True)                                   # the fallback full pass
    calls = []
    real = svc.cycle
    monkeypatch.setattr(svc, "cycle", lambda ws, now=None: calls.append(ws) or real(ws, now))
    assert svc.tick_all() == 0 and calls == []                  # nothing happened: nothing runs
    svc.on_event({"type": "member.added", "workspace_id": "other"} | {"type": "noise.happened"})
    assert svc.tick_all() == 0 and calls == []                  # irrelevant events are ignored
    for _ in range(5):
        svc.on_event({"type": "dataset.version_created", "workspace_id": WS})
    svc._last_cycle.clear()
    svc.tick_all()
    assert calls == [WS]                                        # five events, one cycle, one workspace
    svc.on_event({"type": "task.completed", "workspace_id": WS})
    svc.tick_all()
    assert calls == [WS]                                        # within min_gap: waits


def test_anticipation_actions_are_events(seeded):
    lyst, owner = seeded
    s = lyst.runtime.create_schedule(owner, WS, "Monthly finance review", "Review revenue", 2592000)
    lyst.runtime.decide(owner, s["approval_id"], True)
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                                [datetime.now(UTC) + timedelta(minutes=30), s["schedule_id"]])
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS)
    lyst.anticipation.act(owner, WS, a["id"], "run_now")
    lyst.events.collect()
    types = {e["type"] for e in lyst.events.since(WS, 0)}
    assert {"anticipation.settings_changed", "anticipation.accepted"} <= types
