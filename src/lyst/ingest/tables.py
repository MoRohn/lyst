# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Turning a grid of cells (from a sheet, a document table, a web page) into a typed, honest table.

Shared by every reader in this package. A grid is a list of rows of Python values. ``locate_tables`` finds the
rectangular tables inside it (blank rows and columns separate them; title lines above and notes below are kept
as context, not data), ``shape_table`` picks the header, folds a two-row header, drops and checks total rows,
reads units out of headers and types each column.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..data import normalise_name

ERRORS = {"#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!", "#SPILL!", "#CALC!", "#GETTING_DATA"}
_TOTAL = re.compile(r"^\s*(grand\s+|sub[\s-]?)?totals?\b|^\s*sum\b|^\s*all\s+(regions|products|items)\b", re.I)
_UNIT = re.compile(r"\s*[\(\[]\s*(?P<unit>[^\)\]]{1,24})\s*[\)\]]\s*$")
_CURRENCY = {"$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "¥": "JPY", "jpy": "JPY"}
_SCALE = {"k": 1e3, "thousands": 1e3, "000s": 1e3, "'000": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "millions": 1e6,
          "bn": 1e9, "b": 1e9, "billions": 1e9}


def blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and v != v)


def is_text(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip()) and not _looks_numeric(v)


def _looks_numeric(s: str) -> bool:
    return bool(re.fullmatch(r"\s*[-+(]?[$€£¥]?\s*[\d.,]+\s*%?\)?\s*", s)) and any(ch.isdigit() for ch in s)


@dataclass
class Region:
    """A rectangle of the source grid: rows [top, bottom), columns [left, right)."""

    top: int
    bottom: int
    left: int
    right: int
    title: str | None = None
    notes: list[str] = field(default_factory=list)


def locate_tables(grid: list[list[Any]], *, min_rows: int = 2, min_cols: int = 1) -> list[Region]:
    """Blocks of non-blank cells, separated by fully blank rows, then by fully blank columns inside each block.

    A one-cell line directly above a block (and itself separated from what precedes it) is the block's title; one-cell
    lines below the data are notes. Neither is data."""
    if not grid:
        return []
    width = max((len(r) for r in grid), default=0)
    rows = [list(r) + [None] * (width - len(r)) for r in grid]
    filled = [sum(0 if blank(v) else 1 for v in r) for r in rows]
    blocks: list[tuple[int, int]] = []
    start = None
    for i, n in enumerate(filled + [0]):
        if n and start is None:
            start = i
        elif not n and start is not None:
            blocks.append((start, i))
            start = None
    regions: list[Region] = []
    pending_title: str | None = None
    for top, bottom in blocks:
        # a lone line (one block of one row with one value) is a title for the next block
        if bottom - top == 1 and filled[top] == 1 and width > 1:
            pending_title = next(str(v).strip() for v in rows[top] if not blank(v))
            continue
        # leading one-value lines inside the block are its title (a title right above a header row)
        title_lines: list[str] = []
        while top < bottom - 1 and filled[top] == 1 and _widest(rows, top + 1, bottom) > 1:
            title_lines.append(next(str(v).strip() for v in rows[top] if not blank(v)))
            top += 1
        title = " · ".join(([pending_title] if pending_title else []) + title_lines) or None
        pending_title = None
        # trailing one-value text lines are notes
        notes: list[str] = []
        while bottom - 1 > top + 1 and filled[bottom - 1] == 1:
            only = next(v for v in rows[bottom - 1] if not blank(v))
            if not is_text(only) or _TOTAL.match(str(only)):
                break
            notes.insert(0, str(only).strip())
            bottom -= 1
        for left, right in _column_runs(rows, top, bottom, width):
            if bottom - top >= min_rows and right - left >= min_cols:
                regions.append(Region(top, bottom, left, right, title, notes))
    if pending_title and regions:  # a lone line after the last table is its footnote ("Source: ...")
        regions[-1].notes.append(pending_title)
    return regions


def _widest(rows, top, bottom) -> int:
    return max((sum(0 if blank(v) else 1 for v in rows[i]) for i in range(top, bottom)), default=0)


def _column_runs(rows, top, bottom, width) -> list[tuple[int, int]]:
    used = [any(not blank(rows[i][j]) for i in range(top, bottom)) for j in range(width)]
    runs, start = [], None
    for j, u in enumerate(used + [False]):
        if u and start is None:
            start = j
        elif not u and start is not None:
            runs.append((start, j))
            start = None
    return runs


@dataclass
class Shaped:
    frame: pd.DataFrame
    columns: list[dict[str, Any]]
    header_rows: int
    totals: list[dict[str, Any]]
    warnings: list[str]


def _header_score(row: list[Any]) -> float:
    vals = [v for v in row if not blank(v)]
    if not vals:
        return 0.0
    return sum(1 for v in vals if is_text(v)) / len(row)


