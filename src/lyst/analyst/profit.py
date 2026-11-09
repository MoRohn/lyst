# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Profit from separate income and expense records: "What was my profit last quarter?"

A freelancer or small business usually keeps income and spending in different exports (an invoicing app, a bank or
card), so no single approved metric is profit. When the workspace has an approved income metric and an approved
expense metric, both money sums with a date, and no approved metric of its own for profit, Lyst reads both month by
month as the asker and reports income minus expenses for the period asked about (the last complete month by default,
a quarter, or the year so far), with the margin, the period before, the same span a year earlier, the trailing twelve
months and the months that lost money.

It is a simple profit, income minus expenses, on the dates each record carries. It is not tax or accrual accounting,
and the answer says so.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from ..semantic import MetricSpec
from . import brief as brief_mod
from . import periods
from .registry import REGISTRY, Context, capability

ASKS = re.compile(
    r"\b(profits?|profitable|net income|take[- ]home|bottom line|net (earnings|result)|"
    r"(how much|what) did (i|we) (make|keep|clear|net)|money left|left over after|after expenses|"
    r"income (minus|less|after) (expenses|costs)|(making|losing|lose|lost) money)\b", re.I)
_INCOME = re.compile(r"\b(invoiced|income|revenue|sales|billings?|turnover|earnings|fees)\b", re.I)
_EXPENSE = re.compile(r"\b(expenses?|costs?|spend(ing)?|outgoings|purchases|bills)\b", re.I)
_OWN = re.compile(r"\b(profit|net income|operating income|margin|ebitda|earnings before)\b", re.I)
_YEAR = re.compile(r"\b(this year|year to date|ytd|so far this year)\b", re.I)


def _text(spec: MetricSpec) -> str:
    return f"{spec.name.replace('_', ' ')} {spec.title}"


def pair(metrics: list[MetricSpec]) -> tuple[MetricSpec, MetricSpec] | None:
    """The approved income and expense metrics profit is built from, or None when there is no such pair or the
    workspace already defines profit (or an operating income or margin) itself."""
    if any(_OWN.search(_text(m)) for m in metrics):
        return None

    def money(m: MetricSpec) -> bool:
        return (m.kind == "simple" and m.measure is not None and m.measure.agg == "sum" and m.format == "currency"
                and bool(m.time_column))

    income = [m for m in metrics if money(m) and m.higher_is_better and _INCOME.search(_text(m))]
    spend = [m for m in metrics if money(m) and not m.higher_is_better and _EXPENSE.search(_text(m))]
    if len(income) != 1 or len(spend) != 1:
        return None
    return income[0], spend[0]


def asks_for_profit(text: str) -> bool:
    return bool(ASKS.search(text or "")) and not brief_mod.ELSEWHERE.search(text or "")


def understanding(text: str, inc: MetricSpec, exp: MetricSpec) -> dict[str, Any]:
    grain = "year" if _YEAR.search(text) else ("quarter" if re.search(r"\b(quarter(ly)?|q[1-4])\b", text, re.I) else "month")
    return {**brief_mod.understanding(text, [inc.name, exp.name]), "intent": "profit", "grain": grain}


def _window(question: str, grain: str, anchor: date, fs: int) -> tuple[periods.Period, date, str | None]:
    """The period asked about and the day its data ends (exclusive): a year still running is the year to date."""
    if grain == "year":
        named = periods.parse(question, anchor, fs)
        p = named if named and named.grain in ("year", "ytd") else periods.parse("this year", anchor, fs)
        if p is None:
            p = periods.last_complete("year", anchor, fs)
        last = periods.last_complete("month", anchor, fs)
        end = min(p.end, last.end)
        return p, end, None if end == p.end else f"{p.label} runs through {last.label}, the last complete month."
    p, note = brief_mod._period(question, grain, anchor, fs)
    return p, p.end, note


def _sum(s: pd.Series, start: date, end: date) -> float | None:
    part = s[(s.index >= pd.Timestamp(start)) & (s.index < pd.Timestamp(end))]
    return float(part.sum()) if len(part) else None


def _shift(d: date, months: int) -> date:
    return (pd.Timestamp(d) + pd.DateOffset(months=months)).date()


@capability(REGISTRY, "profit", agent="business_analyst", phase="execute", permission="data.query",
            description="Income minus expenses from separate approved income and expense metrics: the period asked "
                        "about against the one before and a year earlier, margin, trailing twelve months and loss months.")
