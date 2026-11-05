# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Locale-aware formatting and accessibility helpers that every surface (briefings, publications, the interface,
generated dashboards) can share, so a number reads the same way everywhere for a given reader.

No dependency: a small table of locales (separators, grouping, currency placement, month names, compact units)
covers the languages Lyst is used in; an unknown locale falls back to its language and then to en-US.
``LYST_LOCALE`` sets the installation default. Accessibility: WCAG contrast checks for chart and badge colours,
and plain-language descriptions of tables and series for screen readers and alt text.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Locale:
    tag: str
    group: str
    decimal: str
    currency: str        # pattern with {s} symbol and {n} number
    percent: str         # pattern with {n}
    months: tuple[str, ...]
    compact: tuple[str, str, str]  # thousand, million, billion
    india: bool = False  # 12,34,567 grouping
    date_long: str = "{d} {m} {y}"
    date_short: str = "{y}-{mm}-{dd}"


_EN = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
LOCALES: dict[str, Locale] = {
    "en-US": Locale("en-US", ",", ".", "{s}{n}", "{n}%", _EN, ("K", "M", "B"), date_long="{m} {d}, {y}", date_short="{mm}/{dd}/{y}"),
    "en-GB": Locale("en-GB", ",", ".", "{s}{n}", "{n}%", _EN, ("k", "m", "bn"), date_short="{dd}/{mm}/{y}"),
    "de-DE": Locale("de-DE", ".", ",", "{n} {s}", "{n} %", ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
                    "August", "September", "Oktober", "November", "Dezember"), ("Tsd.", "Mio.", "Mrd."), date_long="{d}. {m} {y}",
                    date_short="{dd}.{mm}.{y}"),
    "fr-FR": Locale("fr-FR", " ", ",", "{n} {s}", "{n} %", ("janvier", "février", "mars", "avril", "mai", "juin",
                    "juillet", "août", "septembre", "octobre", "novembre", "décembre"), ("k", "M", "Md"), date_short="{dd}/{mm}/{y}"),
    "es-ES": Locale("es-ES", ".", ",", "{n} {s}", "{n} %", ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                    "agosto", "septiembre", "octubre", "noviembre", "diciembre"), ("mil", "M", "mil M"), date_long="{d} de {m} de {y}",
                    date_short="{dd}/{mm}/{y}"),
    "pt-BR": Locale("pt-BR", ".", ",", "{s} {n}", "{n}%", ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
                    "agosto", "setembro", "outubro", "novembro", "dezembro"), ("mil", "mi", "bi"), date_long="{d} de {m} de {y}",
                    date_short="{dd}/{mm}/{y}"),
    "ja-JP": Locale("ja-JP", ",", ".", "{s}{n}", "{n}%", tuple(f"{i}月" for i in range(1, 13)), ("千", "百万", "十億"),
                    date_long="{y}年{m}{d}日", date_short="{y}/{mm}/{dd}"),
    "hi-IN": Locale("hi-IN", ",", ".", "{s}{n}", "{n}%", _EN, ("K", "M", "B"), india=True, date_short="{dd}/{mm}/{y}"),
}
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹", "BRL": "R$", "CHF": "CHF", "CAD": "CA$", "AUD": "A$"}
MINUS = "−"  # a real minus sign: screen readers say "minus", not "dash"


def locale(tag: str | None = None) -> Locale:
    tag = (tag or os.environ.get("LYST_LOCALE") or "en-US").replace("_", "-")
    if tag in LOCALES:
        return LOCALES[tag]
    lang = tag.split("-")[0].lower()
    return next((loc for t, loc in LOCALES.items() if t.split("-")[0] == lang), LOCALES["en-US"])


