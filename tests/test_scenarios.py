# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Acceptance scenarios A, C and H, checked against independent pandas computations."""

import pandas as pd
import pytest

from lyst import demo
from lyst.runtime import TaskRuntime

from .conftest import ask


@pytest.fixture
def frame():
    return demo.pnl()


def _q(df, start, end):
    m = pd.to_datetime(df["month"])
    return df[(m >= start) & (m < end)]


def test_a_operating_margin_decline_finds_emea(seeded, frame):
    lyst, owner = seeded
    t = ask(lyst, owner, "Why did operating margin decline last quarter?")
    assert t["status"] == "done", t["error"]
    r = t["result"]
    cur, prev = _q(frame, "2026-07-01", "2026-10-01"), _q(frame, "2026-04-01", "2026-07-01")
    m_cur = cur.operating_income.sum() / cur.revenue.sum()
    m_prev = prev.operating_income.sum() / prev.revenue.sum()
    assert f"{m_cur * 100:.1f}%" in r["answer"] and "Q3 2026" in r["answer"]
    assert f"{m_prev * 100:.1f}%" in r["answer"] and "Q2 2026" in r["answer"]
    assert "EMEA" in r["evidence"][0]
    assert r["confidence"]["level"] == "verified"
    names = {c["name"] for c in r["confidence"]["checks"] if c["passed"]}
    assert {"bridge_closes:region", "bridge_closes:product_line", "recompute", "coverage:current"} <= names
    assert r["actions"] and r["implications"]
    assert r["supporting"]["queries"] and all(q["query_id"] for q in r["supporting"]["queries"])
    assert r["supporting"]["metric"]["spec"]["name"] == "operating_margin"
    # the analysis is persisted with its evidence
    db = lyst.stores.workspace("northwind")
    assert db.scalar("SELECT count(*) FROM analyses WHERE task_id = ?", [t["id"]]) == 1


def test_a_values_match_pandas(seeded, frame):
    lyst, owner = seeded
    t = ask(lyst, owner, "What was revenue in Q2 2026 by region?")
    r = t["result"]
    q2 = _q(frame, "2026-04-01", "2026-07-01")
    table = {row[0]: row[1] for row in r["tables"][0]["rows"]}
    for region, value in q2.groupby("region").revenue.sum().items():
        assert table[region] == pytest.approx(value, rel=1e-9)
    # the split the question asked for is in the answer itself, with each region's share of the total
    assert "By region:" in r["answer"] and all(region in r["answer"] for region in q2.region.unique())
    import re

    shares = [int(x) for x in re.findall(r"\((\d+)%\)", r["answer"])]
    assert len(shares) == q2.region.nunique() and 98 <= sum(shares) <= 102


def test_ambiguous_term_asks_instead_of_guessing(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What is our margin?")["result"]
    assert r["status"] == "needs_input" and "cogs_ratio" in r["answer"] and "operating_margin" in r["answer"]


def test_c_ledger_discrepancies_are_found(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Profile pnl_ledger for data quality issues")["result"]
    text = " ".join(r["evidence"])
    assert "revenue: 1 nulls" in text and "1 duplicate rows" in text and "cogs: 1 negative values" in text
    assert {c["name"]: c["passed"] for c in r["confidence"]["checks"]} == {"duplicates": False, "nulls": False}


class Crash(BaseException):
    """Stands in for the process dying: not caught by the runtime."""


def test_h_resume_after_interruption(seeded, monkeypatch):
    lyst, owner = seeded
    calls: dict[str, int] = {}
    real = lyst.services.registry.run

    def flaky(name, ctx, args):
        calls[name] = calls.get(name, 0) + 1
        if name == "driver_analysis" and calls[name] == 1:
            raise Crash()
        return real(name, ctx, args)

    monkeypatch.setattr(lyst.services.registry, "run", flaky)
    tid = lyst.runtime.submit(owner, "northwind", "Why did operating margin decline last quarter?")
    with pytest.raises(Crash):
        lyst.runtime.run_once()
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "running"  # the dead worker still holds the lease
    done_before = [s["seq"] for s in t["steps"] if s["status"] == "done"]
    # a second worker takes over once the lease expires
    lyst.stores.control.execute("UPDATE tasks SET lease_until = now() - INTERVAL 1 SECOND WHERE id = ?", [tid])
    other = TaskRuntime(lyst.stores, lyst.services, worker_id="w_second")
    assert other.run_once() == tid
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "done" and t["attempts"] == 2
    assert calls["understand"] == 1 and calls["metric_value"] == 3  # finished steps did not run again
    assert calls["driver_analysis"] == 2
    assert done_before and all(s["status"] == "done" for s in t["steps"])
    db = lyst.stores.workspace("northwind")
    assert db.scalar("SELECT count(*) FROM analyses WHERE task_id = ?", [tid]) == 1
    # same answer as an uninterrupted run
    monkeypatch.setattr(lyst.services.registry, "run", real)
    clean = ask(lyst, owner, "Why did operating margin decline last quarter?")
    assert clean["result"]["answer"] == t["result"]["answer"]
    kinds = [e["kind"] for e in t["events"]]
    assert kinds.count("claimed") == 2
