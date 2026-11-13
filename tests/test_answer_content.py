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
    # with no dataset named it checks every dataset the asker can read (quality_review.py)
    t = ask(lyst, owner, "Are there any duplicates in our data?")
    assert t["result"]["status"] == "complete" and t["result"]["mode"] == "quality_review"


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


def test_which_member_grew_fastest_is_ranked_by_growth(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Which product line grew revenue fastest this year?")["result"]
    df = demo.pnl()
    now = _q(df, "2026-01-01", "2026-10-01").groupby("product_line").revenue.sum()
    then = _q(df, "2025-01-01", "2025-10-01").groupby("product_line").revenue.sum()
    growth = ((now - then) / then).sort_values(ascending=False)
    assert r["answer"].startswith(f"Fastest growth in revenue by product line, 2026 YTD versus 2025 YTD: {growth.index[0]} "
                                  f"{growth.iloc[0]:+.1%}")
    assert r["confidence"]["level"] == "verified"
    assert r["charts"][0]["x"] == list(growth.index)  # the chart runs in the same order


def test_a_trend_by_region_draws_a_line_per_region(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Show revenue trend by region")["result"]
    df = demo.pnl()
    chart = r["charts"][0]
    assert chart["title"] == "Revenue by month and region" and len(chart["x"]) == 33
    by = {s["name"]: s["y"] for s in chart["series"]}
    assert set(by) == set(df.region.unique())
    last = df[pd.to_datetime(df.month) == "2026-09-01"].groupby("region").revenue.sum()
    assert {k: v[-1] for k, v in by.items()} == pytest.approx(last.to_dict(), rel=1e-9)
    # the total stays as the chart's single line for viewers that draw one, and the headline names the extremes
    assert chart["y"][-1] == pytest.approx(last.sum(), rel=1e-9)
    first = df[pd.to_datetime(df.month) == "2024-01-01"].groupby("region").revenue.sum()
    growth = ((last - first) / first).sort_values()
    assert f"highest growth in {growth.index[-1]} ({growth.iloc[-1]:+.1%}), lowest in {growth.index[0]}" in r["answer"]


def test_a_correlation_draws_both_series_on_one_indexed_chart(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Is revenue correlated with support tickets?")["result"]
    chart = r["charts"][0]
    assert chart["type"] == "line" and "indexed to 100 in Jan 2024" in chart["title"]
    assert [s["y"][0] for s in chart["series"]] == pytest.approx([100.0, 100.0])
    assert len(chart["series"]) == 2 and len(chart["x"]) == 33


def test_downloads_draw_every_line(seeded):
    import io

    from openpyxl import load_workbook
    from pptx import Presentation
    from pypdf import PdfReader

    lyst, owner = seeded
    t = ask(lyst, owner, "Show revenue trend by region")
    regions = set(demo.pnl().region.unique())
    _, xlsx = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "xlsx")["id"])
    wb = load_workbook(io.BytesIO(xlsx))
    ws = next(wb[n] for n in wb.sheetnames if n.startswith("Revenue by month and"))
    assert len(ws._charts[0].series) == len(regions)
    _, pptx = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "pptx")["id"])
    chart = next(sh.chart for s in Presentation(io.BytesIO(pptx)).slides for sh in s.shapes if sh.has_chart)
    assert {s.name for s in chart.plots[0].series} == regions and chart.has_legend
    _, pdf = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "pdf")["id"])
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert all(region in text for region in regions)


