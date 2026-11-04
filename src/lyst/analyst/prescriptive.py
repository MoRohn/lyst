# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Predictive and prescriptive capabilities: a period forecast with uncertainty, staffing scenarios under
that uncertainty, and event impact with a stated identification strategy."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pandas as pd

from ..analytics import causal, planning, timeseries
from ..semantic import compile_metric
from . import periods
from .registry import REGISTRY, Context, capability

SCENARIO_WORDS = re.compile(r"\b(scenario|staffing|headcount|hiring|agents?|capacity plan)\w*", re.I)
IMPACT_WORDS = re.compile(r"\b(impact|effect|consequence)s? of\b", re.I)
AGENT_COUNTS = re.compile(r"\b(\d{1,4})\s*(?:,|and|or|/)?\s*(?:agents?|staff|people|heads?|fte)?", re.I)


def wants_scenarios(text: str) -> bool:
    return bool(SCENARIO_WORDS.search(text))


def wants_impact(text: str) -> tuple[date, str] | None:
    """('impact of X since <month year>') -> the break date and the event text."""
    m = IMPACT_WORDS.search(text)
    if not m:
        return None
    tail = text[m.end():]
    at = re.search(r"\b(?:since|from|after|starting|in)\s+([A-Za-z]{3,9}\.?\s+20\d{2})", tail)
    if not at:
        return None
    brk = periods.parse(at.group(1), date(2100, 1, 1))
    if not brk:
        return None
    event = tail[: at.start()].strip(" ,")
    return brk.start, event


def _spec(ctx: Context, name: str):
    from .engine import _spec as engine_spec  # noqa: PLC0415

    return engine_spec(ctx, name)


def _q(ctx: Context, sql: str):
    return ctx.services.data.query(ctx.actor, ctx.workspace_id, sql, task_id=ctx.task_id)


def _ev(r, purpose: str) -> dict[str, Any]:
    return {"purpose": purpose, "sql": r.sql, "query_id": r.query_id, "rows": len(r.rows), "ms": r.ms, "datasets": r.datasets}


def _series(ctx: Context, spec) -> pd.Series:
    r = _q(ctx, compile_metric(spec, grain="month"))
    idx = pd.to_datetime([p for p, _ in r.rows]).tz_localize(None) if r.rows and getattr(r.rows[0][0], "tzinfo", None) \
        else pd.to_datetime([p for p, _ in r.rows])
    return pd.Series([float(v) for _, v in r.rows], index=idx), r


@capability(REGISTRY, "forecast_period", agent="data_scientist", phase="execute", permission="data.query",
            description="Forecast a metric for the next complete period (quarter or month) with an interval and a backtest.")
