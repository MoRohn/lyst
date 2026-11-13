# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Turn period phrases into half-open date ranges, anchored on the data, not the wall clock.

"Last quarter" means the last complete quarter in the data. The anchor is stated in the
answer as an assumption, so a reader sees which quarter was used.

``fs`` is the month a fiscal year starts (1, January, is the calendar year). With a fiscal year starting in, say,
April, quarters run Apr-Jun, Jul-Sep, ...; a fiscal year is named for the calendar year it ends in (FY2026 runs
April 2025 to March 2026), and "Q1 2026" means the first quarter of FY2026. "Last year", "this year" and "FY2026"
are fiscal years; "in 2026" stays the calendar year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class Period:
    label: str
    start: date
    end: date  # exclusive
    grain: str

    def iso(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()


def _q_start(d: date, fs: int = 1) -> date:
    k = (d.month - fs) % 3
    return _add_months(date(d.year, d.month, 1), -k)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _fy_end_year(d: date, fs: int) -> int:
    """The fiscal year (named for the calendar year it ends in) that contains ``d``."""
    return d.year if fs == 1 or d.month < fs else d.year + 1


def _quarter(start: date, fs: int = 1) -> Period:
    q = ((start.month - fs) % 12) // 3 + 1
    label = f"Q{q} {start.year}" if fs == 1 else f"Q{q} FY{_fy_end_year(start, fs)}"
    return Period(label, start, _add_months(start, 3), "quarter")


def _month(start: date) -> Period:
    return Period(start.strftime("%b %Y"), start, _add_months(start, 1), "month")


def _year(y: int) -> Period:
    return Period(str(y), date(y, 1, 1), date(y + 1, 1, 1), "year")


def _fiscal_year(fy: int, fs: int) -> Period:
    if fs == 1:
        return _year(fy)
    start = date(fy - 1, fs, 1)
    return Period(f"FY{fy}", start, _add_months(start, 12), "year")


def covered_through(max_value, max_day: int | None) -> date | None:
    """The last day the data covers. When every date is the first of a month (``max_day`` is 1), rows stand for
    whole months, so the data runs to the end of the last month: September rows dated 2026-09-01 cover September."""
    if max_value is None:
        return None
    ts = pd.Timestamp(max_value)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    if max_day == 1:
        ts = ts + pd.offsets.MonthEnd(0)
    return ts.date()


def last_complete(grain: str, max_date: date, fs: int = 1) -> Period:
    """The last period of ``grain`` that the data covers fully (max_date is the last day seen)."""
    nxt = pd.Timestamp(max_date) + pd.Timedelta(days=1)
    nd = nxt.date()
    if grain == "month":
        start = date(nd.year, nd.month, 1)
        return _month(_add_months(start, -1)) if nd.day != 1 else _month(_add_months(start, -1))
    if grain == "quarter":
        qs = _q_start(nd, fs)
        return _quarter(_add_months(qs, -3), fs)
    if grain == "year":
        return _fiscal_year(_fy_end_year(nd, fs) - 1, fs)
    raise ValueError(grain)


def previous(p: Period, fs: int = 1) -> Period:
    if p.grain == "quarter":
        return _quarter(_add_months(p.start, -3), fs)
    if p.grain == "month":
        return _month(_add_months(p.start, -1))
    if p.grain == "year":
        return _fiscal_year(_fy_end_year(p.start, fs) - 1, fs)
    raise ValueError(p.grain)


def year_ago(p: Period, fs: int = 1) -> Period:
    start = date(p.start.year - 1, p.start.month, 1)
    if p.grain == "quarter":
        return _quarter(start, fs)
    if p.grain == "month":
        return _month(start)
    return _fiscal_year(_fy_end_year(start, fs), fs)


_Q = re.compile(r"\bq([1-4])\s*(?:fy)?\s*'?(\d{2,4})\b", re.I)
_Y = re.compile(r"\b(?:in|for|during|fy)\s*(20\d{2})\b", re.I)
_FY = re.compile(r"\bfy\s*'?(20\d{2}|\d{2})\b", re.I)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_M = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(20\d{2})\b", re.I)


def quarter(q: int, y: int, fs: int = 1) -> Period:
    """Quarter ``q`` of year ``y`` ("Q2 2026"), read on the workspace's fiscal calendar like ``parse``."""
    if fs == 1:
        return _quarter(date(y, 3 * (q - 1) + 1, 1))
    return _quarter(_add_months(date(y - 1, fs, 1), 3 * (q - 1)), fs)


def parse(text: str, max_date: date, fs: int = 1) -> Period | None:
    t = text.lower()
    if m := _Q.search(t):
        y = int(m.group(2))
        return quarter(int(m.group(1)), y + 2000 if y < 100 else y, fs)
    if m := _M.search(t):
        return _month(date(int(m.group(2)), _MONTHS[m.group(1)[:3].lower()], 1))
    if fs != 1 and (m := _FY.search(t)):
        y = int(m.group(1))
        return _fiscal_year(y + 2000 if y < 100 else y, fs)
    if "last quarter" in t or "previous quarter" in t or "past quarter" in t:
        return last_complete("quarter", max_date, fs)
    if "this quarter" in t or "current quarter" in t:
        return _quarter(_q_start(max_date, fs), fs)
    if "last month" in t or "previous month" in t or "past month" in t:
        return last_complete("month", max_date)
    if "this month" in t:
        return _month(date(max_date.year, max_date.month, 1))
    if "last year" in t or "previous year" in t:
        return last_complete("year", max_date, fs)
    if "this year" in t or "ytd" in t or "year to date" in t:
        fy = _fiscal_year(_fy_end_year(max_date, fs), fs)
        return Period(f"{fy.label} YTD", fy.start, (pd.Timestamp(max_date) + pd.Timedelta(days=1)).date(), "ytd")
    if m := _Y.search(t):
        return _year(int(m.group(1)))
    return None
