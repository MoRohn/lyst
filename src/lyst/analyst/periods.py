"""Turn period phrases into half-open date ranges, anchored on the data, not the wall clock.

"Last quarter" means the last complete quarter in the data. The anchor is stated in the
answer as an assumption, so a reader sees which quarter was used.
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


def _q_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _quarter(start: date) -> Period:
    q = (start.month - 1) // 3 + 1
    return Period(f"Q{q} {start.year}", start, _add_months(start, 3), "quarter")


def _month(start: date) -> Period:
    return Period(start.strftime("%b %Y"), start, _add_months(start, 1), "month")


def _year(y: int) -> Period:
    return Period(str(y), date(y, 1, 1), date(y + 1, 1, 1), "year")


def last_complete(grain: str, max_date: date) -> Period:
    """The last period of ``grain`` that the data covers fully (max_date is the last day seen)."""
    nxt = pd.Timestamp(max_date) + pd.Timedelta(days=1)
    nd = nxt.date()
    if grain == "month":
        start = date(nd.year, nd.month, 1)
        return _month(_add_months(start, -1)) if nd.day != 1 else _month(_add_months(start, -1))
    if grain == "quarter":
        qs = _q_start(nd)
        return _quarter(_add_months(qs, -3))
    if grain == "year":
        return _year(nd.year - 1)
    raise ValueError(grain)


def previous(p: Period) -> Period:
    if p.grain == "quarter":
        return _quarter(_add_months(p.start, -3))
    if p.grain == "month":
        return _month(_add_months(p.start, -1))
    if p.grain == "year":
        return _year(p.start.year - 1)
    raise ValueError(p.grain)


def year_ago(p: Period) -> Period:
    start = date(p.start.year - 1, p.start.month, 1)
    return {"quarter": _quarter, "month": _month}.get(p.grain, lambda s: _year(s.year))(start)


_Q = re.compile(r"\bq([1-4])\s*(?:fy)?\s*'?(\d{2,4})\b", re.I)
_Y = re.compile(r"\b(?:in|for|during|fy)\s*(20\d{2})\b", re.I)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_M = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(20\d{2})\b", re.I)


def parse(text: str, max_date: date) -> Period | None:
    t = text.lower()
    if m := _Q.search(t):
        y = int(m.group(2))
        y = y + 2000 if y < 100 else y
        return _quarter(date(y, 3 * (int(m.group(1)) - 1) + 1, 1))
    if m := _M.search(t):
        return _month(date(int(m.group(2)), _MONTHS[m.group(1)[:3].lower()], 1))
    if "last quarter" in t or "previous quarter" in t or "past quarter" in t:
        return last_complete("quarter", max_date)
    if "this quarter" in t or "current quarter" in t:
        return _quarter(_q_start(max_date))
    if "last month" in t or "previous month" in t or "past month" in t:
        return last_complete("month", max_date)
    if "this month" in t:
        return _month(date(max_date.year, max_date.month, 1))
    if "last year" in t or "previous year" in t:
        return last_complete("year", max_date)
    if "this year" in t or "ytd" in t or "year to date" in t:
        return Period(f"{max_date.year} YTD", date(max_date.year, 1, 1),
                      (pd.Timestamp(max_date) + pd.Timedelta(days=1)).date(), "ytd")
    if m := _Y.search(t):
        return _year(int(m.group(1)))
    return None