def test_a_segment_ratio_investigation_verifies_and_keeps_the_acronym(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Investigate why cogs ratio in EMEA changed in Q3 2026 and test alternative explanations")
    r = t["result"]
    checks = {c["name"]: c for c in r["confidence"]["checks"]}
    assert checks["parts_reconcile"]["passed"], checks["parts_reconcile"]
    assert not r["answer"].startswith("Not verified") and "cOGS" not in r["answer"]
    assert "COGS ratio (EMEA)" in r["answer"]
    # the history behind it is EMEA's, not the whole company's
    quarters = next(c for c in r["charts"] if c["title"] == "COGS ratio (EMEA) by quarter")
    assert f"was {quarters['y'][-1]:.1%} in Q3 2026" in r["answer"]


def _studio(lyst, owner):
    """Daily invoices by client and category through 2026-10-12: October is only partly in the data."""
    import numpy as np

    from lyst.semantic import Measure, MetricSpec

    rng = np.random.default_rng(7)
    days = pd.date_range("2024-01-01", "2026-10-12", freq="D")
    clients = {"Acme Co": 900, "Bluebird Cafe": 300, "Corner Dental": 150}
    rows = [{"day": d.date(), "client": c, "category": cat, "income": round(base * float(rng.uniform(0.8, 1.2)), 2)}
            for d in days for c, base in clients.items() for cat in ("Design services", "Hosting")]
    lyst.data.ingest(owner, "northwind", "studio", demo.csv_bytes(pd.DataFrame(rows)), filename="studio.csv")
    lyst.semantic.define(owner, "northwind", MetricSpec(
        name="studio_income", title="Studio income", dataset="studio", measure=Measure(agg="sum", column="income"),
        time_column="day", dimensions=["client", "category"], format="currency"))


@pytest.mark.parametrize("question,by,first", [
    ("Which clients bring in the most studio income?", "client", "Acme Co"),
    ("What are the biggest categories of studio income?", "category", "Design services"),
    ("Which client brings in the least studio income?", "client", "Corner Dental")])
def test_plural_and_most_least_rank_by_the_dimension(seeded, question, by, first):
    lyst, owner = seeded
    _studio(lyst, owner)
    r = ask(lyst, owner, question)["result"]
    assert r["tables"] and r["tables"][0]["columns"][0] == by, r["answer"]
    assert first in r["answer"].split(":", 1)[-1][:60], r["answer"]


def test_a_partial_month_is_left_out_of_a_forecast(seeded):
    lyst, owner = seeded
    _studio(lyst, owner)
    r = ask(lyst, owner, "Forecast studio income for the next quarter")["result"]
    # read against the last complete quarter, not a few days of October
    assert "Oct 2026" not in r["answer"] and r["answer"].endswith("in Q3 2026."), r["answer"]
    assert any("Oct 2026 is left out" in a for a in r["supporting"]["assumptions"])
    chart = next(c for c in r["charts"] if c["type"] == "forecast")
    assert str(chart["history_x"][-1])[:7] == "2026-09"


def test_a_why_answer_leads_with_each_members_contribution(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Why did operating margin decline last quarter?")["result"]
    bar = r["charts"][0]
    assert bar["type"] == "bar" and bar["title"].startswith("What moved operating margin: each region's contribution")
    assert bar["x"][0] == "EMEA" and bar["y"][0] < 0  # the largest move first, with its sign
    table = next(t for t in r["tables"] if t["title"].startswith("Change in operating margin"))
    contribution = {row[0]: row[-1] for row in table["rows"]}
    assert dict(zip(bar["x"], bar["y"], strict=True)) == pytest.approx(contribution)


def test_next_quarters_are_forecast_quarter_by_quarter(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Forecast revenue for the next 2 quarters")["result"]
    assert r["answer"].startswith("Lyst forecasts revenue of $") and " in Q4 2026 (80% interval " in r["answer"]
    assert ", then $" in r["answer"] and " in Q1 2027 " in r["answer"] and r["answer"].endswith("in Q3 2026.")
    chart = next(c for c in r["charts"] if c["type"] == "forecast")
    assert [str(x)[:7] for x in chart["x"]] == ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03"]
    m = ask(lyst, owner, "Forecast operating margin for the next 2 quarters")["result"]
    assert "averaging " in m["answer"] and "against an average of 32.5% in Q3 2026" in m["answer"]
    n = ask(lyst, owner, "Forecast revenue for the next 9 months")["result"]
    assert "by Jun 2027" in n["answer"]  # a month name, not 2027-06-01


@pytest.mark.parametrize(("question", "earlier"), [("Compare revenue by region for Q2 and Q3 2026", "Q2 2026"),
                                                   ("Compare revenue by region for Q1 2026 vs Q3 2026", "Q1 2026")])
def test_two_named_quarters_compare_each_member(seeded, question, earlier):
    lyst, owner = seeded
    r = ask(lyst, owner, question)["result"]
    assert r["answer"].startswith(f"Revenue by region, Q3 2026 versus {earlier}: ")
    df = demo.pnl()
    q3 = _q(df, "2026-07-01", "2026-10-01").groupby("region").revenue.sum()
    start = {"Q2 2026": "2026-04-01", "Q1 2026": "2026-01-01"}[earlier]
    base = _q(df, start, pd.Timestamp(start) + pd.DateOffset(months=3)).groupby("region").revenue.sum()
    chart = next(c for c in r["charts"] if c["title"] == f"Change in revenue by region, Q3 2026 versus {earlier}")
    assert dict(zip(chart["x"], chart["y"], strict=True)) == pytest.approx((q3 - base).to_dict())


def test_a_year_over_year_why_explains_the_change_against_a_year_earlier(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What drove revenue growth year over year?")["result"]
    assert r["answer"].startswith("Revenue was $25.41M in Q3 2026, +$2.69M (+11.8%) versus Q3 2025")
    assert r["charts"][0]["title"].endswith("contribution, Q3 2025 to Q3 2026")
    assert r["confidence"]["level"] == "verified"  # the bridge closes against the year-earlier quarter


def test_growth_with_no_metric_named_reads_as_revenue_and_says_so(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Which region grew fastest this year?")["result"]
    assert r["answer"].startswith("Fastest growth in revenue by region, 2026 YTD versus 2025 YTD: ")
    assert any(a.startswith("No metric was named, so Lyst read the growth as revenue growth")
               for a in r["supporting"]["assumptions"])


def test_a_significance_question_is_answered_against_the_metrics_own_history(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Is the drop in operating margin statistically significant?")["result"]
    assert "Is it significant? The -3.5 pts move is " in r["answer"] and "unlikely to be ordinary variation" in r["answer"]
    assert any("not a formal hypothesis test" in a for a in r["supporting"]["assumptions"])


def test_a_plain_figure_does_not_carry_the_specialist_checklist(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "How many support tickets did we get last quarter?")["result"]
    assert not any("checks Lyst does not run automatically" in a for a in r["supporting"]["assumptions"])


def test_a_what_if_on_something_no_model_has_says_what_can_change(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What if EMEA costs fell 10%?")["result"]
    assert r["status"] == "needs_input"
    assert r["answer"].startswith("The what-if model Quarterly P&L drivers has nothing called “EMEA costs” to change.")
    assert "operating expenses" in r["answer"] and "company totals" in r["answer"]
    # the same change on something the model has, in the past tense, is applied
    r = ask(lyst, owner, "What if operating expenses fell 10%?")["result"]
    assert "Assumed: Operating expenses -10%" in r["answer"]


def test_how_many_staff_is_answered_with_a_headcount(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "How many staff do we need next quarter?")["result"]
    assert r["answer"].startswith("Lyst forecasts support tickets of ")
    assert "Keeping the risk of falling short under 5% takes " in r["answer"] and " agents, against 43 now." in r["answer"]
    assert any(a.startswith("No demand was named, so staffing is planned against Support tickets")
               for a in r["supporting"]["assumptions"])
