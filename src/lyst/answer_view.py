# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What an answer shows, in one place, for the screen and for every file made from it.

The answer page (``lyst.ui.components.answer``) and the downloads (``lyst.deliverables``) both read a stored
result through these functions, so a PDF, a deck, a workbook or a Markdown file carries the same headline, charts,
artifacts, evidence, checks, assumptions and method a person saw on screen. Pure Python: no Streamlit, no I/O.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

# The on-screen answer, top to bottom. Every download carries each of these that the result has (tests hold it).
ELEMENTS = ("answer", "evidence", "charts", "implications", "actions", "tables", "hypotheses", "made_here",
            "rail", "assumptions", "sources", "citations", "checks", "plan", "queries", "reproduce")

# Section titles, shared by every format so a reader finds the same words in each.
TITLES = {
    "answer": "Answer", "evidence": "Evidence", "charts": "Chart", "implications": "What it means",
    "actions": "What to do", "tables": "Tables", "hypotheses": "Hypotheses tested", "made_here": "Made in this answer",
    "rail": "Behind the answer", "assumptions": "Assumptions", "sources": "Sources",
    "citations": "Documents consulted", "checks": "Checks", "plan": "Plan", "queries": "Queries",
    "reproduce": "Reproduce", "method": "Method, checks and queries",
}

# Lyst's light theme (lyst/ui/theme tokens): the accent for the measure, a muted line for history.
ACCENT = "#0B6B64"
MUTED = "#5b6577"
TEXT = "#16181d"
BORDER = "#e3e5ea"
SURFACE = "#f5f6f8"
GOOD, WARN, BAD = "#1a7f4b", "#b26a00", "#c0392b"

_CAVEAT = re.compile(r"\b(did not|does not|disagree|differently|no period was named|assumed|filtered|caution|not verified)\b", re.I)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def mix(a: str, b: str, t: float) -> str:
    """The colour ``t`` of the way from ``a`` to ``b`` (hex): a solid tint that prints and renders everywhere."""
    pa = [int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    pb = [int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb, strict=True))


BAND = mix("#ffffff", ACCENT, 0.2)  # the forecast interval, as on screen (accent at low opacity on white)


def fmt(v: Any, kind: str = "number") -> str:
    """A figure as the answer writes it: 32.0%, $1.25M, 1,234."""
    if v is None or isinstance(v, bool) or not isinstance(v, int | float):
        return "" if v is None else str(v)
    if isinstance(v, float) and not math.isfinite(v):
        return "n/a"
    if kind == "percent":
        return f"{v * 100:.1f}%"
    if kind == "currency":
        a, s = abs(v), "-" if v < 0 else ""
        for div, unit in ((1e9, "B"), (1e6, "M")):
            if a >= div:
                return f"{s}${a / div:.2f}{unit}"
        return f"{s}${a / 1e3:.1f}K" if a >= 1e3 else f"{s}${a:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.4g}" if abs(v) < 1000 else f"{v:,.0f}"


def as_date(x: Any) -> date | None:
    """'2026-10-01' (or a datetime string) as a date; anything else None."""
    if isinstance(x, date):
        return x
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?(?:[T ].*)?", str(x))
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3] or 1))
    except ValueError:
        return None


def period_label(x: Any) -> str:
    """'2026-10-01' -> 'Oct 2026'; other labels unchanged."""
    d = as_date(x)
    return f"{_MONTHS[d.month - 1]} {d.year}" if d else str(x)


def heading(col: Any) -> str:
    """'prior_rate' -> 'Prior rate'."""
    text = str(col).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def column_kind(col: str, chart_kind: str = "number") -> str:
    """How a table column's numbers read, by the same rule the on-screen table uses."""
    c = str(col).lower()
    if any(k in c for k in ("rate", "effect", "contribution", "share", "margin")):
        return "percent"
    if c in ("value", "forecast", "low", "high", "lo", "hi", "actual", "prior", "current", "change") and chart_kind != "number":
        return chart_kind
    return "number"


