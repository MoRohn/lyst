# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Analytical cognition beyond one decomposition: depth control, competing hypotheses tested with
deterministic computation, multi-period investigation with change points, and corroboration from
related metrics.

Depth (chosen in ``understand``, used by ``plan``):

* **fast**: a value, a breakdown, a guarded query, a registered skill.
* **analytical**: one-period "why", trends, forecasts, data-quality profiles.
* **deep**: "why" over several periods, or when the request asks to investigate, test alternatives or
  find root causes. Adds the quarterly horizon, change points, hypotheses and related metrics.
* **project**: build, automate, dashboard, reporting-system requests (the Studio, later slices).

A hypothesis is a named claim with a deterministic test. The verdicts are *supported*, *partial*,
*refuted* or *untestable*, each with the numbers that decided it. The answer separates established
findings (computed values) from tested hypotheses, and says which explanation the evidence favours.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pandas as pd

from ..analytics.changepoint import change_points
from ..semantic import MetricSpec, compile_metric
from . import periods
from .registry import REGISTRY, Context, capability

DEEP_WORDS = re.compile(r"\b(investigat|root cause|alternative|competing|hypothes|test (the|our|each)|"
                        r"isolate|deep.?dive|diagnos|reconcil)", re.I)
PROJECT_WORDS = re.compile(r"\b(build|create|automate|set up|dashboard|reporting system|pipeline|monitor)\b", re.I)
HORIZON = re.compile(r"\b(?:last|past|previous|across the last|over the last)\s+(\d+|two|three|four|five|six|seven|"
                     r"eight|nine|ten|twelve)\s+(quarters|months|years)\b", re.I)
_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
          "twelve": 12}


def horizon_of(text: str) -> tuple[int, str] | None:
    m = HORIZON.search(text)
    if not m:
        return None
    n = _WORDS.get(m.group(1).lower()) or int(m.group(1))
    return n, m.group(2).lower().rstrip("s")


def depth_of(text: str, intent: str, horizon: tuple[int, str] | None) -> str:
    if PROJECT_WORDS.search(text) and intent not in ("sql", "skill"):
        return "project"
    if intent in ("value", "breakdown", "sql", "skill"):
        return "fast" if not (horizon and intent == "value") else "analytical"
    if intent == "explain" and (horizon or DEEP_WORDS.search(text)):
        return "deep"
    return "analytical"


# ----------------------------------------------------------------------------- helpers

def _q(ctx: Context, sql: str):
    return ctx.services.data.query(ctx.actor, ctx.workspace_id, sql, task_id=ctx.task_id)


def _ev(r, purpose: str) -> dict[str, Any]:
    return {"purpose": purpose, "sql": r.sql, "query_id": r.query_id, "rows": len(r.rows), "ms": r.ms,
            "datasets": r.datasets}


def _spec(ctx: Context, name: str) -> MetricSpec:
    for m in ctx.outputs["contextualize"]["metrics"]:
        if m["spec"]["name"] == name:
            return MetricSpec.model_validate(m["spec"])
    return ctx.services.semantic.get(ctx.actor, ctx.workspace_id, name)


def _label(ts: pd.Timestamp, grain: str, fs: int = 1) -> str:
    if grain == "quarter":
        return periods._quarter(ts.date().replace(day=1), fs).label if fs != 1 else f"Q{(ts.month - 1) // 3 + 1} {ts.year}"
    if grain == "month":
        return ts.strftime("%b %Y")
    if fs != 1 and grain == "year":
        return f"FY{periods._fy_end_year(ts.date(), fs)}"
    return str(ts.year)


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return (a - b) / abs(b)


# ------------------------------------------------------------------- horizon and breaks


@capability(REGISTRY, "horizon_series", agent="analytics_engineer", phase="investigate", permission="data.query",
            description="A governed metric per period over the requested horizon (for example the last six quarters).")
