# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Period-close checks: a month or quarter is checked for arrival, cleanliness, a key-by-key tie-out to a second source
and segment lines that need a comment, against the demo's planted ledger discrepancies and EMEA cost jump."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from lyst import demo
from lyst.analyst import close
from lyst.followups import FollowUps

from .conftest import ask


def _intent(lyst, task):
    row = lyst.runtime.db.query("SELECT output FROM task_steps WHERE task_id = ? AND capability = 'understand'", [task["id"]])
    return json.loads(row[0]["output"])["intent"]


def _check(r, prefix):
    return next(c for c in r["confidence"]["checks"] if c["name"].startswith(prefix))


def test_month_end_close_ties_out_and_lists_lines_to_explain(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Run the month-end close checks")
    r = t["result"]
    assert _intent(lyst, t) == "close" and r["mode"] == "close" and r["status"] == "complete"
    assert r["answer"].startswith("Sep 2026 is not ready to close yet: ")
    # Sep 2026 has no planted ledger discrepancy: the P&L ties to the ledger key by key
    assert _check(r, "ties_out:pnl~pnl_ledger")["passed"] and "pnl ties to pnl_ledger" in r["answer"]
    assert _check(r, "arrived:pnl")["passed"] and _check(r, "clean:pnl")["passed"]
    # the planted EMEA cost jump is the first line to explain, with the metrics that moved with it
    flux = [e for e in r["evidence"] if e.startswith("Flux: ")]
    assert flux[0].startswith("Flux: Operating margin, EMEA (region)") and "moved with it" in flux[0]
    assert not _check(r, "flux")["passed"]
    assert r["actions"][0].startswith("Ask why operating margin moved in EMEA")
    # Lyst reports; it never marks a period closed
    assert any("does not post journals" in a for a in r["supporting"]["assumptions"])
    assert r["supporting"]["queries"] and r["supporting"]["close"]["ready"] is False


def test_a_missing_ledger_amount_breaks_the_august_tie_out(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Is August 2026 ready to close?")["result"]
    assert r["answer"].startswith("Aug 2026 is not ready to close yet: pnl and pnl_ledger do not tie on 1 key")
    df = demo.pnl()
    row = df[(df.region == "LATAM") & (df.product_line == "Hardware") & (pd.to_datetime(df.month) == "2026-08-01")]
    amount = f"{float(row.revenue.iloc[0]):,.2f}"
    assert f"Tie-out break, Hardware / LATAM: revenue {amount} in pnl vs none in pnl_ledger (missing in pnl_ledger)." \
        in r["evidence"]
    breaks = next(tb for tb in r["tables"] if tb["title"].startswith("Tie-out breaks"))
    assert len(breaks["rows"]) == 1


def test_a_double_booking_breaks_the_quarter_tie_out(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Does the P&L tie to the ledger for Q1 2026?")["result"]
    assert r["answer"].startswith("Q1 2026 is not ready to close yet: pnl and pnl_ledger do not tie on 1 key")
    hit = next(e for e in r["evidence"] if e.startswith("Tie-out break, Services / APAC"))
    assert "differ too" in hit  # every amount of the double-booked row, in one line


def test_a_second_source_the_reader_cannot_read_is_named_not_tied(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    lyst.data.set_column_policy(owner, "northwind", "pnl_ledger", "revenue", "confidential", "admin")
    r = ask(lyst, viewer, "Run the close checks for Sep 2026")["result"]
    assert r["status"] == "complete"
    assert not any(c["name"].startswith("ties_out") for c in r["confidence"]["checks"])
    scope = next(c for c in r["confidence"]["checks"] if c["name"] == "access_scope")
    assert "pnl vs pnl_ledger" in scope["detail"]


@pytest.mark.parametrize("text", ["Run the month-end close checks", "Is September ready to close?",
                                  "Does the P&L tie to the ledger for Q3?", "Reconcile revenue to the ledger",
                                  "quarter-end close", "Flux review for Sep 2026"])
def test_close_phrases(text):
    assert close.asks_for_close(text)


@pytest.mark.parametrize("text", ["What was revenue last quarter?", "Give me a weekly KPI brief",
                                  "How close are we to target?", "Reconcile ledger erp against ledger bank",
                                  "Reconcile the ledger erp postings against the ledger bank"])
def test_not_close_phrases(text):
    assert not close.asks_for_close(text)


def test_close_checks_can_be_repeated_or_moved(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Run the month-end close checks")
    fu = FollowUps(lyst.runtime, lyst.semantic)
    res = fu.apply(owner, t["id"], "every weekday at 9")
    assert res["action"] == "schedule" and res["approval_id"] and res["enabled"] is False
    res = fu.apply(owner, t["id"], "what about Aug 2026?")
    assert res["action"] == "task" and res["question"] == "Run the close checks for Aug 2026"
