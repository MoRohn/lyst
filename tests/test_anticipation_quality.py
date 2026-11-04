# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Learning from outcomes, anticipation quality metrics, retention, and acceptance scenario I (offline anticipation:
the whole engine, investigations included, runs without any network request)."""

import socket
from datetime import UTC, datetime, timedelta

import pytest

from lyst.anticipate import learning, model

WS = "northwind"


def _item(db, key, kind="next_best_action", action="watch", subject="revenue"):
    a, _ = model.upsert(db, {"kind": kind, "objective": f"Item {key}", "subject_kind": "metric", "subject_id": subject,
                             "audience": ["u"], "signals": [], "reasons": ["r"], "scores": {}, "score": 50.0, "level_required": 1,
                             "actions": [{"id": action, "label": action}], "depends": {}, "needed_at": None,
                             "valid_until": None, "dedupe_key": f"t|{key}"})
    return a["id"]


def test_one_outcome_moves_ranking_a_little_and_a_pattern_moves_it_more(seeded):
    lyst, _ = seeded
    db = lyst.stores.workspace(WS)
    assert learning.weights(db) == {}
    model.transition(db, _item(db, "1"), "dismissed")
    w1 = learning.weights(db)["next_best_action/watch"]
    assert 0.85 < w1 < 1.0                                                     # not overinterpreted
    for i in range(2, 12):
        model.transition(db, _item(db, str(i), subject=f"m{i}"), "dismissed")
    assert learning.weights(db)["next_best_action/watch"] == 0.5               # a pattern, clamped
    for i in range(12, 15):
        model.transition(db, _item(db, str(i), action="run_now", subject=f"m{i}"), "accepted")
    assert learning.weights(db)["next_best_action/run_now"] > 1.2              # families learn separately


def test_learned_weight_changes_the_score_of_new_items(seeded):
    lyst, owner = seeded
    db = lyst.stores.workspace(WS)
    for i in range(6):
        model.transition(db, _item(db, f"r{i}", kind="recurring_prep", action="run_now", subject=f"s{i}"), "dismissed")
    s = lyst.runtime.create_schedule(owner, WS, "Monthly finance review", "Review revenue", 2592000)
    lyst.runtime.decide(owner, s["approval_id"], True)
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                                [datetime.now(UTC) + timedelta(minutes=30), s["schedule_id"]])
    lyst.anticipation.set_settings(owner, WS, {"level": 1})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="recurring_prep")
    from lyst.anticipate.service import combine

    assert a["score"] < combine(a["scores"]) and a["score"] == combine(a["scores"], learning.weights(db)["recurring_prep/run_now"])


def test_twice_dismissed_family_is_not_raised_again_about_that_subject(seeded):
    lyst, owner = seeded
    db = lyst.stores.workspace(WS)
    s = lyst.runtime.create_schedule(owner, WS, "Monthly finance review", "Review revenue", 2592000)
    lyst.runtime.decide(owner, s["approval_id"], True)
    sid = s["schedule_id"]
    for k in ("a", "b"):
        model.transition(db, _item(db, k, kind="recurring_prep", action="run_now", subject=sid), "dismissed")
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", [datetime.now(UTC) + timedelta(minutes=30), sid])
    lyst.anticipation.cycle(WS)
    assert lyst.anticipation.list(owner, WS, kind="recurring_prep") == []


def test_completion_is_learned_and_quality_is_reported(seeded):
    lyst, owner = seeded
    s = lyst.runtime.create_schedule(owner, WS, "Monthly finance review", "Review revenue for the latest month", 2592000)
    lyst.runtime.decide(owner, s["approval_id"], True)
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                                [datetime.now(UTC) + timedelta(minutes=30), s["schedule_id"]])
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS)
    lyst.anticipation.act(owner, WS, a["id"], "run_now")
    lyst.runtime.drain()
    lyst.anticipation.cycle(WS)
    db = lyst.stores.workspace(WS)
    assert db.scalar("SELECT status FROM anticipations WHERE id = ?", [a["id"]]) == "completed"
    lyst.anticipation.act(owner, WS, a["id"], "useful")
    q = lyst.anticipation.quality(owner, WS)
    assert q["relevance"]["positive"] == 3 and q["relevance"]["useful_rate"] == 1.0      # accepted, completed, useful
    assert q["timeliness"]["prepared_before_needed"] == 1.0 and q["timeliness"]["median_lead_hours"] > 0
    assert q["governance"] == {"accepted": 0, "accepted_without_audit": 0, "tasks_run_as_someone_else": 0, "denied_attempts": 0}
    assert q["efficiency"]["model_calls"] == 0 and q["basis"].endswith("no model-generated confidence")
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    from lyst.security import AccessDenied

    with pytest.raises(AccessDenied):
        lyst.anticipation.quality(viewer, WS)


def test_retention_removes_old_closed_items_only(seeded):
    lyst, _ = seeded
    db = lyst.stores.workspace(WS)
    old, fresh, open_ = _item(db, "old"), _item(db, "fresh"), _item(db, "open")
    model.transition(db, old, "dismissed")
    model.transition(db, fresh, "dismissed")
    db.execute("UPDATE anticipations SET updated_at = now() - INTERVAL 200 DAY WHERE id IN (?, ?)", [old, open_])
    assert learning.prune(db) == 1
    left = {r["id"] for r in db.query("SELECT id FROM anticipations")}
    assert left == {fresh, open_} and not db.scalar("SELECT count(*) FROM anticipation_events WHERE anticipation_id = ?", [old])


def test_i_offline_anticipation_makes_no_network_request(seeded, monkeypatch):
    """Every anticipator, including a level-3 investigation run by the analyst, with sockets to anywhere but loopback
    refused. The installation has no model configured, as in an offline deployment."""
    lyst, owner = seeded
    attempts: list = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(address)
            raise OSError("network disabled in this test")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("no DNS offline")))
    m = lyst.monitors.create(owner, WS, metric="operating_margin", grain="month", threshold_sigma=6.0)
    lyst.runtime.decide(owner, m["approval_id"], True)
    s = lyst.runtime.create_schedule(owner, WS, "Monthly finance review", "Review revenue for the latest month", 2592000)
    lyst.runtime.decide(owner, s["approval_id"], True)
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                                [datetime.now(UTC) + timedelta(minutes=30), s["schedule_id"]])
    t = lyst.runtime.submit(owner, WS, "Why did operating margin fall in Q3 2026?")
    lyst.runtime.drain()
    lyst.anticipation.set_settings(owner, WS, {"level": 3})
    lyst.anticipation.cycle(WS)
    lyst.runtime.drain()
    lyst.anticipation.cycle(WS)
    kinds = {a["kind"] for a in lyst.anticipation.list(owner, WS)}
    assert {"recurring_prep", "next_best_action"} <= kinds and lyst.runtime.get(owner, t)["status"] == "done"
    assert attempts == []
    st = lyst.anticipation.settings(owner, WS)
    assert st["external_services"] is False and "nothing leaves this installation" in st["methods"]