def profit(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from ..onboarding import fiscal_start  # noqa: PLC0415
    from .engine import _evidence  # noqa: PLC0415

    s = ctx.services
    u = ctx.outputs["understand"]
    specs = {m.name: m for m in s.semantic.metrics(ctx.actor, ctx.workspace_id)}
    inc, exp = specs[u["metrics"][0]], specs[u["metrics"][1]]
    fs = fiscal_start(s.semantic.stores.workspace(ctx.workspace_id))
    evidence = []
    covered = []
    for spec in (inc, exp):
        d, r = brief_mod._covered(ctx, spec)
        evidence.append(_evidence(r, f"{spec.dataset}: how far the data runs"))
        covered.append(d)
    if not all(covered):
        return {"empty": True, "evidence": evidence}
    anchor = min(covered)  # profit needs both sides: stop where the shorter one stops
    p, end, note = _window(u["question"], u.get("grain") or "month", anchor, fs)
    series = {}
    for spec in (inc, exp):
        sr, r = brief_mod._series(ctx, spec, "month", fs)
        evidence.append(_evidence(r, f"{spec.title} by month"))
        series[spec.name] = sr
    months = (end.year - p.start.year) * 12 + end.month - p.start.month
    spans = {"current": (p.start, end), "prior": (_shift(p.start, -months), p.start),
             "year_ago": (_shift(p.start, -12), _shift(end, -12))}
    figures = {}
    for k, (a, b) in spans.items():
        i, e = _sum(series[inc.name], a, b), _sum(series[exp.name], a, b)
        figures[k] = {"start": a.isoformat(), "end": b.isoformat(), "income": i, "expenses": e,
                      "profit": None if i is None or e is None else i - e}
    monthly = pd.DataFrame({"income": series[inc.name], "expenses": series[exp.name]}).fillna(0.0)
    monthly = monthly[monthly.index < pd.Timestamp(_shift(periods.last_complete("month", anchor, fs).end, 0))]
    monthly["profit"] = monthly["income"] - monthly["expenses"]
    last12 = monthly.tail(12)
    return {"income": inc.name, "expenses": exp.name, "period": {"label": p.label, "grain": p.grain,
                                                                  "start": p.start.isoformat(), "end": end.isoformat()},
            "note": note, "months": months, "figures": figures,
            "monthly": [{"month": ts.strftime("%b %Y"), **{k: float(v) for k, v in row.items()}}
                        for ts, row in last12.iterrows()],
            "data_through": {inc.dataset: covered[0].isoformat(), exp.dataset: covered[1].isoformat()},
            "evidence": evidence}


def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    sign = "-" if v < 0 else ""
    v = abs(v)
    return f"{sign}${v / 1000:,.1f}K" if v >= 10_000 else f"{sign}${v:,.0f}"


def _label(f: dict[str, Any], current: str, which: str, months: int) -> str:
    if which == "current":
        return current
    start, last = date.fromisoformat(f["start"]), _shift(date.fromisoformat(f["end"]), -1)
    if months == 1:
        return f"{start:%b %Y}"
    if months == 3 and start.month in (1, 4, 7, 10):
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    return f"{start:%b %Y} to {last:%b %Y}"


def result(ctx: Context) -> dict[str, Any]:
    o = ctx.outputs
    u, pr = o["understand"], o["profit"]
    specs = {m.name: m for m in ctx.services.semantic.metrics(ctx.actor, ctx.workspace_id)}
    if pr.get("empty"):
        answer = "There is no data yet for both income and expenses, so profit cannot be worked out."
        out = brief_mod._shape(u["question"], u, answer, [], [], [], [], [], [], pr, status="needs_input")
        out["mode"] = "profit"
        return out
    inc, exp = specs[pr["income"]], specs[pr["expenses"]]
    f, p, months = pr["figures"], pr["period"], pr["months"]
    cur = f["current"]
    label = p["label"] + (" so far" if p["grain"] == "year" and months < 12 and "YTD" not in p["label"] else "")
    margin = cur["profit"] / cur["income"] if cur["income"] else None
    verb = "Profit" if cur["profit"] >= 0 else "Loss"
    answer = (f"{verb} in {label}: {_money(abs(cur['profit']))} ({_money(cur['income'])} {inc.title.lower()} minus "
              f"{_money(cur['expenses'])} {exp.title.lower()})" + (f", a {margin:.0%} margin" if margin is not None and margin >= 0 else "") + ".")
    comps = []
    for k in ("prior", "year_ago") if p["grain"] not in ("year", "ytd") else ("year_ago",):
        x = f[k]
        if x["profit"] is None:
            continue
        d = cur["profit"] - x["profit"]
        comps.append(f"{'up' if d >= 0 else 'down'} {_money(abs(d))} on {_label(x, label, k, months)} ({_money(x['profit'])})")
    if comps:
        answer += " That is " + " and ".join(comps) + "."
    findings = [f"{inc.title}: {_money(cur['income'])} in {label}" + (
        f", against {_money(f['year_ago']['income'])} a year earlier" if f["year_ago"]["income"] is not None else "") + ".",
                f"{exp.title}: {_money(cur['expenses'])} in {label}" + (
        f", against {_money(f['year_ago']['expenses'])} a year earlier" if f["year_ago"]["expenses"] is not None else "") + "."]
    m12 = pr["monthly"]
    if len(m12) >= 6:
        t_inc, t_prof = sum(m["income"] for m in m12), sum(m["profit"] for m in m12)
        findings.append(f"Last {len(m12)} months: {_money(t_prof)} profit on {_money(t_inc)} {inc.title.lower()}"
                        + (f" ({t_prof / t_inc:.0%} margin)" if t_inc else "") + ".")
        losses = [m["month"] for m in m12 if m["profit"] < 0]
        if losses:
            findings.append(f"Months that lost money: {', '.join(losses)}.")
        best = max(m12, key=lambda m: m["profit"])
        worst = min(m12, key=lambda m: m["profit"])
        findings.append(f"Best month {best['month']} ({_money(best['profit'])}), weakest {worst['month']} "
                        f"({_money(worst['profit'])}).")
    implications, actions = [], []
    yo = f["year_ago"]
    if yo["income"] and yo["expenses"] and cur["income"] is not None:
        gi = (cur["income"] - yo["income"]) / abs(yo["income"])
        ge = (cur["expenses"] - yo["expenses"]) / abs(yo["expenses"])
        if ge > gi + 0.05:
            implications.append(f"{exp.title} grew faster than {inc.title.lower()} on a year earlier "
                                f"({ge:+.0%} against {gi:+.0%}), so the margin is being squeezed.")
            actions.append(f"Ask what drove {exp.title.lower()} in {label}, to see which costs grew.")
        elif gi > ge + 0.05:
            implications.append(f"{inc.title} grew faster than {exp.title.lower()} on a year earlier "
                                f"({gi:+.0%} against {ge:+.0%}), so more of each dollar earned is kept.")
    if cur["profit"] < 0:
        actions.insert(0, f"Ask which {exp.title.lower()} rose, and which clients billed less, in {label}.")
    actions.append(f"Ask \"who owes me money?\" to see what of the {inc.title.lower()} is still unpaid.")
    checks = [{"name": "numbers_backed", "passed": bool(pr["evidence"]), "severity": "error",
               "detail": "income and expenses each come from a recorded query of their approved metric"},
              {"name": "profit_adds_up", "passed": abs(cur["income"] - cur["expenses"] - cur["profit"]) < 1e-6,
               "severity": "error", "detail": "profit equals income minus expenses for the period"}]
    today = datetime.now(UTC).date()
    assumptions = [f"Profit here is {inc.title.lower()} minus {exp.title.lower()}, each counted on the date its record "
                   f"carries ({inc.time_column} and {exp.time_column}). It is not tax or accrual accounting.",
                   "Data through: " + ", ".join(f"{k} {v} ({brief_mod._ago(date.fromisoformat(v), today)})"
                                                for k, v in sorted(pr["data_through"].items())) + "."]
    if pr.get("note"):
        assumptions.insert(0, pr["note"])
    tables = [{"title": "Profit by month", "columns": ["month", inc.title, exp.title, "profit"],
               "rows": [[m["month"], round(m["income"], 2), round(m["expenses"], 2), round(m["profit"], 2)] for m in m12]}]
    charts = [{"type": "bar", "title": "Profit by month", "x": [m["month"] for m in m12],
               "y": [round(m["profit"], 2) for m in m12], "format": "currency"}] if m12 else []
    out = brief_mod._shape(u["question"], u, answer, findings, implications, actions, tables, charts, checks,
                           {"evidence": pr["evidence"]}, status="complete", assumptions=assumptions)
    out["mode"] = "profit"
    out["supporting"]["plan"] = [{"capability": "profit", "agent": "business_analyst"}]
    out["supporting"]["workflow"]["strategy"] = "profit"
    out["supporting"]["brief"] = None
    out["supporting"]["profit"] = {"period": p, "figures": f}
    return out


__all__ = ["ASKS", "asks_for_profit", "pair", "profit", "result", "understanding"]
