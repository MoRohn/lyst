# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The business brief: every approved metric for one period, what moved more than usual, why, and what needs a person.

"Give me a weekly KPI brief", "What changed this month?" and "Board briefing on Q3 2026" name no metric: they ask about
all of them. The brief reads each approved metric the asker may query (through the SQL guard, so row and column policies
apply), for the last complete month or quarter (or the one named):

* **value, against the period before and the same period a year earlier**;
* **unusual or not**: the change is compared with the metric's own history of period-to-period changes (robust z, median
  and MAD), so a 3% move in a volatile metric is normal and a 1 point move in a steady ratio is not; a run of moves in
  the same direction is named too;
* **where it came from**: for the biggest unusual moves, the one segment of an approved dimension that explains most of
  the change (rates split into rate and mix exactly, like the driver analysis);
* **what is already being watched**, open alerts, approvals waiting, and how current each dataset is.

Every figure is computed by a query recorded in Supporting work; nothing is written by a model. Asking it with "Repeat it
weekly" makes it a schedule like any other answer, so it arrives without being asked.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from ..analytics import drivers
from ..semantic import MetricSpec, _cond, _q, compile_metric
from . import periods
from .registry import REGISTRY, Context, capability

# A request about the business as a whole, not one figure. Checked only when the request names no metric, so
# "brief me on revenue" stays a revenue answer.
ASKS = re.compile(
    r"\b(brief(ing)?|digest|kpis?|scorecard|recap|round[- ]?up|business review|qbr|mbr|"
    r"what('s| has| have)? (changed|moved|happened)|anything (changed|unusual|i should know)|"
    r"how (are|is) (we|the business|business) doing|how did we do|state of the business|"
    r"(weekly|monthly|quarterly) (update|summary|report)|summary of (the |our )?(business|metrics|numbers))\b", re.I)
_QUARTER = re.compile(r"\b(quarter(ly)?|qbr|q[1-4])\b", re.I)
_EXEC = re.compile(r"\b(board|executive|exec|leadership|ceo|cfo)\b", re.I)

UNUSUAL_Z = 2.5  # a period-to-period change this many robust sigmas from the metric's usual change
MIN_HISTORY = 6  # changes needed before "unusual" is judged at all
EXPLAIN_TOP = 3  # unusual moves that get a driver


# "Brief me on what our documents say about the Data Act" asks about documents, not the business's numbers
ELSEWHERE = re.compile(r"\b(documents?|docs|files?|contracts?|polic(y|ies)|regulations?|news|articles?|emails?|"
                       r"(says?|said) about|transcripts?|filings?|web(site)?)\b", re.I)


def asks_for_brief(text: str) -> bool:
    return bool(ASKS.search(text or "")) and not ELSEWHERE.search(text or "")


# "Brief me on revenue" names a metric and still asks for a brief, scoped to it
NAMED = re.compile(r"\b(brief(ing)?|digest|recap|scorecard|round[- ]?up)\b", re.I)


def asks_for_metric_brief(text: str) -> bool:
    return bool(NAMED.search(text or "")) and not ELSEWHERE.search(text or "")


def understanding(text: str, metrics: list[str] | None = None) -> dict[str, Any]:
    """What ``understand`` returns for a brief: the metrics it covers (none named means all of them), the grain, and
    who it is for."""
    grain = "quarter" if _QUARTER.search(text) else "month"
    return {"question": text, "intent": "brief", "metrics": list(metrics or []), "matched": {}, "ambiguous": {},
            "precedence": {},
            "by": None, "dataset": None, "method": "rules", "grain": grain, "depth": "standard",
            "audience": "executive" if _EXEC.search(text) else "analyst", "filters": [], "workflow": None}


def _fmt(value: float | None, spec: MetricSpec, *, delta: bool = False) -> str:
    from .engine import fmt  # noqa: PLC0415 - the engine imports this module

    return fmt(value, spec, delta=delta)


