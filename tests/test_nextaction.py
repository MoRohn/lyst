# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Next-best analytical action. Acceptance scenario D: after a supplier performance analysis, Lyst proposes the next
investigation from its unresolved finding, specific to it, and nothing generic or already asked."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from lyst import demo
from lyst.semantic import Measure, MetricSpec

WS = "northwind"


def _suppliers() -> pd.DataFrame:
    """Two years of monthly deliveries. In Q3 2026 Acme Metals slips, and inside Acme only its castings line does."""
    rng = np.random.default_rng(7)
    rows = []
    for month in pd.date_range("2024-10-01", "2026-09-01", freq="MS"):
        for supplier in ("Acme Metals", "Borealis Parts", "Cobalt Supply"):
            for category in ("Castings", "Fasteners", "Electronics"):
                n = 200
                rate = 0.95 + rng.normal(0, 0.004)
                if supplier == "Acme Metals" and category == "Castings" and month >= pd.Timestamp("2026-07-01"):
                    rate = 0.55
                rows.append({"month": month.date().isoformat(), "supplier": supplier, "category": category,
                             "deliveries": n, "on_time": int(round(n * rate))})
    return pd.DataFrame(rows)


@pytest.fixture
def suppliers(seeded):
    lyst, owner = seeded
    lyst.data.ingest(owner, WS, "deliveries", demo.csv_bytes(_suppliers()), filename="deliveries.csv", title="Supplier deliveries")
    lyst.semantic.define(owner, WS, MetricSpec(
        name="on_time_rate", title="On-time delivery rate", dataset="deliveries", kind="ratio",
        numerator=Measure(agg="sum", column="on_time"), denominator=Measure(agg="sum", column="deliveries"),
        time_column="month", dimensions=["supplier", "category"], format="percent", synonyms=["on time delivery", "otd"]))
    return lyst, owner


def _analyse(lyst, owner, q):
    tid = lyst.runtime.submit(owner, WS, q)
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "done", t
    return t


def test_d_supplier_analysis_suggests_the_specific_next_investigation(suppliers):
    lyst, owner = suppliers
    t = _analyse(lyst, owner, "Why did on-time delivery rate fall in Q3 2026?")
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    lyst.anticipation.cycle(WS)
    sugg = lyst.anticipation.list(owner, WS, subject_id=t["id"])
    assert sugg, "a finished analysis with a dominant driver yields next steps"
    top = sugg[0]
    drill = top["actions"][0]
    # specific: names the dominant member, the period, and the dimension the analysis did not use
    assert top["kind"] == "next_best_action" and drill["id"] == "run_now"
    assert "Acme Metals" in drill["question"] and "Q3 2026" in drill["question"] and "by category" in drill["question"]
    assert "Acme Metals accounts for" in top["reasons"][0] and top["verdict"] == "ready"
    full = lyst.anticipation.get(owner, WS, top["id"])
    assert full["verification"]["checks"][0]["check"] == "slice" and full["resources"]["queries"] == 1
    kinds = {s["objective"].split(" ")[0] for s in sugg}
    assert kinds <= {"Look", "Watch", "See", "Run"} and len(sugg) <= 3          # nothing generic
    # useful: running it finds the castings line inside Acme
    r = lyst.anticipation.act(owner, WS, top["id"], "run_now")
    lyst.runtime.drain()
    follow = lyst.runtime.get(owner, r["task_id"])
    assert follow["status"] == "done" and follow["kind"] == "followup"
    assert "Castings" in " ".join(follow["result"]["evidence"])
    # the follow-up itself was a drill: it does not spawn another drill on the same slice
    lyst.anticipation.cycle(WS)
    assert not [s for s in lyst.anticipation.list(owner, WS, subject_id=r["task_id"]) if s["objective"].startswith("Look inside")]


def test_watch_requests_a_monitor_that_still_needs_approval(suppliers):
    lyst, owner = suppliers
    t = _analyse(lyst, owner, "Why did on-time delivery rate fall in Q3 2026?")
    lyst.anticipation.cycle(WS)                                          # level 1: suggested, not executed
    watch = next(s for s in lyst.anticipation.list(owner, WS, subject_id=t["id"]) if s["actions"][0]["id"] == "watch")
    assert watch["status"] == "candidate"
    r = lyst.anticipation.act(owner, WS, watch["id"], "watch")
    assert r["needs_approval"] and r["approval_id"]
    ap = [a for a in lyst.runtime.approvals(owner, WS) if a["id"] == r["approval_id"]]
    assert ap and ap[0]["action"] == "monitor.activate"
    mon = lyst.stores.workspace(WS).query("SELECT enabled FROM monitors WHERE id = ?", [r["monitor_id"]])
    assert mon[0]["enabled"] is False                                    # inferred interest is not authorization


def test_repeated_question_suggests_a_schedule_at_its_rhythm(suppliers):
    lyst, owner = suppliers
    q = "Why did on-time delivery rate fall in Q3 2026?"
    tasks = [_analyse(lyst, owner, q) for _ in range(3)]
    for i, t in enumerate(tasks):   # asked about a week apart
        lyst.stores.control.execute("UPDATE tasks SET created_at = ? WHERE id = ?",
                                    [datetime.now(UTC) - timedelta(days=14 - 7 * i, minutes=1), t["id"]])
    lyst.anticipation.cycle(WS)
    rec = [s for s in lyst.anticipation.list(owner, WS) if s["actions"][0]["id"] == "schedule"]
    assert len(rec) == 1 and rec[0]["actions"][0]["every_seconds"] == 604800 and "3 times" in rec[0]["reasons"][0]
    r = lyst.anticipation.act(owner, WS, rec[0]["id"], "schedule")
    assert r["needs_approval"]
    assert lyst.stores.control.scalar("SELECT enabled FROM schedules WHERE id = ?", [r["schedule_id"]]) is False


def test_no_suggestion_for_what_was_already_asked_or_for_an_empty_slice(suppliers):
    lyst, owner = suppliers
    t = _analyse(lyst, owner, "Why did on-time delivery rate fall in Q3 2026?")
    _analyse(lyst, owner, "What drove on-time delivery rate for supplier Acme Metals in Q3 2026 by category?")
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    lyst.anticipation.cycle(WS)
    assert not [s for s in lyst.anticipation.list(owner, WS, subject_id=t["id"]) if s["objective"].startswith("Look inside")]


def test_viewer_without_rights_gets_no_runnable_slice_for_restricted_data(suppliers):
    lyst, owner = suppliers
    t = _analyse(lyst, owner, "Why did on-time delivery rate fall in Q3 2026?")
    # the person loses access before preparation: the drill is dropped (suppressed), never shown
    db = lyst.stores.workspace(WS)
    lyst.stores.control.execute("DELETE FROM memberships WHERE user_id = ? AND workspace_id = ?", [owner.user_id, WS])
    admin = lyst.guard.create_user("adm@northwind.test", "Adm")
    lyst.stores.control.execute("INSERT INTO memberships VALUES (?, ?, 'owner')", [WS, admin.user_id])
    lyst.anticipation.set_settings(admin, WS, {"level": 2})
    lyst.anticipation.cycle(WS)
    drills = db.query("SELECT status FROM anticipations WHERE subject_id = ? AND dedupe_key LIKE '%|drill|%'", [t["id"]])
    assert drills and drills[0]["status"] == "suppressed"
