# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Fiscal calendars from the business profile: quarters and years follow the organization's fiscal year."""

from __future__ import annotations

from datetime import date

from lyst.analyst import periods
from tests.conftest import ask


def test_calendar_year_is_unchanged():
    assert periods.parse("revenue in Q2 2025", date(2026, 9, 30)).label == "Q2 2025"
    assert periods.last_complete("quarter", date(2026, 9, 30)).label == "Q3 2026"
    assert periods.parse("revenue this year", date(2026, 9, 30)).label == "2026 YTD"


def test_fiscal_year_starting_in_april():
    anchor = date(2026, 9, 30)
    q = periods.parse("revenue last quarter", anchor, 4)  # data runs through Sep 2026: FY2027 Q2 is Jul-Sep
    assert (q.label, q.start, q.end) == ("Q2 FY2027", date(2026, 7, 1), date(2026, 10, 1))
    assert periods.previous(q, 4).label == "Q1 FY2027"
    assert periods.year_ago(q, 4).label == "Q2 FY2026"
    q1 = periods.parse("revenue in Q1 2026", anchor, 4)
    assert (q1.label, q1.start, q1.end) == ("Q1 FY2026", date(2025, 4, 1), date(2025, 7, 1))
    fy = periods.parse("revenue for FY2026", anchor, 4)
    assert (fy.start, fy.end) == (date(2025, 4, 1), date(2026, 4, 1))
    assert periods.last_complete("year", anchor, 4).label == "FY2026"
    ytd = periods.parse("revenue year to date", anchor, 4)
    assert (ytd.label, ytd.start) == ("FY2027 YTD", date(2026, 4, 1))
    assert periods.parse("revenue in 2025", anchor, 4).label == "2025"  # "in 2025" stays the calendar year


def test_answers_use_the_profile_fiscal_calendar(seeded):
    lyst, owner = seeded
    lyst.onboarding.set_profile(owner, "northwind", {"fiscal_year_start_month": 4})
    t = ask(lyst, owner, "What was revenue last quarter?")
    assert t["status"] == "done" and "FY2027" in t["result"]["answer"], t["result"]["answer"]


def test_fiscal_buckets_in_compiled_metrics(seeded):
    """Quarter and year buckets follow a fiscal year that starts outside the calendar quarters (O6)."""
    from lyst.semantic import compile_metric

    lyst, owner = seeded
    spec = lyst.semantic.get(owner, "northwind", "revenue")
    assert compile_metric(spec, grain="quarter") == compile_metric(spec, grain="quarter", fiscal_start=1)  # calendar SQL unchanged
    assert compile_metric(spec, grain="month", fiscal_start=2) == compile_metric(spec, grain="month")
    q = lyst.data.query(owner, "northwind", compile_metric(spec, grain="quarter", fiscal_start=2))
    starts = {str(r[0])[:10] for r in q.rows}
    assert "2025-02-01" in starts and "2025-01-01" not in starts and all(s[5:7] in ("02", "05", "08", "11") for s in starts)
    # every month lands in exactly one fiscal quarter: totals agree with the calendar buckets
    cal = lyst.data.query(owner, "northwind", compile_metric(spec, grain="quarter"))
    assert abs(sum(r[1] for r in q.rows) - sum(r[1] for r in cal.rows)) < 1e-6
    y = lyst.data.query(owner, "northwind", compile_metric(spec, grain="year", fiscal_start=7))
    assert {str(r[0])[5:10] for r in y.rows} == {"07-01"}
    import pytest
    with pytest.raises(ValueError):
        compile_metric(spec, grain="quarter", fiscal_start=13)


def test_quarterly_horizon_uses_fiscal_quarters(seeded):
    import json
    import re

    lyst, owner = seeded
    lyst.onboarding.set_profile(owner, "northwind", {"fiscal_year_start_month": 2})
    t = ask(lyst, owner, "Investigate how operating margin changed over the last six quarters")
    assert t["status"] == "done" and "FY2027" in t["result"]["answer"]
    labels = set(re.findall(r"Q[1-4] (?:FY)?\d{4}", json.dumps(t["result"], default=str)))
    assert labels and all("FY" in lab for lab in labels), labels  # every quarter label in the deep investigation is fiscal


def test_next_quarter_forecast_is_the_next_fiscal_quarter(seeded):
    lyst, owner = seeded
    lyst.onboarding.set_profile(owner, "northwind", {"fiscal_year_start_month": 2})
    t = ask(lyst, owner, "Forecast support tickets for next quarter")
    assert t["status"] == "done"
    # data runs to Sep 2026; with years from February the next fiscal quarter is Nov 2026 - Jan 2027 (Q4 FY2027)
    assert "Q4 FY2027" in t["result"]["answer"], t["result"]["answer"]