def _change(cur: float, prev: float | None, spec: MetricSpec) -> float | None:
    """Points for a percent metric, a relative change otherwise."""
    if prev is None:
        return None
    if spec.format == "percent":
        return cur - prev
    return (cur - prev) / abs(prev) if prev else None


def _change_text(change: float | None, spec: MetricSpec) -> str:
    if change is None:
        return "n/a"
    if spec.format == "percent":
        return f"{change * 100:+.1f} pts"
    return f"{change * 100:+.1f}%"


def robust_z(history: list[float], latest: float) -> float | None:
    """How far ``latest`` sits from the usual value in ``history``, in robust sigmas (median and 1.4826 x MAD)."""
    if len(history) < MIN_HISTORY:
        return None
    h = np.asarray(history, dtype=float)
    med = float(np.median(h))
    mad = float(np.median(np.abs(h - med))) * 1.4826
    if mad < 1e-12:
        spread = float(np.std(h))
        if spread < 1e-12:
            return 0.0 if abs(latest - med) < 1e-12 else float(np.sign(latest - med)) * 10.0
        mad = spread
    return (latest - med) / mad


def streak(changes: list[float]) -> int:
    """How many periods in a row, ending with the last, moved in the same direction."""
    if not changes or changes[-1] == 0:
        return 0
    sign, n = np.sign(changes[-1]), 0
    for c in reversed(changes):
        if np.sign(c) != sign:
            break
        n += 1
    return n


def _series(ctx: Context, spec: MetricSpec, grain: str, fs: int) -> tuple[pd.Series, Any]:
    r = ctx.services.data.query(ctx.actor, ctx.workspace_id, compile_metric(spec, grain=grain, fiscal_start=fs),
                                task_id=ctx.task_id)
    pts = []
    for row in r.rows:
        if row[0] is None or row[-1] is None:
            continue
        ts = pd.Timestamp(row[0])
        pts.append((ts.tz_localize(None) if ts.tzinfo else ts, float(row[-1])))
    s = pd.Series([v for _, v in pts], index=pd.DatetimeIndex([p for p, _ in pts]), dtype=float).sort_index()
    return s, r


def _covered(ctx: Context, spec: MetricSpec) -> tuple[date | None, Any]:
    col = _q(spec.time_column)
    r = ctx.services.data.query(ctx.actor, ctx.workspace_id,
                                f"SELECT max({col}) AS d, max(day({col})) AS maxday FROM {_q(spec.dataset)}",
                                task_id=ctx.task_id)
    if not r.rows:
        return None, r
    return periods.covered_through(r.rows[0][0], r.rows[0][1]), r


def _frame(ctx: Context, spec: MetricSpec, p: periods.Period, exprs: dict[str, str]):
    cols = ", ".join(_q(d) for d in spec.dimensions)
    aggs = ", ".join(f"SUM({e}) AS {k}" for k, e in exprs.items())
    tc = _q(spec.time_column)
    where = [f"{tc} >= DATE '{p.start.isoformat()}'", f"{tc} < DATE '{p.end.isoformat()}'"] + [_cond(f) for f in spec.filters]
    sql = f"SELECT {cols}, {aggs} FROM {_q(spec.dataset)} WHERE {' AND '.join(where)} GROUP BY {cols}"
    r = ctx.services.data.query(ctx.actor, ctx.workspace_id, sql, task_id=ctx.task_id)
    return r.frame(), r