def horizon_series(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    spec = _spec(ctx, args["metric"])
    grain, n = args.get("grain", "quarter"), int(args.get("periods", 6))
    period = ctx.outputs["contextualize"]["period"]
    anchor = date.fromisoformat(period["anchor"])
    if args.get("until"):  # a series that ends with the period the request named, not the latest one
        end = date.fromisoformat(str(args["until"]["end"]))
    else:
        end = periods.last_complete(grain, anchor, period.get("fiscal_start", 1)).end
    start = pd.Timestamp(end) - pd.DateOffset(**{f"{grain}s": n}) if grain != "quarter" else \
        pd.Timestamp(end) - pd.DateOffset(months=3 * n)
    fs = period.get("fiscal_start", 1)
    r = _q(ctx, compile_metric(spec, start=start.date().isoformat(), end=end.isoformat(), grain=grain, fiscal_start=fs))
    pts = []
    for period, value in r.rows:
        ts = pd.Timestamp(period).tz_localize(None) if getattr(period, "tzinfo", None) else pd.Timestamp(period)
        pts.append({"period": ts.date().isoformat(), "label": _label(ts, grain, fs),
                    "value": float(value) if value is not None else None})
    return {"metric": spec.name, "grain": grain, "points": pts, "evidence": [_ev(r, f"{spec.name} by {grain}, last {n}")]}


@capability(REGISTRY, "change_points", agent="statistical_analyst", phase="investigate", permission="analyst.ask",
            optional=True, description="Where the level of a series shifted (binary segmentation with a BIC penalty).")
def change_points_step(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    src = ctx.outputs[args["series_key"]]
    pts = [p for p in src["points"] if p["value"] is not None]
    breaks = change_points([p["value"] for p in pts])
    out = []
    for b in breaks:
        out.append({"index": b.index, "at": pts[b.index]["period"], "label": pts[b.index]["label"],
                    "before_label": pts[b.index - 1]["label"], "before": b.before, "after": b.after,
                    "shift": b.shift, "gain": b.gain, "penalty": b.penalty})
    latest_outlier = None
    if len(pts) >= 4:
        import numpy as np  # noqa: PLC0415

        head = np.asarray([p["value"] for p in pts[:-1]], dtype=float)
        sd = float(head.std(ddof=1)) if len(head) > 1 else 0.0
        dev = pts[-1]["value"] - float(head.mean())
        if sd > 0 and abs(dev) > 2 * sd:
            latest_outlier = {"label": pts[-1]["label"], "deviation": dev, "sigma": abs(dev) / sd, "prior_mean": float(head.mean())}
            # a break whose "after" segment is just the final two points is the outlier wearing a different hat
            out = [b for b in out if b["index"] < len(pts) - 2]
    return {"breaks": out, "points": len(pts), "method": "binary segmentation, mean shift, BIC penalty",
            "latest": out[-1] if out else None, "latest_outlier": latest_outlier}


# -------------------------------------------------------------------------- hypotheses

def _parts_sql(spec: MetricSpec, p: dict[str, Any]) -> str | None:
    """Numerator and denominator totals of a ratio metric for one period."""
    if spec.kind != "ratio":
        return None
    from .engine import _sum_target  # noqa: PLC0415

    num, den = _sum_target(spec.numerator), _sum_target(spec.denominator)
    if not (num and den):
        return None
    where = [f'"{spec.time_column}" >= \'{p["start"]}\'', f'"{spec.time_column}" < \'{p["end"]}\'']
    from ..semantic import _cond  # noqa: PLC0415

    where += [_cond(f) for f in spec.filters]
    return f'SELECT SUM({num}) AS num, SUM({den}) AS den FROM "{spec.dataset}" WHERE {" AND ".join(where)}'


@capability(REGISTRY, "hypothesize", agent="lead_analyst", phase="hypothesize", permission="analyst.ask",
            description="State the competing explanations a change could have, each with the test that decides it.")
def hypothesize(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    spec = _spec(ctx, args["metric"])
    hs = [
        {"id": "concentration", "claim": f"One {' or '.join(spec.dimensions) or 'segment'} drove most of the change",
         "test": "the largest member's own effect covers at least 60% of the total change"},
        {"id": "broad", "claim": "The change is broad-based across segments",
         "test": "at least 70% of members moved in the direction of the total and no member covers half of it"},
        {"id": "mix", "claim": "A shift in mix between segments, not their own performance, moved the metric",
         "test": "mix effects explain at least 30% of the change (ratio metrics only)"},
        {"id": "seasonality", "claim": "The move repeats every year at this time",
         "test": "the same period a year earlier moved in the same direction by a comparable amount"},
        {"id": "single_month", "claim": "One month inside the period explains the change",
         "test": "one month deviates from the prior period's monthly level by more than twice the others"},
        {"id": "artifact", "claim": "A data problem produced the change rather than the business",
         "test": "duplicates, nulls in the metric's columns, or incomplete coverage inside the period"},
    ]
    if spec.kind == "ratio":
        hs.append({"id": "parts", "claim": "The numerator moved, not the denominator (or the reverse)",
                   "test": "relative change of numerator and denominator between the periods"})
    return {"metric": spec.name, "hypotheses": hs}


@capability(REGISTRY, "test_hypotheses", agent="statistical_analyst", phase="test", permission="data.query",
            description="Decide each hypothesis with computed numbers: supported, partial, refuted or untestable.")
def test_hypotheses(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    o = ctx.outputs
    spec = _spec(ctx, args["metric"])
    cur_p, prev_p = args["current"], args["prior"]
    cur, prev = o.get("current", {}).get("value"), o.get("prior", {}).get("value")
    total = (cur - prev) if cur is not None and prev is not None else None
    drivers = o.get("drivers") or {}
    evidence: list[dict[str, Any]] = []
    verdicts: dict[str, dict[str, Any]] = {}

    def verdict(hid: str, status: str, detail: str, **numbers):
        verdicts[hid] = {"id": hid, "verdict": status, "detail": detail, "numbers": numbers}

    # concentration / broad / mix: from the decomposition already computed
    if total and drivers.get("by_dimension"):
        best = drivers.get("best") or {}
        key = "effect" if drivers["kind"] == "rate" else "change"
        share = abs(best.get(key, 0)) / abs(total) if best else 0.0
        verdict("concentration", "supported" if share >= 0.6 else ("partial" if share >= 0.35 else "refuted"),
                f"{best.get('dimension')} = {best.get('member')} explains {share:.0%} of the change", share=share,
                member=best.get("member"), dimension=best.get("dimension"))
        sign = 1 if total > 0 else -1
        broad_scores = []
        for dim, t in drivers["by_dimension"].items():
            rows = [r for r in t["table"] if str(r.get(dim)) != "(unattributed)"]
            col = "rate_effect" if drivers["kind"] == "rate" else "change"
            same = sum(1 for r in rows if (r.get(col) or 0) * sign > 0)
            broad_scores.append((dim, same / len(rows) if rows else 0.0))
        dim, frac = max(broad_scores, key=lambda x: x[1]) if broad_scores else (None, 0.0)
        verdict("broad", "supported" if frac >= 0.7 and share < 0.5 else ("partial" if frac >= 0.5 else "refuted"),
                f"{frac:.0%} of {dim} members moved with the total" if dim else "no dimensions", fraction=frac,
                dimension=dim)
        if drivers["kind"] == "rate":
            mix = {dim: sum(r.get("mix_effect") or 0 for r in t["table"]) for dim, t in drivers["by_dimension"].items()}
            dim, m = max(mix.items(), key=lambda kv: abs(kv[1]))
            mshare = abs(m) / abs(total)
            verdict("mix", "supported" if mshare >= 0.3 else ("partial" if mshare >= 0.1 else "refuted"),
                    f"mix across {dim} explains {mshare:.0%} of the change", share=mshare, dimension=dim)
        else:
            verdict("mix", "untestable", "mix effects apply to ratio metrics")
    else:
        for h in ("concentration", "broad", "mix"):
            verdict(h, "untestable", "no decomposition available")

    # seasonality: same move a year earlier?
    ya = o.get("year_ago", {}).get("value")
    ya_prev = None
    if ya is not None and total:
        from .engine import _prev_period, _year_ago  # noqa: PLC0415

        yap = _prev_period(_year_ago(cur_p) or cur_p)
        r = _q(ctx, compile_metric(spec, start=yap["start"], end=yap["end"]))
        evidence.append(_ev(r, f"{spec.name} for {yap['label']}"))
        ya_prev = r.rows[0][0] if r.rows and r.rows[0][0] is not None else None
        if ya_prev is not None:
            last_year_move = ya - float(ya_prev)
            same_dir = last_year_move * total > 0
            ratio = abs(last_year_move) / abs(total)
            verdict("seasonality", "supported" if same_dir and 0.5 <= ratio <= 2 else
                    ("partial" if same_dir else "refuted"),
                    f"a year earlier the same transition moved {last_year_move:+.4g} against {total:+.4g} now",
                    last_year=last_year_move, now=total)
        else:
            verdict("seasonality", "untestable", "no data for the comparable period a year earlier")
    else:
        verdict("seasonality", "untestable", "no year-earlier value")

    # single month: monthly values across prior and current periods
    if spec.time_column and total:
        r = _q(ctx, compile_metric(spec, start=prev_p["start"], end=cur_p["end"], grain="month"))
        evidence.append(_ev(r, f"{spec.name} by month across {prev_p['label']} and {cur_p['label']}"))
        months = [(pd.Timestamp(p).date().isoformat(), float(v)) for p, v in r.rows if v is not None]
        prior_months = [v for d, v in months if d < cur_p["start"]]
        cur_months = [(d, v) for d, v in months if d >= cur_p["start"]]
        if prior_months and len(cur_months) >= 2:
            base = sum(prior_months) / len(prior_months)
            devs = [(d, v - base) for d, v in cur_months]
            devs.sort(key=lambda x: abs(x[1]), reverse=True)
            top, rest = devs[0], devs[1:]
            rest_max = max(abs(x[1]) for x in rest) if rest else 0.0
            single = abs(top[1]) > 2 * rest_max and rest_max >= 0
            verdict("single_month", "supported" if single else "refuted",
                    f"{pd.Timestamp(top[0]).strftime('%b %Y')} deviates {top[1]:+.4g} from the prior level; "
                    f"the next largest month {rest_max:.4g}", month=top[0], deviation=top[1], next_largest=rest_max)
        else:
            verdict("single_month", "untestable", "fewer than two months in the period")
    else:
        verdict("single_month", "untestable", "the metric has no time column")

    # data artifact: duplicates, nulls and coverage inside the current period
    cols = sorted(spec.columns())
    where = f'"{spec.time_column}" >= \'{cur_p["start"]}\' AND "{spec.time_column}" < \'{cur_p["end"]}\''
    collist = ", ".join(f'"{c}"' for c in cols)
    r = _q(ctx, f'SELECT count(*) AS rows, count(*) - (SELECT count(*) FROM (SELECT DISTINCT {collist} FROM "{spec.dataset}" '
                f'WHERE {where})) AS dup_rows, '
                + ", ".join(f'count(*) - count("{c}") AS "null_{c}"' for c in cols if c != spec.time_column)
                + f', count(DISTINCT date_trunc(\'month\', "{spec.time_column}")) AS months FROM "{spec.dataset}" WHERE {where}')
    evidence.append(_ev(r, f"data checks inside {cur_p['label']}"))
    row = dict(zip(r.columns, r.rows[0], strict=True))
    expected_months = {"quarter": 3, "month": 1, "year": 12}.get(cur_p.get("grain"), None)
    nulls = {k[5:]: v for k, v in row.items() if k.startswith("null_") and v}
    problems = []
    if row["dup_rows"]:
        problems.append(f"{row['dup_rows']} duplicate rows")
    if nulls:
        problems.append("nulls in " + ", ".join(f"{k} ({v})" for k, v in nulls.items()))
    if expected_months and row["months"] < expected_months:
        problems.append(f"only {row['months']} of {expected_months} months present")
    verdict("artifact", "supported" if problems else "refuted",
            "; ".join(problems) if problems else f"{row['rows']} rows, no duplicates, no nulls, {row['months']} months",
            duplicates=row["dup_rows"], months=row["months"])

    # parts of a ratio
    if spec.kind == "ratio":
        sql_c, sql_p = _parts_sql(spec, cur_p), _parts_sql(spec, prev_p)
        if sql_c and sql_p:
            rc, rp = _q(ctx, sql_c), _q(ctx, sql_p)
            evidence += [_ev(rc, f"parts of {spec.name}, {cur_p['label']}"), _ev(rp, f"parts of {spec.name}, {prev_p['label']}")]
            nc, dc = (float(v) if v is not None else None for v in rc.rows[0])
            np_, dp = (float(v) if v is not None else None for v in rp.rows[0])
            num_chg, den_chg = _pct(nc, np_), _pct(dc, dp)
            if num_chg is not None and den_chg is not None:
                mover = "numerator" if abs(num_chg) > abs(den_chg) * 1.5 else (
                    "denominator" if abs(den_chg) > abs(num_chg) * 1.5 else "both")
                verdict("parts", "supported" if mover != "both" else "partial",
                        f"numerator {num_chg:+.1%}, denominator {den_chg:+.1%}: the {mover} moved",
                        numerator_change=num_chg, denominator_change=den_chg, mover=mover,
                        numerator=[np_, nc], denominator=[dp, dc])
            else:
                verdict("parts", "untestable", "a part is empty")
        else:
            verdict("parts", "untestable", "the ratio's parts are not additive")

    ordered = [verdicts[h["id"]] for h in o.get("hypotheses", {}).get("hypotheses", []) if h["id"] in verdicts]
    supported = [v for v in ordered if v["verdict"] == "supported" and v["id"] != "artifact"]
    favoured = None
    if supported:
        pref = ["concentration", "parts", "single_month", "mix", "seasonality", "broad"]
        favoured = sorted(supported, key=lambda v: pref.index(v["id"]) if v["id"] in pref else 99)[0]["id"]
    return {"metric": spec.name, "verdicts": ordered, "favoured": favoured,
            "data_artifact": verdicts.get("artifact", {}).get("verdict") == "supported", "evidence": evidence}


# ---------------------------------------------------------------------- related metrics

@capability(REGISTRY, "related_metrics", agent="business_analyst", phase="investigate", permission="data.query",
            optional=True, description="Other approved metrics on the same dataset, for the same periods: do they corroborate the move?")
def related_metrics(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    spec = _spec(ctx, args["metric"])
    cur_p, prev_p = args["current"], args["prior"]
    out, ev = [], []
    for other in ctx.services.semantic.metrics(ctx.actor, ctx.workspace_id):
        if other.name == spec.name or other.dataset != spec.dataset or not other.time_column:
            continue
        rc = _q(ctx, compile_metric(other, start=cur_p["start"], end=cur_p["end"]))
        rp = _q(ctx, compile_metric(other, start=prev_p["start"], end=prev_p["end"]))
        ev += [_ev(rc, f"{other.name} for {cur_p['label']}"), _ev(rp, f"{other.name} for {prev_p['label']}")]
        c = float(rc.rows[0][0]) if rc.rows and rc.rows[0][0] is not None else None
        p = float(rp.rows[0][0]) if rp.rows and rp.rows[0][0] is not None else None
        out.append({"metric": other.name, "title": other.title, "format": other.format, "current": c, "prior": p,
                    "change": (c - p) if c is not None and p is not None else None, "relative": _pct(c, p),
                    "higher_is_better": other.higher_is_better})
    return {"metrics": out, "evidence": ev}


def summarize_hypotheses(tests: dict[str, Any], spec: MetricSpec, fmt) -> list[str]:
    lines = []
    names = {"concentration": "Concentration in one segment", "broad": "Broad-based move", "mix": "Mix shift",
             "seasonality": "Seasonal pattern", "single_month": "A single month", "artifact": "Data artifact",
             "parts": "Numerator vs denominator"}
    for v in tests.get("verdicts", []):
        mark = {"supported": "supported", "partial": "partly supported", "refuted": "refuted",
                "untestable": "could not be tested"}[v["verdict"]]
        lines.append(f"{names.get(v['id'], v['id'])}: {mark} ({v['detail']}).")
    return lines


__all__ = ["depth_of", "horizon_of", "summarize_hypotheses"]
