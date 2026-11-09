# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What an answer carries for everyday analyst questions: a single figure comes with its recent history as a chart,
a trend answers the window it was asked about in words, a ranking leads with the members asked for, a filter is
named in the headline, and a what-if starts from the same quarter as every other answer."""

from __future__ import annotations

import pandas as pd
import pytest

from lyst import demo

from .conftest import ask


def _q(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    m = pd.to_datetime(df["month"])
    return df[(m >= start) & (m < end)]


def test_a_single_figure_comes_with_the_quarters_leading_up_to_it(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was revenue last quarter?")["result"]
    assert r["answer"].startswith("Revenue was $25.41M in Q3 2026")
    chart = r["charts"][0]
    assert chart["type"] == "bar" and chart["title"] == "Revenue by quarter"
    assert len(chart["x"]) == 8 and chart["x"][-1] == "Q3 2026"
    q3 = _q(demo.pnl(), "2026-07-01", "2026-10-01").revenue.sum()
    assert chart["y"][-1] == pytest.approx(q3, rel=1e-9)
    # a named earlier period ends the chart, not the latest quarter in the data
    r = ask(lyst, owner, "What was revenue in Q1 2025?")["result"]
    assert r["charts"][0]["x"][-1] == "Q1 2025"
    # the confidence check that passed says so, instead of describing the failure it did not find
    fresh = next(c for c in r["confidence"]["checks"] if c["name"] == "freshness")
    assert fresh["passed"] and "covering all of Q1 2025" in fresh["detail"]


def test_a_trend_answers_the_window_it_was_asked_about(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "How has operating margin trended over the last 12 months?")["result"]
    chart = r["charts"][0]
    assert chart["type"] == "line" and len(chart["x"]) == 12 and str(chart["x"][0]).startswith("2025-10")
    # direction, both ends in plain months, and the month that moved it
    assert r["answer"].startswith("Operating margin fell from ") and "in Oct 2025 to " in r["answer"]
    assert "in Sep 2026" in r["answer"] and "over 12 months" in r["answer"]
    assert "The biggest move was Jul 2026" in r["answer"]
    assert not any("2025-0" in e for e in r["evidence"])  # anomalies outside the window are not reported, nor ISO dates
    # with no window named, the whole history is charted and described
    r = ask(lyst, owner, "Show the operating margin trend over time")["result"]
    assert len(r["charts"][0]["x"]) == 33 and "over 33 months" in r["answer"]


def test_a_ranking_leads_with_the_members_asked_for(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Top 3 regions by operating income")["result"]
    q3 = _q(demo.pnl(), "2026-07-01", "2026-10-01").groupby("region").operating_income.sum().sort_values(ascending=False)
    assert r["answer"].startswith("Top 3 regions by operating income in Q3 2026: ")
    named = [region for region in q3.index if region in r["answer"]]
    assert named == list(q3.index[:3])  # the three largest, in order, and not the fourth
    assert r["answer"].index(q3.index[0]) < r["answer"].index(q3.index[1]) < r["answer"].index(q3.index[2])
    assert r["tables"][0]["title"] == "Operating income by region"
    # the lowest member of a lower-is-better metric, and "Highest" really is the largest value
    r = ask(lyst, owner, "Which channel had the lowest support tickets last quarter?")["result"]
    d = _q(demo.demand(), "2026-07-01", "2026-10-01").groupby("channel").tickets.sum()
    assert r["answer"].startswith(f"Lowest channel by support tickets in Q3 2026: {d.idxmin()} ")
    assert f"Highest: {d.idxmax()}" in r["evidence"][0]
    # "top line" is revenue, not a ranking
    r = ask(lyst, owner, "What was top line last quarter?")["result"]
    assert r["answer"].startswith("Revenue was ")


def test_a_filter_is_named_in_the_headline(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "How many tickets did chat handle in August 2026?")["result"]
    aug = demo.demand()
    aug = aug[(pd.to_datetime(aug.month) == "2026-08-01") & (aug.channel == "chat")].tickets.sum()
    assert r["answer"].startswith(f"Support tickets (chat) were {aug:,} in Aug 2026")


def test_a_what_if_starts_from_the_quarter_every_other_answer_calls_last(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What happens to operating margin if labor costs increase 6% while revenue grows 3%?")["result"]
    q3 = _q(demo.pnl(), "2026-07-01", "2026-10-01")
    base = q3.operating_income.sum() / q3.revenue.sum()
    assert r["answer"].startswith(f"Operating margin would go from {base * 100:.2f}% (Q3 2026 actual) to ")
    assert any("Q3 2026" in e and "data through 2026-09-30" in e for e in r["evidence"])
    assert "pnl_drivers" not in " ".join(r["actions"]) and "Quarterly P&L drivers" in r["actions"][0]