def _driver(ctx: Context, spec: MetricSpec, cur: periods.Period, prev: periods.Period) -> tuple[dict | None, list]:
    """The one segment that explains most of the change, or None when the metric cannot be split additively."""
    from .engine import _evidence, _sum_target  # noqa: PLC0415

    if not spec.dimensions:
        return None, []
    try:
        if spec.kind == "ratio":
            num, den = _sum_target(spec.numerator), _sum_target(spec.denominator)
            if not (num and den):
                return None, []
            a, r1 = _frame(ctx, spec, cur, {"num": num, "den": den})
            b, r0 = _frame(ctx, spec, prev, {"num": num, "den": den})
            best = drivers.explain_rate(a, b, "num", "den", spec.dimensions)
        else:
            target = _sum_target(spec.measure)
            if not target:
                return None, []
            a, r1 = _frame(ctx, spec, cur, {"value": target})
            b, r0 = _frame(ctx, spec, prev, {"value": target})
            best = drivers.explain(a, b, "value", spec.dimensions)
    except Exception:  # noqa: BLE001 - a driver is a refinement; the brief stands without it
        return None, []
    if not best or best.get("share") is None or not np.isfinite(best["share"]):
        return None, []
    ev = [_evidence(r1, f"{spec.name} by segment, {cur.label}"), _evidence(r0, f"{spec.name} by segment, {prev.label}")]
    return {"dimension": best["dimension"], "member": str(best["member"]), "share": float(best["share"])}, ev


def _period(question: str, grain: str, anchor: date, fs: int) -> tuple[periods.Period, str | None]:
    named = periods.parse(question, anchor, fs)
    if named and named.grain in ("month", "quarter"):
        if named.end <= date.fromordinal(anchor.toordinal() + 1):
            return named, None
        done = periods.last_complete(named.grain, anchor, fs)
        return done, (f"{named.label} is not complete in the data yet (it runs to {anchor:%d %b %Y}), so the brief "
                      f"covers {done.label}.")
    return periods.last_complete(grain, anchor, fs), None


def _ago(d: date, today: date) -> str:
    days = (today - d).days
    return "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")


@capability(REGISTRY, "business_brief", agent="lead_analyst", phase="execute", permission="data.query",
            description="Every approved metric for one period against the period before and a year earlier: what moved "
                        "more than usual, the segment behind it, alerts, approvals and how current the data is.")
