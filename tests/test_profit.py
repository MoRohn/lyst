# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Profit from separate income and expense exports: "What was my profit last quarter?" is income minus expenses from
the two approved metrics, compared with the period before and a year earlier, when the workspace has no profit metric
of its own."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from lyst.analyst import profit
from lyst.semantic import Measure, MetricSpec

from .conftest import ask
from .test_small_business import WS, books, open_studio


def _sums(start: str, end: str) -> tuple[float, float]:
    inv, exp = books()
    inv["issue_date"], exp["date"] = pd.to_datetime(inv["issue_date"]), pd.to_datetime(exp["date"])
    i = inv[(inv.issue_date >= start) & (inv.issue_date < end)].amount.sum()
    e = exp[(exp.date >= start) & (exp.date < end)].amount.sum()
    return float(i), float(e)


def _money(v: float) -> str:
    return profit._money(v)


def test_profit_last_quarter_is_income_minus_expenses(lyst):
    owner, _ = open_studio(lyst)
    t = ask(lyst, owner, "What was my profit last quarter?", ws=WS)
    r = t["result"]
    u = json.loads(lyst.runtime.db.query("SELECT output FROM task_steps WHERE task_id = ? AND capability = 'understand'",
                                         [t["id"]])[0]["output"])
    assert u["intent"] == "profit" and u["metrics"] == ["invoiced", "expenses"]
    assert r["status"] == "complete" and r["mode"] == "profit"
    i, e = _sums("2026-07-01", "2026-10-01")
    pi, pe = _sums("2026-04-01", "2026-07-01")
    yi, ye = _sums("2025-07-01", "2025-10-01")
    assert r["answer"].startswith(f"Profit in Q3 2026: {_money(i - e)} ({_money(i)} invoiced minus {_money(e)} expenses), "
                                  f"a {(i - e) / i:.0%} margin.")
    assert f"on Q2 2026 ({_money(pi - pe)})" in r["answer"] and f"on Q3 2025 ({_money(yi - ye)})" in r["answer"]
    assert {c["name"]: c["passed"] for c in r["confidence"]["checks"]} == {"numbers_backed": True, "profit_adds_up": True}
    assert any("not tax or accrual accounting" in a for a in r["supporting"]["assumptions"])
    months = r["tables"][0]["rows"]
    assert len(months) == 12 and months[-1][0] == "Sep 2026"


def test_a_losing_month_and_the_year_so_far(lyst):
    owner, _ = open_studio(lyst)
    i, e = _sums("2026-08-01", "2026-09-01")
    r = ask(lyst, owner, "What was my profit in Aug 2026?", ws=WS)["result"]
    assert r["answer"].startswith(f"{'Loss' if i < e else 'Profit'} in Aug 2026: {_money(abs(i - e))}")
    r = ask(lyst, owner, "How much did I make this year?", ws=WS)["result"]
    i, e = _sums("2026-01-01", "2026-10-01")
    yi, ye = _sums("2025-01-01", "2025-10-01")
    assert r["answer"].startswith(f"Profit in 2026 YTD: {_money(i - e)}")
    assert f"on Jan 2025 to Sep 2025 ({_money(yi - ye)})" in r["answer"]
    assert "runs through Sep 2026" in r["supporting"]["assumptions"][0]


def test_profit_moves_to_another_period(lyst):
    from lyst.followups import FollowUps

    owner, _ = open_studio(lyst)
    t = ask(lyst, owner, "What was my profit last quarter?", ws=WS)
    res = FollowUps(lyst.runtime, lyst.semantic).apply(owner, t["id"], "what about Q2 2026?")
    assert res["action"] == "task" and res["question"] == "What was my profit in Q2 2026?"
    lyst.runtime.drain()
    assert lyst.runtime.get(owner, res["task_id"])["result"]["answer"].startswith("Profit in Q2 2026: ")


def test_a_workspace_with_its_own_profit_measure_keeps_it(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "What was our profit last quarter?")
    assert (t.get("result") or {}).get("mode") != "profit"


def _spec(name, title, hib, fmt="currency", agg="sum"):
    return MetricSpec(name=name, title=title, dataset="d", measure=Measure(agg=agg, column="amount"),
                      time_column="date", format=fmt, higher_is_better=hib)


def test_pair_needs_one_income_and_one_expense_and_no_profit_of_its_own():
    inc, exp = _spec("invoiced", "Invoiced", True), _spec("expenses", "Expenses", False)
    assert profit.pair([inc, exp, _spec("active_clients", "Active clients", True, "number", "count_distinct")]) == (inc, exp)
    assert profit.pair([inc]) is None
    assert profit.pair([inc, exp, _spec("operating_income", "Operating income", True)]) is None
    assert profit.pair([inc, exp, _spec("revenue", "Revenue", True)]) is None  # two income metrics: ambiguous


@pytest.mark.parametrize("text", ["What was my profit last quarter?", "How much did I make this year?", "Am I making money?",
                                  "What's my take-home after expenses?"])
def test_profit_phrases(text):
    assert profit.asks_for_profit(text)


@pytest.mark.parametrize("text", ["Who owes me money?", "What was revenue last quarter?",
                                  "What do our documents say about profit sharing?"])
def test_not_profit_phrases(text):
    assert not profit.asks_for_profit(text)