def shape_table(cells: list[list[Any]], *, formulas: list[list[str | None]] | None = None,
                merged_header: bool = False) -> Shaped:
    """Header, columns, types, units, totals for one located table. ``formulas`` mirrors ``cells`` with the
    formula text of each cell (or None) so derived columns are reported, not silently flattened."""
    warnings: list[str] = []
    width = max(len(r) for r in cells)
    cells = [list(r) + [None] * (width - len(r)) for r in cells]
    if formulas is not None:
        formulas = [list(r) + [None] * (width - len(r)) for r in formulas]
    header_rows = 1 if _header_score(cells[0]) >= 0.5 else 0
    # a second header row: all text, the first header had gaps (merged groups), and data follows
    if (header_rows and len(cells) > 3 and _header_score(cells[1]) >= 0.5
            and all(not isinstance(v, (int, float)) or isinstance(v, bool) for v in cells[1])
            and (merged_header or any(blank(v) for v in cells[0]))
            and any(isinstance(v, (int, float, dt.date)) for v in cells[2])):
        header_rows = 2
    if header_rows == 0:
        names = [f"column_{j + 1}" for j in range(width)]
        sources = list(names)
        warnings.append("No header row was found, so the columns are named column_1, column_2 and so on.")
    elif header_rows == 1:
        sources = [("" if blank(v) else str(v).strip()) for v in cells[0]]
    else:
        parent, out = "", []
        for a, b in zip(cells[0], cells[1], strict=True):
            parent = str(a).strip() if not blank(a) else parent
            child = "" if blank(b) else str(b).strip()
            out.append(f"{parent} {child}".strip() if parent and child and parent != child else (child or parent))
        sources = out
    if header_rows:
        names = []
        for j, s in enumerate(sources):
            names.append(s or f"column_{j + 1}")
            if not s:
                warnings.append(f"Column {j + 1} has no header; it is named column_{j + 1}.")
    body = cells[header_rows:]
    body_f = formulas[header_rows:] if formulas is not None else None

    # total rows: a label that says total, at the end or inside (subtotals); removed from the data and checked
    totals: list[dict[str, Any]] = []
    keep: list[int] = []
    for i, row in enumerate(body):
        label = next((v for v in row if not blank(v)), None)
        if isinstance(label, str) and _TOTAL.match(label):
            totals.append({"row": i + header_rows, "label": label.strip(), "values": row})
        else:
            keep.append(i)
    body = [body[i] for i in keep]
    if body_f is not None:
        body_f = [body_f[i] for i in keep]

    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    data: dict[str, list[Any]] = {}
    errors = 0
    for j, raw in enumerate(names):
        label, unit, scale = split_unit(raw)
        name = normalise_name(label) or f"column_{j + 1}"
        base, k = name, 2
        while name in seen:
            name, k = f"{base}_{k}", k + 1
        seen.add(name)
        values = []
        for row in body:
            v = row[j]
            if isinstance(v, str) and v.strip() in ERRORS:
                errors += 1
                v = None
            values.append(v)
        typed, kind = coerce(values)
        data[name] = typed
        col: dict[str, Any] = {"name": name, "header": raw, "type": kind}
        if unit:
            col["unit"] = unit
        if scale:
            col["scale"] = scale
        if body_f is not None:
            fs = [r[j] for r in body_f if r[j]]
            if fs and len(fs) >= max(1, len(body_f) // 2):
                col["formula"] = generalise_formula(fs[0])
                col["derived"] = True
                missing = sum(1 for v, f in zip(values, (r[j] for r in body_f), strict=True) if f and v is None)
                if missing:
                    warnings.append(f"{missing} cells in {name} hold formulas with no saved result (the file was never "
                                    "recalculated); open and save it in a spreadsheet app, or those cells stay empty.")
        nulls = sum(1 for v in typed if v is None or (isinstance(v, float) and v != v))
        if body and nulls == len(body):
            col["empty"] = True
        columns.append(col)
    if errors:
        warnings.append(f"{errors} cells held spreadsheet errors such as #DIV/0! or #N/A; they are loaded as empty.")
    frame = pd.DataFrame(data)
    # drop columns that are entirely empty and unnamed (padding from merged or formatted cells)
    for c in [c for c in columns if c.get("empty") and c["header"].startswith("column_")]:
        frame = frame.drop(columns=[c["name"]])
        columns.remove(c)
    for t in totals:
        t.update(check_total(t, columns, frame, names))
        del t["values"]
    return Shaped(frame, columns, header_rows, totals, warnings)


def split_unit(header: str) -> tuple[str, str | None, float | None]:
    """'Revenue (USD m)' -> ('Revenue', 'USD', 1e6); 'Margin %' -> ('Margin', '%', None)."""
    text = header.strip()
    unit, scale = None, None
    m = _UNIT.search(text)
    if m:
        inner = m.group("unit").strip()
        parts = inner.replace(",", " ").split()
        for p in parts:
            low = p.lower()
            if low in _CURRENCY or p in _CURRENCY:
                unit = _CURRENCY.get(low, _CURRENCY.get(p))
            elif low in _SCALE:
                scale = _SCALE[low]
            elif low in ("%", "pct", "percent"):
                unit = "%"
            elif low[:1] in _CURRENCY and low[1:] in _SCALE:
                unit, scale = _CURRENCY[low[:1]], _SCALE[low[1:]]
            elif unit is None:
                unit = p
        text = text[: m.start()].strip()
    elif text.endswith("%"):
        unit, text = "%", text[:-1].strip()
    elif text[:1] in _CURRENCY and len(text) > 1:
        unit, text = _CURRENCY[text[:1]], text[1:].strip()
    return text or header, unit, scale


def coerce(values: list[Any]) -> tuple[list[Any], str]:
    """One column's values, typed the way a person reading the sheet would type them."""
    present = [v for v in values if not blank(v)]
    if not present:
        return [None] * len(values), "empty"
    if all(isinstance(v, bool) for v in present):
        return [None if blank(v) else bool(v) for v in values], "boolean"
    if all(isinstance(v, (dt.datetime, dt.date)) for v in present):
        has_time = any(isinstance(v, dt.datetime) and (v.hour or v.minute or v.second) for v in present)
        out = [None if blank(v) else (v if has_time else (v.date() if isinstance(v, dt.datetime) else v)) for v in values]
        return out, "timestamp" if has_time else "date"
    nums = [_number(v) for v in values]
    if all(n is not None for n, v in zip(nums, values, strict=True) if not blank(v)):
        if all(float(n).is_integer() for n in nums if n is not None) and not any(
                isinstance(v, float) for v in present) and not any(isinstance(v, str) and "." in v for v in present):
            return [None if n is None else int(n) for n in nums], "integer"
        return [None if n is None else float(n) for n in nums], "number"
    if all(isinstance(v, str) for v in present):
        parsed = pd.to_datetime(pd.Series([None if blank(v) else v for v in values]), errors="coerce", format="mixed")
        if parsed.notna().sum() == len(present) and len(present) >= 2:
            return [None if pd.isna(p) else p.date() for p in parsed], "date"
    return [None if blank(v) else str(v).strip() for v in values], "text"


def _number(v: Any) -> float | None:
    if blank(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not _looks_numeric(v):
        return None
    s = v.strip()
    neg = s.startswith("(") and s.endswith(")")
    pct = s.endswith("%") or s.endswith("%)")
    s = re.sub(r"[()$€£¥%\s]", "", s).lstrip("+")
    if s.count(",") and s.count(".") == 0 and re.fullmatch(r"-?\d{1,3}(,\d{3})+", s) is None and s.count(",") == 1:
        s = s.replace(",", ".")  # a decimal comma
    s = s.replace(",", "")
    try:
        n = float(s)
    except ValueError:
        return None
    n = -n if neg else n
    return n / 100 if pct else n


def generalise_formula(f: str) -> str:
    """'=C5-D5' -> '=C{row}-D{row}': the shape of a derived column, not one cell's instance of it."""
    return re.sub(r"(?<![A-Z$])(\$?[A-Z]{1,3})(\d+)\b", lambda m: f"{m.group(1)}{{row}}", f)


def check_total(total: dict[str, Any], columns: list[dict[str, Any]], frame: pd.DataFrame,
                names: list[str]) -> dict[str, Any]:
    """Does a total row equal the sum of the rows it closes? Reported either way; never silently trusted."""
    checks = []
    for j, col in enumerate(columns):
        if col["type"] not in ("integer", "number") or col.get("unit") == "%" or col["name"] not in frame:
            continue
        idx = names.index(col["header"]) if col["header"] in names else j
        stated = _number(total["values"][idx]) if idx < len(total["values"]) else None
        if stated is None:
            continue
        computed = float(pd.to_numeric(frame[col["name"]], errors="coerce").sum())
        # exact up to float noise; a whole-number total over fractional rows may be rounded for display
        tol = 0.5 if float(stated).is_integer() and not float(computed).is_integer() else max(abs(stated) * 1e-9, 0.005)
        ok = abs(computed - stated) <= tol
        checks.append({"column": col["name"], "stated": stated, "sum_of_rows": round(computed, 6), "matches": ok})
    return {"checks": checks, "matches": all(c["matches"] for c in checks) if checks else None}


def preview(frame: pd.DataFrame, n: int = 5) -> list[list[Any]]:
    out = []
    for row in frame.head(n).itertuples(index=False):
        out.append([None if (isinstance(v, float) and v != v) else (v.isoformat() if hasattr(v, "isoformat") else v)
                    for v in row])
    return out


def proposed_name(*parts: str | None) -> str:
    text = "_".join(p for p in parts if p)
    return normalise_name(text)[:48] if text else "table"