def _group(int_part: str, loc: Locale) -> str:
    if len(int_part) <= 3:
        return int_part
    if loc.india:
        head, tail = int_part[:-3], int_part[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        return loc.group.join(parts + [tail])
    parts = []
    while int_part:
        parts.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    return loc.group.join(parts)


def number(x: Any, digits: int = 0, tag: str | None = None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    from decimal import ROUND_HALF_UP, Decimal  # noqa: PLC0415 - half up, as people round (2.5 -> 3), not to even

    loc = locale(tag)
    d = Decimal(str(abs(float(x)))).quantize(Decimal(1).scaleb(-max(digits, 0)), rounding=ROUND_HALF_UP)
    text = f"{d:.{digits}f}"
    int_part, _, frac = text.partition(".")
    out = _group(int_part, loc) + (loc.decimal + frac if frac else "")
    return (MINUS + out) if float(x) < 0 and float(d) != 0 else out


def currency(x: Any, code: str = "USD", digits: int | None = None, tag: str | None = None) -> str:
    loc = locale(tag)
    if x is None:
        return "–"
    digits = (0 if code == "JPY" else 2) if digits is None else digits
    n = number(abs(float(x)), digits, tag)
    body = loc.currency.format(s=SYMBOLS.get(code, code), n=n)
    return (MINUS + body) if float(x) < 0 else body


def percent(x: Any, digits: int = 1, tag: str | None = None) -> str:
    """``x`` is a fraction: 0.125 -> 12.5%."""
    if x is None:
        return "–"
    return locale(tag).percent.format(n=number(float(x) * 100, digits, tag))


def compact(x: Any, digits: int = 1, tag: str | None = None) -> str:
    if x is None:
        return "–"
    loc = locale(tag)
    v = abs(float(x))
    for scale, unit in ((1e9, loc.compact[2]), (1e6, loc.compact[1]), (1e3, loc.compact[0])):
        if v >= scale:
            sep = "" if loc.tag.startswith(("en", "ja", "hi")) and len(unit) <= 2 else " "
            s = number(v / scale, digits, tag).rstrip("0").rstrip(loc.decimal) if digits else number(v / scale, 0, tag)
            return (MINUS if float(x) < 0 else "") + s + sep + unit
    return number(x, 0, tag)


def date(d: dt.date | dt.datetime | str, style: str = "long", tag: str | None = None) -> str:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    loc = locale(tag)
    pattern = loc.date_long if style == "long" else loc.date_short
    return pattern.format(d=d.day, m=loc.months[d.month - 1], y=d.year, dd=f"{d.day:02d}", mm=f"{d.month:02d}")


# -- accessibility -------------------------------------------------------------------------

def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((_lum(fg), _lum(bg)), reverse=True)
    return round((a + 0.05) / (b + 0.05), 2)


def passes_aa(fg: str, bg: str, *, large: bool = False, graphic: bool = False) -> bool:
    """WCAG 2.2 AA: 4.5:1 for text, 3:1 for large text and for graphics such as chart marks."""
    return contrast(fg, bg) >= (3.0 if large or graphic else 4.5)


def describe_series(name: str, periods: list[Any], values: list[float], *, fmt=None, tag: str | None = None) -> str:
    """One sentence a screen reader can say for a line chart: where it starts, ends, peaks and how it moved."""
    pts = [(p, v) for p, v in zip(periods, values, strict=False) if v is not None]
    if not pts:
        return f"{name}: no data."
    f = fmt or (lambda v: number(v, 0, tag))

    def label(p: Any) -> str:
        return date(p, "long", tag) if isinstance(p, dt.date | dt.datetime) else str(p)

    (p0, v0), (p1, v1) = pts[0], pts[-1]
    hi = max(pts, key=lambda t: t[1])
    lo = min(pts, key=lambda t: t[1])
    if len(pts) == 1:
        return f"{name} was {f(v0)} in {label(p0)}."
    change = (v1 - v0) / abs(v0) if v0 else None
    verb = "rose" if v1 > v0 else "fell" if v1 < v0 else "was unchanged"
    how = f" ({percent(abs(change), 0, tag)})" if change is not None and verb != "was unchanged" else ""
    text = f"{name} {verb} from {f(v0)} in {label(p0)} to {f(v1)} in {label(p1)}{how}"
    extremes = []
    if hi[0] not in (p0, p1):
        extremes.append(f"peaking at {f(hi[1])} in {label(hi[0])}")
    if lo[0] not in (p0, p1) and lo[0] != hi[0]:
        extremes.append(f"lowest at {f(lo[1])} in {label(lo[0])}")
    return text + (", " + " and ".join(extremes) if extremes else "") + f", over {len(pts)} points."


def describe_table(title: str, columns: list[str], rows: list[list[Any]], *, max_rows: int = 3) -> str:
    """Alt text for a table: its size, its columns and its first rows read out."""
    head = f"{title}: a table of {len(rows)} row{'s' if len(rows) != 1 else ''} and {len(columns)} columns ({', '.join(map(str, columns))})."
    if not rows:
        return head
    reads = ["; ".join(f"{c} {v}" for c, v in zip(columns, r, strict=False)) for r in rows[:max_rows]]
    return head + " " + " | ".join(reads) + ("." if len(rows) <= max_rows else f", and {len(rows) - max_rows} more.")


__all__ = ["LOCALES", "compact", "contrast", "currency", "date", "describe_series", "describe_table", "locale", "number",
           "passes_aa", "percent"]
