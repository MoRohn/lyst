# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Forecast track record: "How accurate were your last forecasts?"

Every forecast answer states its own backtest error, but nobody could ask how the forecasts Lyst actually gave turned
out. Lyst now reads the forecast answers already given in this workspace and, for each forecast month the data has
since filled, compares the forecast with the actual figure (computed now, as the asker, from the approved definition):

* the error per forecast and per metric (mean absolute percentage error), and whether forecasts ran high or low;
* how often the actual landed inside the forecast's interval (an 80% interval should hold about 8 times in 10);
* the backtest error each forecast claimed, against the error it turned out to have;
* the biggest miss.

A forecast for a filtered slice ("revenue in EMEA") is left out rather than compared with the whole metric. When no
forecast month has arrived yet the answer says so, with the month the first check becomes possible.
"""

from __future__ import annotations

import json
import re
from typing import Any

import numpy as np
import pandas as pd

from ..semantic import compile_metric
from . import brief as brief_mod
from .registry import REGISTRY, Context, capability

ASKS = re.compile(
    r"\b(how (accurate|good|reliable|right|close|well) (?:were|are|have been|was|is|did)\s+(?:your|lyst'?s|its|"
    r"(?:the|our)\s+(?:last|past|previous|recent|earlier|latest))\s+(?:\w+\s+)?(forecasts?|predictions?|projections?)|"
    r"forecast(ing)? (accuracy|track record|performance|errors?|quality)|(accuracy|track record) of (your|the|our|lyst'?s) "
    r"(forecasts?|predictions?)|(forecasts?|predictions?) (turn(ed)? out|come true|hold up|held up|been right))", re.I)
MAX_TASKS = 200


def asks_for_track_record(text: str) -> bool:
    return bool(ASKS.search(text or ""))


def understanding(text: str) -> dict[str, Any]:
    return {**brief_mod.understanding(text), "intent": "track_record"}


def _month(x: Any) -> str:
    return str(x)[:7]


def _label(month: str) -> str:
    return pd.Timestamp(month + "-01").strftime("%b %Y")


def _past_forecasts(ctx: Context) -> list[dict[str, Any]]:
    """Forecast answers already given in this workspace, newest first, each with its metric and monthly points."""
    rt = ctx.services.runtime
    rows = rt.db.query("SELECT id, created_at, result FROM tasks WHERE workspace_id = ? AND status = 'done' AND id <> ? "
                       "AND result LIKE '%\"type\": \"forecast\"%' ORDER BY created_at DESC LIMIT ?",
                       [ctx.workspace_id, ctx.task_id or "", MAX_TASKS])
    out = []
    for r in rows:
        res = json.loads(r["result"]) if isinstance(r["result"], str) else r["result"]
        if not res or res.get("status") != "complete":
            continue
        spec = ((res.get("supporting") or {}).get("metric") or {}).get("spec") or {}
        chart = next((c for c in res.get("charts") or [] if c.get("type") == "forecast"), None)
        if not spec.get("name") or not chart:
            continue
        u = rt.db.query("SELECT output FROM task_steps WHERE task_id = ? AND capability = 'understand'", [r["id"]])
        filters = (json.loads(u[0]["output"]) or {}).get("filters") if u and u[0]["output"] else None
        backtest = None
        for f in res.get("evidence") or []:
            if m := re.search(r"backtest error ([\d.]+)%", str(f)):
                backtest = float(m.group(1)) / 100
        points = [{"month": _month(x), "yhat": y, "lo": lo, "hi": hi}
                  for x, y, lo, hi in zip(chart.get("x") or [], chart.get("y") or [], chart.get("lo") or [],
                                          chart.get("hi") or [], strict=False) if y is not None]
        out.append({"task_id": r["id"], "made": str(r["created_at"])[:10], "metric": spec["name"],
                    "question": res.get("question"), "filtered": bool(filters), "backtest": backtest, "points": points,
                    "made_from": _month(chart["history_x"][-1]) if chart.get("history_x") else None})
    return out


@capability(REGISTRY, "forecast_track_record", agent="forecaster", phase="validate", permission="data.query",
            description="How the forecasts Lyst already gave turned out: error per forecast and metric, bias, interval "
                        "coverage and claimed against realised error, for every forecast month the data has filled.")
def forecast_track_record(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from .engine import _evidence, _max_date  # noqa: PLC0415

    s = ctx.services
    allowed = {m.name: m for m in s.semantic.metrics(ctx.actor, ctx.workspace_id)}
    past = _past_forecasts(ctx)
    actuals: dict[str, dict[str, float]] = {}
    through: dict[str, str] = {}
    evidence = []
    scored, waiting, skipped = [], [], []
    for f in past:
        spec = allowed.get(f["metric"])
        if spec is None or f["filtered"]:
            skipped.append(f)
            continue
        if f["metric"] not in actuals:
            r = s.data.query(ctx.actor, ctx.workspace_id, compile_metric(spec, grain="month"), task_id=ctx.task_id)
            evidence.append(_evidence(r, f"{spec.name} by month, actuals"))
            actuals[f["metric"]] = {_month(p): float(v) for p, v in r.rows if v is not None}
            # a month counts only once the data covers it to its last day (month-stamped data covers its month)
            end = _max_date(ctx, spec)
            last = pd.Timestamp(end) if end is not None else pd.Timestamp.min
            complete = last if last.is_month_end else (last.replace(day=1) - pd.Timedelta(days=1))
            through[f["metric"]] = complete.strftime("%Y-%m")
        act, done = actuals[f["metric"]], through[f["metric"]]
        rows = []
        for p in f["points"]:
            if p["month"] <= done and p["month"] in act and (f["made_from"] is None or p["month"] > f["made_from"]):
                a = act[p["month"]]
                rows.append({**p, "actual": a, "ape": abs(p["yhat"] - a) / abs(a) if a else None,
                             "inside": p["lo"] is not None and p["hi"] is not None and p["lo"] <= a <= p["hi"],
                             "high": p["yhat"] > a})
        if rows:
            scored.append({**f, "title": spec.title, "format": spec.format, "rows": rows,
                           "mape": float(np.mean([x["ape"] for x in rows if x["ape"] is not None])) if any(
                               x["ape"] is not None for x in rows) else None})
        else:
            first = min((p["month"] for p in f["points"]), default=None)
            waiting.append({**f, "title": spec.title, "first": first, "through": done})
    return {"made": len(past), "scored": scored, "waiting": waiting, "skipped": len(skipped), "evidence": evidence}


def result(ctx: Context) -> dict[str, Any]:
    from .engine import fmt_plain  # noqa: PLC0415

    o = ctx.outputs
    u, t = o["understand"], o["track_record"]
    scored, waiting = t["scored"], t["waiting"]
    findings, implications, actions, tables = [], [], [], []
    assumptions = ["Actuals are computed now from each metric's approved definition; a month counts once the data "
                   "reaches its last day.",
                   "A forecast for a filtered slice is left out rather than compared with the whole metric."]
    if not t["made"]:
        answer = ("Lyst has not given a forecast in this workspace yet, so there is no track record to check. Ask for "
                  "one (for example \"Forecast revenue for next quarter\") and ask again once those months are in the data.")
        out = brief_mod._shape(u["question"], u, answer, [], [], [], [], [], [], {}, status="complete",
                               assumptions=assumptions)
        return _finish(out, t)
    if not scored:
        first = min((w["first"] for w in waiting if w["first"]), default=None)
        data_to = max((w["through"] for w in waiting), default=None)
        answer = (f"{'None of the ' + str(t['made']) + ' forecasts' if t['made'] != 1 else 'The one forecast'} Lyst has given here can{'' if t['made'] != 1 else 'not'} be checked "
                  "yet: " + (f"the first forecast month is {_label(first)} and the data runs to {_label(data_to)}."
                             if first and data_to else "their months are not in the data yet."))
        claimed = sorted({w["backtest"] for w in waiting if w["backtest"] is not None})
        if claimed:
            answer += ((" Its own backtest put" if len(waiting) == 1 else " Their own backtests put") + f" the error at {claimed[0]:.1%}" +
                       (f" to {claimed[-1]:.1%}" if claimed[-1] != claimed[0] else "") + ".")
        if first:
            actions.append(f"Ask again once {_label(first)} is loaded; Lyst will compare each forecast with what happened.")
        out = brief_mod._shape(u["question"], u, answer, [], [], actions, [], [], [], {}, status="complete",
                               assumptions=assumptions)
        return _finish(out, t)

    every = [r for f in scored for r in f["rows"]]
    apes = [r["ape"] for r in every if r["ape"] is not None]
    mape = float(np.mean(apes)) if apes else None
    inside = sum(r["inside"] for r in every)
    high = sum(r["high"] for r in every)
    n = len(every)
    lean = ("ran high" if high >= 0.75 * n else "ran low" if high <= 0.25 * n else "missed both ways") if n >= 4 else None
    answer = (f"Lyst checked {len(scored)} forecast{'s' if len(scored) != 1 else ''} against {n} month"
              f"{'s' if n != 1 else ''} that have since arrived: "
              + (f"{'they were' if len(scored) != 1 else 'it was'} off by {mape:.1%} on average, " if mape is not None else "")
              + f"and the actual landed inside the forecast's interval {inside} of {n} times")
    answer += (f"; {'they' if len(scored) != 1 else 'it'} {lean}." if lean and lean != "missed both ways" else ".")
    worst = max(every, key=lambda r: r["ape"] or 0)
    wf = next(f for f in scored if worst in f["rows"])
    findings.append(f"Biggest miss: {wf['title']} for {_label(worst['month'])}, forecast {fmt_plain(worst['yhat'], wf['format'])} against "
                    f"{fmt_plain(worst['actual'], wf['format'])} actual ({worst['ape']:.1%} off), from the forecast of {wf['made']}.")
    by_metric: dict[str, list[dict]] = {}
    for f in scored:
        by_metric.setdefault(f["title"], []).extend(f["rows"])
    for title, rows in by_metric.items():
        e = [r["ape"] for r in rows if r["ape"] is not None]
        findings.append(f"{title}: {np.mean(e):.1%} average error over {len(rows)} month{'s' if len(rows) != 1 else ''}, "
                        f"inside the interval {sum(r['inside'] for r in rows)} of {len(rows)}.")
    claimed = [f for f in scored if f["backtest"] is not None and f["mape"] is not None]
    if claimed:
        worse = [f for f in claimed if f["mape"] > 2 * max(f["backtest"], 0.005)]
        findings.append(f"{len(claimed) - len(worse)} of {len(claimed)} forecasts did about as well as their backtest "
                        "claimed" + (f"; {len(worse)} did more than twice as badly." if worse else "."))
        if worse:
            implications.append("A forecast that misses well beyond its backtest usually means the series changed "
                                "after it was made; read the next forecast's interval with that in mind.")
    if n >= 5 and inside / n < 0.6:
        implications.append(f"The intervals held only {inside} of {n} times, fewer than an 80% interval should; they "
                            "are too narrow for this data.")
        actions.append("Treat the forecast's range as narrower than the real uncertainty until more months are checked.")
    if waiting:
        findings.append(f"{len(waiting)} more forecast{'s' if len(waiting) != 1 else ''} cover months not in the data yet.")
    if t["skipped"]:
        findings.append(f"{t['skipped']} forecast{'s' if t['skipped'] != 1 else ''} for a filtered slice or a metric you "
                        "cannot read were left out.")
    actions.append(f"Ask why {wf['title'].lower() if not wf['title'][:2].isupper() else wf['title']} differed from its "
                   f"forecast in {_label(worst['month'])}.")
    tables.append({"title": "Forecasts against what happened",
                   "columns": ["metric", "made", "month", "forecast", "low", "high", "actual", "error", "inside interval"],
                   "rows": [[f["title"], f["made"], _label(r["month"]), r["yhat"], r["lo"], r["hi"], r["actual"],
                             r["ape"], "yes" if r["inside"] else "no"] for f in scored for r in f["rows"]]})
    checks = [{"name": "numbers_backed", "passed": True,
               "detail": "actuals come from recorded queries; forecasts are the figures each answer gave"},
              {"name": "enough_months", "passed": n >= 3,
               "detail": f"{n} forecast month{'s' if n != 1 else ''} checked" + ("" if n >= 3 else "; too few to judge")}]
    out = brief_mod._shape(u["question"], u, answer, findings, implications, actions, tables, [], checks,
                           {"evidence": t["evidence"]}, status="complete", assumptions=assumptions)
    return _finish(out, t)


def _finish(out: dict[str, Any], t: dict[str, Any]) -> dict[str, Any]:
    out["mode"] = "track_record"
    out["supporting"]["plan"] = [{"capability": "forecast_track_record", "agent": "forecaster"}]
    out["supporting"]["workflow"]["strategy"] = "track_record"
    out["supporting"]["brief"] = None
    out["supporting"]["reproduce"] = "Open each forecast answer listed and re-run the actuals queries in Supporting work."
    return out


__all__ = ["ASKS", "asks_for_track_record", "forecast_track_record", "result", "understanding"]
