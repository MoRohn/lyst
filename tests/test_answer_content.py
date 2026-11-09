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


def test_a_split_compared_with_a_year_earlier_names_each_members_change(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Compare hardware revenue by region year over year")
    r = t["result"]
    df = demo.pnl()
    hw = df[df.product_line == "Hardware"]
    now = _q(hw, "2026-07-01", "2026-10-01").groupby("region").revenue.sum()
    then = _q(hw, "2025-07-01", "2025-10-01").groupby("region").revenue.sum()
    change = (now - then).sort_values(ascending=False)
    assert r["confidence"]["level"] == "verified", r["answer"]  # it answers the comparison asked, so it is not flagged
    assert r["answer"].startswith("Hardware revenue by region, Q3 2026 versus Q3 2025: " + change.index[0])
    assert " In total " in r["answer"]
    chart = r["charts"][0]
    assert chart["type"] == "bar" and chart["x"] == list(change.index)
    assert chart["y"] == pytest.approx(list(change.values), rel=1e-9)
    table = r["tables"][0]
    assert table["columns"] == ["region", "Q3 2025", "Q3 2026", "change", "change_pct"]
    # quarter over quarter works the same way
    r = ask(lyst, owner, "Revenue by region versus the previous quarter")["result"]
    assert "Q3 2026 versus Q2 2026" in r["answer"]


def test_a_data_quality_question_checks_the_data_instead_of_listing_it(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Are there any data quality issues in the pnl dataset?")["result"]
    assert r.get("mode") != "conversation"
    assert r["answer"].startswith("pnl has ") and r["tables"][-1]["title"] == "Columns"
    # with no dataset named it asks which one, with the datasets as choices
    t = ask(lyst, owner, "Are there any duplicates in our data?")
    assert t["result"]["status"] == "needs_input" and "Name the dataset" in t["result"]["answer"]


def test_an_ambiguous_term_names_the_metrics_in_words(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Show me margin")["result"]
    assert r["status"] == "needs_input"
    assert r["answer"] == "“Margin” matches more than one approved metric: COGS ratio or operating margin. Pick the one you mean."


def test_a_rise_in_a_lower_is_better_metric_is_called_a_rise(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Why did support tickets increase in August 2026?")["result"]
    assert r["implications"][0].startswith("The rise is concentrated in channel chat")
    assert "pricing" not in " ".join(r["actions"])
    assert r["evidence"][0].startswith("The largest driver is channel chat")
    assert r["tables"][0]["title"] == "Change in support tickets by channel"


def test_year_to_date_is_compared_with_the_same_months_a_year_earlier(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What's the revenue split by product line this year?")["result"]
    df = demo.pnl()
    now, then = _q(df, "2026-01-01", "2026-10-01").revenue.sum(), _q(df, "2025-01-01", "2025-10-01").revenue.sum()
    assert r["answer"].startswith("Revenue was ") and "in 2026 YTD" in r["answer"]
    assert "versus 2025 YTD" in r["answer"] and "prior period" not in r["answer"]
    assert f"({(now - then) / then:+.1%})" in r["answer"]
    assert r["tables"][0]["title"] == "Revenue by product line"


def test_a_quarter_forecast_is_read_against_the_last_quarter(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Forecast revenue for next quarter")["result"]
    assert "for Q4 2026" in r["answer"] and r["answer"].endswith("against $25.41M in Q3 2026.")


def test_which_month_was_highest_names_the_month(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Which month had the highest revenue in 2025?")["result"]
    by_month = _q(demo.pnl(), "2025-01-01", "2026-01-01").groupby("month").revenue.sum()
    best = pd.Timestamp(by_month.idxmax()).strftime("%b %Y")
    assert r["answer"].startswith(f"{best} had the highest revenue in 2025: ")
    assert r["charts"][0]["title"] == "Revenue by month" and len(r["charts"][0]["x"]) == 12