def business_brief(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from ..onboarding import fiscal_start  # noqa: PLC0415
    from .engine import _evidence  # noqa: PLC0415

    s = ctx.services
    u = ctx.outputs["understand"]
    fs = fiscal_start(s.semantic.stores.workspace(ctx.workspace_id))
    specs = [m for m in s.semantic.metrics(ctx.actor, ctx.workspace_id) if m.time_column
             and (not u.get("metrics") or m.name in u["metrics"])]
    evidence: list[dict[str, Any]] = []
    covered: dict[str, date] = {}
    skipped: list[str] = []
    for spec in specs:
        try:
            d, r = _covered(ctx, spec)
        except Exception:  # noqa: BLE001 - a metric the asker may not query is left out, not a failed brief
            skipped.append(spec.title)
            continue
        evidence.append(_evidence(r, f"{spec.dataset}: how far the data runs"))
        if d:
            covered[spec.name] = d
    if not covered:
        return {"empty": True, "skipped": skipped, "evidence": evidence}
    # the period follows the data most metrics have reached, so one late extract does not hold back the whole brief
    anchor = pd.Series(list(covered.values())).mode().max()
    p, note = _period(u["question"], u.get("grain") or "month", anchor, fs)
    prev, ya = periods.previous(p, fs), periods.year_ago(p, fs)
    rows: list[dict[str, Any]] = []
    for spec in specs:
        if spec.name not in covered:
            continue
        if covered[spec.name] < date.fromordinal(p.end.toordinal() - 1):
            rows.append({"metric": spec.name, "title": spec.title, "late": True, "through": covered[spec.name].isoformat()})
            continue
        try:
            series, r = _series(ctx, spec, p.grain, fs)
        except Exception:  # noqa: BLE001
            skipped.append(spec.title)
            continue
        evidence.append(_evidence(r, f"{spec.name} by {p.grain}"))
        series = series[series.index < pd.Timestamp(p.end)]
        at = pd.Timestamp(p.start)
        if at not in series.index:
            rows.append({"metric": spec.name, "title": spec.title, "late": True, "through": covered[spec.name].isoformat()})
            continue
        value = float(series[at])
        prior = float(series[pd.Timestamp(prev.start)]) if pd.Timestamp(prev.start) in series.index else None
        year = float(series[pd.Timestamp(ya.start)]) if pd.Timestamp(ya.start) in series.index else None
        steps = [_change(float(b), float(a), spec) for a, b in zip(series.iloc[:-1], series.iloc[1:], strict=False)]
        steps = [c for c in steps if c is not None and np.isfinite(c)]
        change = _change(value, prior, spec)
        z = robust_z(steps[:-1][-24:], steps[-1]) if change is not None and steps else None
        # a level that moved and then held (a step change) looks normal month to month, so the gap to a year earlier is
        # read against the metric's own year-over-year gaps as well
        yoy = _change(value, year, spec)
        yoys = [_change(float(v), float(series[i - pd.DateOffset(years=1)]), spec)
                for i, v in series.items() if i - pd.DateOffset(years=1) in series.index]
        yoys = [c for c in yoys if c is not None and np.isfinite(c)]
        z_yoy = robust_z(yoys[:-1][-24:], yoys[-1]) if yoy is not None and yoys else None
        basis = None
        if z is not None and abs(z) >= UNUSUAL_Z and change is not None and abs(change) >= 5e-4:
            basis = "prior"
        elif z_yoy is not None and abs(z_yoy) >= UNUSUAL_Z and yoy is not None and abs(yoy) >= 5e-4:
            basis = "year_ago"
        moved = {"prior": change, "year_ago": yoy}.get(basis or "prior")
        flat = moved is None or abs(moved) < 5e-4
        good = None if flat else ((moved > 0) == spec.higher_is_better)
        rows.append({
            "metric": spec.name, "title": spec.title, "spec": spec, "value": value, "prior": prior, "year_ago": year,
            "change": change, "yoy": yoy, "z": z, "z_yoy": z_yoy,
            "usual_yoy": float(np.median(yoys[:-1][-24:])) if len(yoys) > 1 else None, "good": good, "basis": basis,
            "unusual": basis is not None, "streak": streak(steps),
            "history": [{"label": periods._quarter(i.date(), fs).label if p.grain == "quarter" else i.strftime("%b %Y"),
                         "value": float(v)} for i, v in series.iloc[-13:].items()],
        })
    live = [x for x in rows if not x.get("late")]

    def size(x: dict[str, Any]) -> float:
        return abs(x["z"] if x["basis"] == "prior" else x["z_yoy"])

    unusual = sorted([x for x in live if x["unusual"]], key=lambda x: (x["good"] is not False, -size(x)))
    # a brief on one metric always says where its move came from, unusual or not
    explain = unusual[:EXPLAIN_TOP] if len(live) > 1 else live
    for x in explain:
        drv, ev = _driver(ctx, x["spec"], p, prev if x["basis"] != "year_ago" else ya)
        x["driver"] = drv
        evidence += ev
    watched: set[str] = set()
    alerts: list[dict[str, Any]] = []
    if getattr(s, "monitors", None) is not None:
        try:
            watched = {m["metric"] for m in s.monitors.list(ctx.actor, ctx.workspace_id) if m.get("enabled")}
            alerts = [{"summary": f["summary"], "metric": f["metric"], "task_id": f.get("task_id")}
                      for f in s.monitors.findings(ctx.actor, ctx.workspace_id, status="new", limit=5)]
        except Exception:  # noqa: BLE001 - alerts are context; a reader without access just gets none
            watched, alerts = set(), []
    waiting = 0
    if s.runtime is not None:
        try:
            waiting = len(s.runtime.approvals(ctx.actor, ctx.workspace_id))
        except Exception:  # noqa: BLE001
            waiting = 0
    return {"period": {"label": p.label, "start": p.start.isoformat(), "end": p.end.isoformat(), "grain": p.grain},
            "prior_label": prev.label, "year_ago_label": ya.label, "note": note, "anchor": anchor.isoformat(),
            "rows": [{k: v for k, v in x.items() if k != "spec"} for x in rows],
            "unusual": [x["metric"] for x in unusual], "watched": sorted(watched), "alerts": alerts,
            "approvals_waiting": waiting, "skipped": skipped,
            "data_through": {k: v.isoformat() for k, v in covered.items()}, "evidence": evidence}


def _driver_text(x: dict[str, Any]) -> str:
    d = x.get("driver")
    if not d:
        return ""
    share = d["share"]
    if share > 1.2:  # other segments moved the other way
        return f", driven by {d['member']} ({d['dimension'].replace('_', ' ')}) while other segments offset part of it"
    if share >= 0.6:
        return f", mostly {d['member']} ({d['dimension'].replace('_', ' ')}, {share:.0%} of the change)"
    if share >= 0.3:
        return f", led by {d['member']} ({d['dimension'].replace('_', ' ')}, {share:.0%} of the change)"
    return ", spread across segments"


def _move(x: dict[str, Any], spec: MetricSpec, b: dict[str, Any]) -> str:
    against = x.get("basis") or "prior"
    delta = x["change"] if against == "prior" else x["yoy"]
    label = b["prior_label"] if against == "prior" else b["year_ago_label"]
    amount = f"{abs(delta) * 100:.1f} pts" if spec.format == "percent" else f"{abs(delta) * 100:.1f}%"
    if against == "prior":
        return f"{spec.title} {'rose' if delta > 0 else 'fell'} {amount} to {_fmt(x['value'], spec)} (vs {label})"
    text = f"{spec.title} is {amount} {'above' if delta > 0 else 'below'} {label}, at {_fmt(x['value'], spec)}"
    usual = x.get("usual_yoy")
    if usual is not None and abs(usual) >= 5e-4:
        text += f" (it is usually {_change_text(usual, spec)} year over year)"
    return text


def result(ctx: Context) -> dict[str, Any]:
    """The task result for a brief, in the shape every answer viewer reads."""
    o = ctx.outputs
    u, b = o["understand"], o["brief"]
    specs = {m.name: m for m in ctx.services.semantic.metrics(ctx.actor, ctx.workspace_id)}
    question = u["question"]
    checks: list[dict[str, Any]] = []
    if b.get("empty"):
        answer = ("There is nothing to brief on yet: no approved metric with a date can be read here. "
                  "An admin can approve metrics under Brain, then ask again.")
        return _shape(question, u, answer, [], [], [], [], [], checks, b, status="needs_input")
    p = b["period"]["label"]
    rows = [x for x in b["rows"] if not x.get("late")]
    late = [x for x in b["rows"] if x.get("late")]
    unusual = [next(x for x in rows if x["metric"] == m) for m in b["unusual"]]
    calm = [x for x in rows if x["metric"] not in b["unusual"]]
    lead: list[str] = []
    if len(rows) == 1 and not late:
        x = rows[0]
        spec = specs[x["metric"]]
        lead.append(f"{spec.title} in {p}: {_fmt(x['value'], spec)}, {_change_text(x['change'], spec)} vs {b['prior_label']}"
                    + (f" and {_change_text(x['yoy'], spec)} vs {b['year_ago_label']}" if x.get("yoy") is not None else "")
                    + (f" ({'an unusual gap to ' + b['year_ago_label'] if x['basis'] == 'year_ago' else 'a bigger move than usual'})"
                       if x["unusual"] else ", within its normal range")
                    + (_driver_text(x) if x.get("driver") and x.get("change") else "") + ".")
        if x.get("streak", 0) >= 3:
            lead.append(f"It has moved {'up' if (x['change'] or 0) > 0 else 'down'} {x['streak']} "
                        f"{b['period']['grain']}s in a row.")
    elif unusual:
        lead.append(f"{p}: {len(unusual)} of {len(rows)} metrics moved more than usual.")
        lead.append("; ".join(_move(x, specs[x["metric"]], b) + _driver_text(x) for x in unusual[:3]) + ".")
        if calm:
            lead.append(f"The other {len(calm)} stayed within their normal range.")
    else:
        lead.append(f"{p}: nothing moved more than usual across {len(rows)} metric{'s' if len(rows) != 1 else ''}.")
        big = max(rows, key=lambda x: abs(x["change"] or 0), default=None)
        if big and big["change"]:
            lead.append(f"The biggest move: {_move(big, specs[big['metric']], b)}.")
    answer = " ".join(lead)

    findings: list[str] = []
    for x in unusual + calm:
        spec = specs[x["metric"]]
        line = f"{spec.title}: {_fmt(x['value'], spec)} in {p}, {_change_text(x['change'], spec)} vs {b['prior_label']}"
        if x.get("yoy") is not None:
            line += f", {_change_text(x['yoy'], spec)} vs {b['year_ago_label']}"
        tag = {"prior": f"a bigger move than usual vs {b['prior_label']}",
               "year_ago": f"an unusual gap to {b['year_ago_label']}"}.get(x.get("basis") or "", "within its normal range")
        if x.get("streak", 0) >= 3:
            tag += f"; {x['streak']} {b['period']['grain']}s in a row {'up' if (x['change'] or 0) > 0 else 'down'}"
        findings.append(f"{line} ({tag}).")
    for x in late:
        findings.append(f"{x['title']} has no data for {p} yet (its data runs to {x['through']}).")

    implications: list[str] = []
    bad = [x for x in unusual if x["good"] is False]
    for x in bad[:2]:
        d = x.get("driver")
        spec = specs[x["metric"]]
        if d and d["share"] >= 0.6:
            implications.append(f"{spec.title}'s move is concentrated in {d['member']}, so a response there matters more "
                                 "than a company-wide one.")
        else:
            implications.append(f"{spec.title} moved the wrong way by more than its usual variation.")
    for x in calm:
        if x.get("streak", 0) >= 3 and x["good"] is False:
            implications.append(f"{specs[x['metric']].title} is within its normal range but has moved the wrong way "
                                f"{x['streak']} {b['period']['grain']}s in a row.")
    if late:
        implications.append(f"{len(late)} metric{'s are' if len(late) != 1 else ' is'} not in this brief because the data "
                            f"for {p} has not arrived.")

    actions: list[str] = []
    for x in unusual[:EXPLAIN_TOP]:
        spec = specs[x["metric"]]
        name = spec.title.lower() if not spec.title[:2].isupper() else spec.title
        if x["basis"] == "prior":
            actions.append(f"Ask why {name} {'rose' if (x['change'] or 0) > 0 else 'fell'} in {p}.")
        else:
            actions.append(f"Ask why {name} changed between {b['year_ago_label']} and {p}.")
        if x["metric"] not in b["watched"]:
            actions.append(f"Watch {spec.title} so the next unusual move is caught and investigated as it lands.")
    if b["alerts"]:
        actions.append(f"Review {len(b['alerts'])} open alert{'s' if len(b['alerts']) != 1 else ''} from the metrics Lyst "
                       "is watching.")
    if b["approvals_waiting"]:
        actions.append(f"Decide {b['approvals_waiting']} approval{'s' if b['approvals_waiting'] != 1 else ''} waiting for "
                       "you in Work.")
    if not unusual and not b["watched"]:
        actions.append("Repeat this brief weekly so changes reach you without asking.")

    table = {"title": f"Metrics, {p}",
             "columns": ["metric", p, f"vs {b['prior_label']}", f"vs {b['year_ago_label']}", "read"],
             "rows": [[specs[x["metric"]].title, _fmt(x["value"], specs[x["metric"]]),
                       _change_text(x["change"], specs[x["metric"]]), _change_text(x.get("yoy"), specs[x["metric"]]),
                       {"prior": f"unusual vs {b['prior_label']}", "year_ago": f"unusual vs {b['year_ago_label']}"}.get(
                           x.get("basis") or "", "normal")] for x in unusual + calm]}
    charts = []
    if unusual:
        x = unusual[0]
        spec = specs[x["metric"]]
        charts.append({"type": "bar", "title": f"{spec.title} by {b['period']['grain']}",
                       "x": [h["label"] for h in x["history"]], "y": [h["value"] for h in x["history"]],
                       "format": spec.format})

    today = datetime.now(UTC).date()
    for m, through in sorted(b["data_through"].items()):
        d = date.fromisoformat(through)
        checks.append({"name": f"coverage:{m}", "passed": d.toordinal() >= date.fromisoformat(b["period"]["end"]).toordinal() - 1,
                       "detail": f"data runs to {through} ({_ago(d, today)})", "severity": "warning"})
    checks.append({"name": "numbers_backed", "passed": True,
                   "detail": f"every figure comes from the {len(b['evidence'])} queries in Supporting work",
                   "severity": "error"})
    if b["skipped"]:
        checks.append({"name": "access_scope", "passed": True, "severity": "warning",
                       "detail": f"left out (not readable with your access): {', '.join(b['skipped'])}"})
    assumptions = [f"Each metric uses its approved definition; 'more than usual' means a change at least "
                   f"{UNUSUAL_Z:g} robust standard deviations from that metric's own {b['period']['grain']}-to-"
                   f"{b['period']['grain']} changes, or a gap to the same {b['period']['grain']} a year earlier that far from its usual "
                   "year-over-year gap (median and MAD of up to 24 earlier values each).",
                   f"Periods are anchored on the data's latest date ({b['anchor']})."]
    if b.get("note"):
        assumptions.insert(0, b["note"])
    return _shape(question, u, answer, findings, implications, actions, [table], charts, checks, b,
                  status="complete", assumptions=assumptions)


def _shape(question: str, u: dict[str, Any], answer: str, findings: list[str], implications: list[str],
           actions: list[str], tables: list[dict], charts: list[dict], checks: list[dict], b: dict[str, Any], *,
           status: str, assumptions: list[str] | None = None) -> dict[str, Any]:
    warn = any(not c["passed"] for c in checks)
    level = "not verified" if status != "complete" else ("verified with warnings" if warn else "verified")
    audience = u.get("audience", "analyst")
    brief = answer if audience != "executive" else " ".join([answer] + implications[:2] + actions[:1])
    return {
        "status": status,
        "mode": "brief",
        "depth": "standard",
        "audience": audience,
        "brief": brief,
        "question": question,
        "answer": answer,
        "hypotheses": [],
        "evidence": findings,
        "implications": implications,
        "actions": actions,
        "charts": charts,
        "tables": tables,
        "open": None,
        "confidence": {"level": level, "basis": "validation checks below; not a model probability", "checks": checks},
        "supporting": {"queries": b.get("evidence", []), "sources": [], "citations": [], "consistency": None,
                       "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "metric": None,
                       "assumptions": assumptions or [], "plan": [{"capability": "business_brief", "agent": "lead_analyst"}],
                       "narrative_by": "template", "expertise": None, "brief": {
                           "period": b.get("period"), "unusual": b.get("unusual", []), "watched": b.get("watched", []),
                           "alerts": b.get("alerts", []), "data_through": b.get("data_through", {})},
                       "workflow": {"used": None, "strategy": "brief", "compiled": None},
                       "reproduce": "Re-run each query in Supporting work; the brief recomputes from the same data."},
    }


__all__ = ["ASKS", "asks_for_brief", "business_brief", "result", "robust_z", "streak", "understanding"]