def cell(v: Any, kind: str = "number") -> str:
    """A table cell as text: numbers in the column's format, missing values as an em dash."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        if abs(v) < 5e-7:
            v = 0.0
        if kind != "number":
            return fmt(v, kind)
        return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.2f}"
    if isinstance(v, int):
        return fmt(v, kind) if kind != "number" else f"{v:,}"
    if isinstance(v, dict | list):
        import json  # noqa: PLC0415

        return json.dumps(v, default=str)
    return str(v)


def trust_line(r: dict[str, Any]) -> str | None:
    """What 'Verified' means for this answer, in one line a business reader can repeat."""
    conf = r.get("confidence") or {}
    checks = conf.get("checks") or []
    passed = sum(1 for c in checks if c.get("passed"))
    if r.get("status") == "needs_input":
        return "Lyst needs one more detail before it can answer; see below."
    if conf.get("level") == "verified" and passed == len(checks):
        return (f"All {len(checks)} checks passed: every figure was recomputed independently from approved data."
                if checks else "Every figure was recomputed independently from approved data.")
    if checks:
        return f"{passed} of {len(checks)} checks passed. Read the notes below before relying on this."
    return None


def caveats(r: dict[str, Any]) -> list[str]:
    """What a reader must not miss: wrong premises, defaulted periods, filters, disagreements, failed warnings."""
    sup = r.get("supporting") or {}
    out = [a for a in sup.get("assumptions", []) if _CAVEAT.search(a)]
    out += [f"Check not passed: {c.get('name', 'a check')}" + (f" ({c['detail']})" if c.get("detail") else "")
            for c in (r.get("confidence") or {}).get("checks", []) if not c.get("passed", True)]
    return out


def workflow_note(r: dict[str, Any]) -> str | None:
    wf = (r.get("supporting") or {}).get("workflow") or {}
    if wf.get("used"):
        return (f"Reused the checked analysis “{wf['used']['title']}”, verified {wf['used'].get('verified_runs', 0)}× "
                "before, so this took one pass instead of being worked out again.")
    if (wf.get("compiled") or {}).get("created"):
        return "Saved as a repeatable analysis: rerun it for any period, or have it repeat on its own."
    return None


def level_label(r: dict[str, Any]) -> str:
    if r.get("status") == "needs_input":
        return "Needs your input"
    if r.get("mode") == "conversation":
        return "Reply"
    return str((r.get("confidence") or {}).get("level") or "").capitalize()


def breakdown_chart(r: dict[str, Any]) -> dict[str, Any] | None:
    """A breakdown answer (a table of one category column and one number) as a bar chart, so the Summary shows
    the split at a glance instead of only naming the largest part. Only when the answer has no chart of its own."""
    if r.get("charts"):
        return None
    for t in r.get("tables") or []:
        cols, rows = t.get("columns") or [], t.get("rows") or []
        if len(cols) == 2 and 2 <= len(rows) <= 24 and all(
                isinstance(row[1], int | float) and not isinstance(row[0], int | float) for row in rows):
            kind = "percent" if any(k in cols[1] for k in ("rate", "share", "margin", "ratio")) else (
                "currency" if "$" in str(r.get("answer", "")) else "number")
            return {"type": "bar", "title": t.get("title") or f"{heading(cols[1])} by {heading(cols[0]).lower()}",
                    "x": [str(row[0]) for row in rows], "y": [float(row[1]) for row in rows], "format": kind}
    return None


def charts(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Every chart the answer shows: its own charts, or the breakdown bar when it has none."""
    own = [c for c in r.get("charts") or [] if c.get("x")]
    if own:
        return own
    bc = breakdown_chart(r)
    return [bc] if bc else []


