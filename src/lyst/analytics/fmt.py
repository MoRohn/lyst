# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Number formatting for dashboards and findings, with half-up rounding (Tableau and Excel round half away
from zero; Python's round() rounds half to even, so 0.125 -> "0.12" would disagree with a published screen).

    money(1_234_567)            -> "$1.23M"      money(812.4)          -> "$812"
    money(1_234_567, full=True) -> "$1,234,567"
    compact(48_213)             -> "48.2k"       kcount(489_520)       -> "490k"
    pct(0.1234)                 -> "12.3%"       signed_pct(-0.042)    -> "-4.2%"
    pts(0.0123)                 -> "+1.2 pts"    num(12345.6)          -> "12,346"
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

__all__ = ["change", "compact", "kcount", "money", "num", "pct", "pts", "round_half_up", "signed", "signed_pct"]

_UNITS = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k"))
DASH = "—"


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def round_half_up(x: float, digits: int = 0) -> float:
    """Round half away from zero: 2.5 -> 3, -2.5 -> -3, 0.125 -> 0.13 at 2 digits. A tiny negative that rounds
    to zero gives 0.0, not -0.0 (which would print as "-0.0%")."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP)) + 0.0


def num(x, digits: int = 0) -> str:
    """12345.6 -> "12,346"; with digits=1 -> "12,345.6"."""
    if _missing(x):
        return DASH
    return f"{round_half_up(x, digits):,.{digits}f}"


def compact(x, digits: int = 1, unit: str = "") -> str:
    """Short form for KPI values: 1_234_567 -> "1.2M", 950 -> "950", 48_213 -> "48.2k". Trailing ".0" is dropped."""
    if _missing(x):
        return DASH
    sign = "-" if x < 0 else ""
    a = abs(float(x))
    for size, suffix in _UNITS:
        if round_half_up(a / size, digits) >= 1:
            v = round_half_up(a / size, digits)
            if v >= 1000 and suffix != "T":  # 999_950 rounds to 1000.0k: step up a unit
                continue
            text = f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:.0f}"
            return f"{sign}{unit}{text}{suffix}"
    text = f"{round_half_up(a, 0):,.0f}" if a >= 10 or a == 0 else f"{round_half_up(a, digits):.{digits}f}".rstrip("0").rstrip(".")
    return f"{sign}{unit}{text}"


def kcount(n) -> str:
    """Counts in summary tables: whole thousands from 10,000 ("490k"), one decimal below ("2.1k"), "<0.1k" for a
    small non-zero count so a tiny group does not read as none, "0" for zero."""
    if _missing(n):
        return DASH
    n = float(n)
    if n == 0:
        return "0"
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 10_000:
        return f"{sign}{round_half_up(a / 1000, 0):,.0f}k"
    v = round_half_up(a / 1000, 1)
    if v == 0:
        return f"{sign}<0.1k"
    return f"{sign}{v:.1f}k"


def money(x, digits: int | None = None, *, full: bool = False, symbol: str = "$") -> str:
    """$1.23M / $48.2k / $812; full=True -> "$1,234,567". Negative -> "-$1.2k"."""
    if _missing(x):
        return DASH
    if full:
        d = 0 if digits is None else digits
        s = f"{round_half_up(abs(x), d):,.{d}f}"
        return f"{'-' if x < 0 else ''}{symbol}{s}"
    a = abs(x)
    d = digits if digits is not None else (2 if 1e6 <= a < 1e7 else 1)
    return compact(x, d, unit=symbol)


def pct(x, digits: int = 1) -> str:
    """Ratio -> percent: 0.1234 -> "12.3%"."""
    if _missing(x):
        return DASH
    return f"{round_half_up(x * 100, digits):.{digits}f}%"


def signed_pct(x, digits: int = 1) -> str:
    """Relative change with an explicit sign: 0.042 -> "+4.2%", 0 -> "0.0%"."""
    if _missing(x):
        return DASH
    v = round_half_up(x * 100, digits)
    return f"{'+' if v > 0 else ''}{v:.{digits}f}%"


def pts(x, digits: int = 1) -> str:
    """A change between two rates, in percentage points: 0.0123 -> "+1.2 pts"."""
    if _missing(x):
        return DASH
    v = round_half_up(x * 100, digits)
    return f"{'+' if v > 0 else ''}{v:.{digits}f} pts"


def signed(text_fn, x) -> str:
    """Prefix "+" to a formatter's output for positive values: signed(money, 1200) -> "+$1.2k"."""
    if _missing(x):
        return DASH
    s = text_fn(x)
    return f"+{s}" if x > 0 else s


def change(current, prior):
    """Relative change, or None when there is no prior value to compare with."""
    if _missing(current) or _missing(prior) or prior == 0:
        return None
    return (current - prior) / abs(prior)