def forecast_period(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    spec = _spec(ctx, args["metric"])
    y, r = _series(ctx, spec)
    grain = args.get("grain", "quarter")
    last = y.index[-1]
    if grain == "quarter":
        nxt_start = (last + pd.offsets.QuarterBegin(startingMonth=1)).normalize()
        if nxt_start <= last:
            nxt_start = nxt_start + pd.offsets.QuarterBegin(startingMonth=1)
        months = pd.date_range(nxt_start, periods=3, freq="MS")
    else:
        months = pd.date_range(last + pd.offsets.MonthBegin(1), periods=1, freq="MS")
    horizon = int((months[-1].year - last.year) * 12 + months[-1].month - last.month)
    f = timeseries.forecast(y, horizon=horizon, grain="month", level=0.8)
    tbl = f.table[f.table["period"].isin(months)]
    agg = planning.quarter_from_monthly(tbl["yhat"].tolist(), tbl["lo"].tolist(), tbl["hi"].tolist(), level=0.8)
    label = f"Q{(months[0].month - 1) // 3 + 1} {months[0].year}" if grain == "quarter" else months[0].strftime("%b %Y")
    return {"metric": spec.name, "period_label": label, "months": [m.date().isoformat() for m in months],
            "monthly": [{"period": p.date().isoformat(), "yhat": float(a), "lo": float(b), "hi": float(c)}
                        for p, a, b, c in zip(tbl["period"], tbl["yhat"], tbl["lo"], tbl["hi"], strict=True)],
            "total": agg, "method": f.method, "backtest_mape": f.backtest_mape, "backtest_points": f.backtest_points,
            "history_last": {"period": last.date().isoformat(), "value": float(y.iloc[-1])},
            "history": [{"period": p.date().isoformat(), "value": float(v)} for p, v in y.items()],
            "evidence": [_ev(r, f"{spec.name} by month (forecast input)")]}


@capability(REGISTRY, "staffing_scenarios", agent="statistical_analyst", phase="execute", permission="data.query",
            description="Compare staffing scenarios against the demand forecast: shortfall risk, utilisation, cost; "
                        "and the minimum headcount for a 5% shortfall risk.")
def staffing_scenarios(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    fc = ctx.outputs[args["forecast_key"]]
    text = ctx.outputs["understand"]["question"]
    # staffing parameters: a dataset with agents / capacity_per_agent / cost_per_agent, latest month
    catalog = {d["name"]: d for d in ctx.services.data.catalog(ctx.actor, ctx.workspace_id)}
    staffing_ds = next((n for n, d in catalog.items()
                        if {"agents", "capacity_per_agent"} <= {c["name"] for c in d["schema"]}), None)
    evidence = []
    if staffing_ds:
        cols = {c["name"] for c in catalog[staffing_ds]["schema"]}
        tcol = next((c for c in ("month", "date", "period") if c in cols), None)
        order = f' ORDER BY "{tcol}" DESC' if tcol else ""
        r = _q(ctx, f'SELECT agents, capacity_per_agent{", cost_per_agent" if "cost_per_agent" in cols else ""} '
                    f'FROM "{staffing_ds}"{order} LIMIT 1')
        evidence.append(_ev(r, f"current staffing from {staffing_ds}"))
        row = dict(zip(r.columns, r.rows[0], strict=True))
        agents, cap = float(row["agents"]), float(row["capacity_per_agent"])
        cost = float(row["cost_per_agent"]) if row.get("cost_per_agent") is not None else None
        source = f"{staffing_ds} (latest month)"
    else:
        return {"skipped": "no staffing dataset with agents and capacity_per_agent columns; add one or state the "
                           "headcount and capacity per agent in the request"}
    months = len(fc["months"])
    cap_period = cap * months
    named = [int(x) for x in AGENT_COUNTS.findall(text) if 1 <= int(x) <= 5000 and int(x) != 20 and int(x) != 2026]
    named = [n for n in named if n not in (int(agents),)] if len(named) >= 3 else []
    if len(named) >= 3:
        scenarios = [{"name": f"{n} agents", "agents": n} for n in named[:3]]
    else:
        scenarios = [{"name": f"Hold at {int(agents)}", "agents": agents},
                     {"name": f"+10% ({int(round(agents * 1.1))})", "agents": round(agents * 1.1)},
                     {"name": f"+20% ({int(round(agents * 1.2))})", "agents": round(agents * 1.2)}]
    for sc in scenarios:
        sc["capacity_per_agent"] = cap_period
        sc["cost_per_agent"] = cost * months if cost is not None else None
    total = fc["total"]
    results = planning.staffing_scenarios(total["total"], total["sigma"], scenarios)
    minimum = planning.minimum_staff(total["total"], total["sigma"], cap_period, max_p_shortfall=0.05)
    best = min((r for r in results if r.p_shortfall <= 0.05), key=lambda r: r.cost if r.cost is not None else r.agents, default=None)
    return {"period_label": fc["period_label"], "staffing_source": source, "current_agents": agents, "capacity_per_agent_month": cap,
            "cost_per_agent_month": cost, "months": months,
            "scenarios": [r.__dict__ for r in results], "minimum_agents_for_5pct": minimum,
            "recommended": best.name if best else None, "evidence": evidence}


@capability(REGISTRY, "event_impact", agent="statistical_analyst", phase="test", permission="data.query",
            description="Estimate what changed after an event with an interrupted time series, stating the assumptions.")
def event_impact(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    spec = _spec(ctx, args["metric"])
    y, r = _series(ctx, spec)
    brk = pd.Timestamp(args["break_date"])
    idx = int((y.index >= brk).argmax()) if (y.index >= brk).any() else len(y)
    try:
        eff = causal.interrupted_time_series(y, idx)
    except ValueError as exc:
        return {"skipped": str(exc), "evidence": [_ev(r, f"{spec.name} by month")]}
    return {"metric": spec.name, "event": args.get("event", ""), "break_date": brk.date().isoformat(), "method": eff.method,
            "effect": eff.effect, "ci": [eff.ci_low, eff.ci_high], "p_value": eff.p_value, "n": eff.n, "details": eff.details,
            "assumptions": eff.assumptions, "warnings": eff.warnings,
            "series": [{"period": p.date().isoformat(), "value": float(v)} for p, v in y.items()],
            "evidence": [_ev(r, f"{spec.name} by month (impact input)")]}