def chart_rows(c: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """A chart's data as one table: actual history then forecast with its interval, or the plotted series."""
    if c.get("type") == "forecast":
        cols = ["period", "actual", "forecast", "low", "high"]
        rows: list[list[Any]] = [[x, y, None, None, None] for x, y in zip(c.get("history_x") or [], c.get("history_y") or [], strict=False)]
        n = len(c["x"])
        lo, hi = c.get("lo") or [None] * n, c.get("hi") or [None] * n
        rows += [[x, None, y, a, b] for x, y, a, b in zip(c["x"], c["y"], lo, hi, strict=False)]
        return cols, rows
    label = "category" if c.get("type") == "bar" else "period"
    return [label, "value"], [[x, y] for x, y in zip(c["x"], c["y"], strict=False)]


def chart_alt(c: dict[str, Any]) -> str:
    """A text equivalent of the chart: what it shows, its range, its last value (and the forecast's interval)."""
    kind = c.get("format", "number")
    ys = [v for v in (c.get("history_y") or []) + (c.get("y") or []) if isinstance(v, int | float)]
    if not ys:
        return str(c.get("title") or "Chart")
    xs = (c.get("history_x") or []) + (c.get("x") or [])
    name = {"forecast": "Forecast", "bar": "Bar chart"}.get(c.get("type"), "Line chart")
    text = (f"{name}: {c.get('title', '')}. {len(ys)} points from {period_label(xs[0])} to {period_label(xs[-1])}; "
            f"lowest {fmt(min(ys), kind)}, highest {fmt(max(ys), kind)}, last {fmt(ys[-1], kind)}.")
    if c.get("type") == "forecast" and c.get("lo") and c.get("hi"):
        text += f" Interval at {period_label(c['x'][-1])}: {fmt(c['lo'][-1], kind)} to {fmt(c['hi'][-1], kind)}."
    return text


def measure_kind(r: dict[str, Any]) -> str:
    """How the answer's measure reads (percent, currency or number): its chart's format, else its metric's."""
    for c in charts(r):
        if c.get("format"):
            return str(c["format"])
    spec = ((r.get("supporting") or {}).get("metric") or {}).get("spec") or {}
    return str(spec.get("format") or "number")


def table_kinds(r: dict[str, Any], columns: list[str]) -> list[str]:
    kind = measure_kind(r)
    return [column_kind(c, kind) for c in columns]


def made_here(r: dict[str, Any]) -> list[dict[str, Any]]:
    """The artifacts this answer produced, as listed under 'Made in this answer': each chart, on its own."""
    return [{"kind": "chart", "title": c.get("title") or f"Chart {i + 1}", "index": i}
            for i, c in enumerate(r.get("charts") or [])]


def sources(r: dict[str, Any]) -> list[dict[str, Any]]:
    return list({(s.get("dataset"), s.get("version")): s for s in (r.get("supporting") or {}).get("sources") or []}.values())


def source_line(s: dict[str, Any], full: bool = True) -> str:
    parts = [s.get("dataset"), f"v{s['version']}" if s.get("version") else None,
             f"{s['rows']:,} rows" if isinstance(s.get("rows"), int) else None,
             f"data through {s['data_through']}" if s.get("data_through") else None]
    if full:
        parts += [f"sha256 {str(s['sha256'])[:16]}" if s.get("sha256") else None,
                  f"loaded {str(s['loaded_at'])[:16]}" if s.get("loaded_at") else None]
    return " · ".join(str(p) for p in parts if p not in (None, ""))


def rail(r: dict[str, Any]) -> list[tuple[str, str]]:
    """The evidence beside the answer, as (heading, text) pairs: checks, data, expertise, assumptions, method."""
    conf = r.get("confidence") or {}
    sup = r.get("supporting") or {}
    out: list[tuple[str, str]] = []
    checks = conf.get("checks") or []
    if checks:
        ok = sum(1 for c in checks if c.get("passed"))
        out.append(("Checks", f"{ok} of {len(checks)} passed"))
    srcs = sources(r)
    if srcs:
        out.append(("Data", "; ".join(source_line(s, full=False) for s in srcs[:4])))
    ex = sup.get("expertise") or {}
    if ex:
        disc = ex.get("disciplines") or []
        names = ", ".join(str(d.get("name", d.get("id", ""))) if isinstance(d, dict) else str(d) for d in disc[:4])
        counts: dict[str, int] = {}
        for rule in ex.get("rules") or []:
            counts[rule.get("status", "")] = counts.get(rule.get("status", ""), 0) + 1
        bits = [names or str(ex.get("primary") or "")]
        if counts:
            bits.append("rules: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items() if v))
        if ex.get("note"):
            bits.append(str(ex["note"]))
        out.append(("Expertise applied", ". ".join(b for b in bits if b)))
    assumptions = sup.get("assumptions") or []
    if assumptions:
        out.append((f"Assumptions · {len(assumptions)}", "; ".join(str(a) for a in assumptions)))
    q = sup.get("queries") or []
    if q or sup.get("plan"):
        out.append(("Method", f"{len(sup.get('plan') or [])} steps · {len(q)} governed quer{'y' if len(q) == 1 else 'ies'}"))
    return out


def plan_line(r: dict[str, Any]) -> str:
    return " → ".join(str(p.get("capability", "?")) for p in (r.get("supporting") or {}).get("plan") or [])


def query_line(q: dict[str, Any]) -> str:
    bits = [f"{q['rows']} rows" if q.get("rows") is not None else None, f"{q['ms']} ms" if q.get("ms") is not None else None,
            f"query #{q['query_id']}" if q.get("query_id") else None,
            ", ".join(q["datasets"]) if q.get("datasets") else None]
    return " · ".join(b for b in bits if b)


def reproduce_line(r: dict[str, Any]) -> str | None:
    sup = r.get("supporting") or {}
    if not sup.get("reproduce"):
        return None
    return str(sup["reproduce"]) + (f" Narrative written by: {sup['narrative_by']}." if sup.get("narrative_by") else "")


def present(r: dict[str, Any]) -> set[str]:
    """The elements this result has, so each format (and its tests) knows what it owes the reader."""
    sup = r.get("supporting") or {}
    conf = r.get("confidence") or {}
    have = {"answer"} if r.get("answer") else set()
    have |= {k for k in ("evidence", "implications", "actions", "tables", "hypotheses") if r.get(k)}
    if charts(r):
        have.add("charts")
    if made_here(r):
        have.add("made_here")
    if rail(r):
        have.add("rail")
    have |= {k for k in ("assumptions", "citations", "plan", "queries", "reproduce") if sup.get(k)}
    if sources(r):
        have.add("sources")
    if conf.get("checks"):
        have.add("checks")
    return have
