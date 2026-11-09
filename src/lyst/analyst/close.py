# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Period-close checks: is a month or quarter ready to report, and what needs a comment before it is.

"Run the month-end close checks", "Is September ready to close?" and "Does the P&L tie to the ledger for Q3?" ask the
questions an analyst works through when a period's data lands. For every dataset behind an approved metric, Lyst checks:

1. **Arrived and complete**: the period has rows, about as many as the periods before it, and every segment (each
   combination of the metrics' approved dimensions) that reported last period reported this one.
2. **Clean**: no exact duplicate rows and no missing amounts in the columns the metrics add up.
3. **Ties out**: where the catalog found a second source that carries the same measures at the same grain (an
   ``overlap``, such as the P&L and the finance ledger), the period's amounts are matched key by key within 0.5%, and
   every break is listed with both amounts.
4. **Flux**: each metric, segment by segment, against the period before and a year earlier. A segment line that moved
   further than that line usually moves (robust z of its own history) is listed as needing a comment.

Everything runs through the SQL guard as the asker and every figure comes from a recorded query. Lyst only reports:
it never posts a journal, edits a dataset or marks a period closed.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from ..semantic import MetricSpec, _q, compile_metric
from . import brief as brief_mod
from . import periods
from .registry import REGISTRY, Context, capability

# "the ledger" as the books, not a dataset named ledger_bank: "reconcile the ledger erp postings against the ledger bank"
# is a reconciliation between two named datasets, which the reconciliation method answers
_END = r"(?=\s*($|[?.!,;]|(for|in|at|this|last|as of)\b))"
ASKS = re.compile(
    r"\b((month|quarter|period|year)[- ]end( close)?|close checks?|closing checks?|ready to close|close the (books|month|"
    r"quarter)|(is|are) (\w+ ){0,3}(closed|ready to (report|close))|tie[sd]?[- ]out|ties? (out )?to the ledger{END}|"
    r"reconcil\w* (\w+ ){0,4}(to|with|against) the (ledger|gl|general ledger|books){END}|flux (review|analysis))\b"
    .replace("{END}", _END), re.I)

TOLERANCE = 0.005  # relative difference a tie-out accepts per key
ROWS_LOW = 0.9  # a period with fewer rows than this share of its recent median is flagged as possibly incomplete
FLUX_Z = 3.5  # a segment line this many robust sigmas from its own usual move needs a comment
MATERIAL_PTS = 0.01  # and it moved at least 1 point (a rate) ...
MATERIAL_REL = 0.05  # ... or 5% (an amount)
FLUX_LIMIT = 8


def asks_for_close(text: str) -> bool:
    return bool(ASKS.search(text or ""))


def understanding(text: str) -> dict[str, Any]:
    grain = "quarter" if re.search(r"\b(quarter(ly)?|q[1-4])\b", text, re.I) else "month"
    return {**brief_mod.understanding(text), "intent": "close", "grain": grain}


def _evidence(r, purpose: str) -> dict[str, Any]:
    from .engine import _evidence as ev  # noqa: PLC0415

    return ev(r, purpose)


def _query(ctx: Context, sql: str):
    return ctx.services.data.query(ctx.actor, ctx.workspace_id, sql, task_id=ctx.task_id)


def _in(spec_time: str, p: periods.Period) -> str:
    t = _q(spec_time)
    return f"{t} >= DATE '{p.start.isoformat()}' AND {t} < DATE '{p.end.isoformat()}'"


def _measures(spec: MetricSpec) -> set[str]:
    out = set()
    for m in (spec.measure, spec.numerator, spec.denominator):
        if m is not None and getattr(m, "column", None):
            out.add(m.column)
    return out


def _arrival(ctx: Context, dataset: str, time_col: str, dims: list[str], measures: list[str], p: periods.Period,
             prev: periods.Period, evidence: list) -> dict[str, Any]:
    t = _q(time_col)
    r = _query(ctx, f"SELECT date_trunc('{p.grain}', {t}) AS period, count(*) AS n FROM {_q(dataset)} "
                    f"WHERE {t} < DATE '{p.end.isoformat()}' GROUP BY 1 ORDER BY 1")
    evidence.append(_evidence(r, f"{dataset}: rows per {p.grain}"))
    counts = {pd.Timestamp(row[0]).tz_localize(None) if pd.Timestamp(row[0]).tzinfo else pd.Timestamp(row[0]): int(row[1])
              for row in r.rows if row[0] is not None}
    rows = counts.get(pd.Timestamp(p.start), 0)
    recent = [n for ts, n in sorted(counts.items()) if ts < pd.Timestamp(p.start)][-6:]
    usual = float(np.median(recent)) if recent else None
    out: dict[str, Any] = {"dataset": dataset, "rows": rows, "usual_rows": usual, "missing_segments": [],
                           "duplicates": 0, "null_amounts": {}}
    if dims and rows:
        cols = ", ".join(_q(d) for d in dims)
        r = _query(ctx, f"SELECT {cols} FROM {_q(dataset)} WHERE {_in(time_col, prev)} GROUP BY {cols} EXCEPT "
                        f"SELECT {cols} FROM {_q(dataset)} WHERE {_in(time_col, p)} GROUP BY {cols}")
        evidence.append(_evidence(r, f"{dataset}: segments in {prev.label} with no rows in {p.label}"))
        out["missing_segments"] = [" / ".join(str(v) for v in row) for row in r.rows][:10]
    if rows:
        r = _query(ctx, f"SELECT (SELECT count(*) FROM {_q(dataset)} WHERE {_in(time_col, p)}) - "
                        f"(SELECT count(*) FROM (SELECT DISTINCT * FROM {_q(dataset)} WHERE {_in(time_col, p)}))")
        evidence.append(_evidence(r, f"{dataset}: exact duplicate rows in {p.label}"))
        out["duplicates"] = int(r.rows[0][0] or 0)
        if measures:
            r = _query(ctx, "SELECT " + ", ".join(f"count(*) - count({_q(m)}) AS {_q(m)}" for m in measures)
                       + f" FROM {_q(dataset)} WHERE {_in(time_col, p)}")
            evidence.append(_evidence(r, f"{dataset}: missing amounts in {p.label}"))
            out["null_amounts"] = {m: int(v) for m, v in zip(r.columns, r.rows[0], strict=False) if v}
    out["complete"] = bool(rows) and not out["missing_segments"] and (usual is None or rows >= ROWS_LOW * usual)
    return out


def _tie_out(ctx: Context, rel: dict[str, Any], time_col: str, p: periods.Period, evidence: list) -> dict[str, Any]:
    a, b = rel["from_dataset"], rel["to_dataset"]
    ev = rel.get("evidence") or {}
    keys = [k for k in ev.get("grain") or [] if k != time_col]
    measures = list(ev.get("measures") or [])[:6]
    cols = ", ".join(_q(k) for k in keys)
    sums = ", ".join(f"sum(CAST({_q(m)} AS DOUBLE)) AS {_q(m)}" for m in measures)

    def side(ds: str) -> pd.DataFrame:
        sql = (f"SELECT {cols + ', ' if cols else ''}{sums} FROM {_q(ds)} WHERE {_in(time_col, p)}"
               + (f" GROUP BY {cols}" if cols else ""))
        r = _query(ctx, sql)
        evidence.append(_evidence(r, f"{ds}: amounts by {', '.join(keys) or 'total'} for {p.label}"))
        return r.frame()

    fa, fb = side(a), side(b)
    if keys:
        m = fa.merge(fb, on=keys, how="outer", suffixes=("__a", "__b"), indicator=True)
    else:
        m = fa.add_suffix("__a").join(fb.add_suffix("__b"))
        m["_merge"] = "both"
    breaks: list[dict[str, Any]] = []
    totals = {}
    for meas in measures:
        va, vb = m[f"{meas}__a"], m[f"{meas}__b"]
        totals[meas] = {a: float(va.sum()), b: float(vb.sum())}
        for i in range(len(m)):
            x, y = va.iloc[i], vb.iloc[i]
            key = " / ".join(str(m[k].iloc[i]) for k in keys) or "total"
            if pd.isna(x) or pd.isna(y):
                breaks.append({"key": key, "measure": meas, a: None if pd.isna(x) else float(x),
                               b: None if pd.isna(y) else float(y),
                               "why": f"missing in {b}" if pd.isna(y) else f"missing in {a}"})
            elif abs(x - y) > TOLERANCE * max(abs(x), abs(y), 1):
                breaks.append({"key": key, "measure": meas, a: float(x), b: float(y), "why": "amounts differ"})
    return {"a": a, "b": b, "keys": keys, "measures": measures, "breaks": breaks, "totals": totals,
            "ties": not breaks, "matched_keys": int(len(m)), "broken_keys": len({br["key"] for br in breaks})}


def _flux(ctx: Context, spec: MetricSpec, p: periods.Period, prev: periods.Period, ya: periods.Period, fs: int,
          evidence: list) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for dim in spec.dimensions:
        try:
            r = _query(ctx, compile_metric(spec, grain=p.grain, by=[dim], fiscal_start=fs, end=p.end.isoformat()))
        except Exception:  # noqa: BLE001, S112 - a split the reader cannot query is skipped
            continue
        evidence.append(_evidence(r, f"{spec.name} by {dim} and {p.grain}"))
        df = r.frame()
        if df.empty:
            continue
        df["period"] = pd.to_datetime(df["period"]).dt.tz_localize(None)
        for member, g in df.groupby(dim):
            s = g.set_index("period")["value"].astype(float).sort_index()
            at = pd.Timestamp(p.start)
            if at not in s.index:
                continue
            value = float(s[at])
            for against, other in (("prior", prev), ("year_ago", ya)):
                lag = pd.DateOffset(months=1 if p.grain == "month" else 3) if against == "prior" else pd.DateOffset(years=1)
                hist = [brief_mod._change(float(v), float(s[i - lag]), spec) for i, v in s.items()
                        if i - lag in s.index and i < at]
                hist = [h for h in hist if h is not None and np.isfinite(h)]
                base = s.get(pd.Timestamp(other.start))
                change = brief_mod._change(value, float(base), spec) if base is not None else None
                if change is None or len(hist) < brief_mod.MIN_HISTORY:
                    continue
                # material: the move differs from this line's usual move by at least a point (a rate) or 5% (an amount)
                if abs(change - float(np.median(hist[-24:]))) < (MATERIAL_PTS if spec.format == "percent" else MATERIAL_REL):
                    continue
                z = brief_mod.robust_z(hist[-24:], change)
                if z is not None and abs(z) >= FLUX_Z:
                    lines.append({"metric": spec.name, "title": spec.title, "dimension": dim, "member": str(member),
                                  "against": against, "label": other.label, "value": value, "base": float(base),
                                  "change": change, "z": float(z), "usual": float(np.median(hist[-24:])),
                                  "good": (change > 0) == spec.higher_is_better})
    best: dict[tuple[str, str], dict[str, Any]] = {}  # one line per segment: its larger move of the two comparisons
    for x in lines:
        k = (x["dimension"], x["member"])
        if k not in best or abs(x["z"]) > abs(best[k]["z"]):
            best[k] = x
    return list(best.values())


@capability(REGISTRY, "close_checks", agent="quality", phase="validate", permission="data.query",
            description="Period-close checks: data arrived and complete, no duplicates or missing amounts, second "
                        "sources tie out key by key, and segment lines that moved more than usual need a comment.")
def close_checks(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from ..onboarding import fiscal_start  # noqa: PLC0415

    s = ctx.services
    u = ctx.outputs["understand"]
    fs = fiscal_start(s.semantic.stores.workspace(ctx.workspace_id))
    evidence: list[dict[str, Any]] = []
    specs = [m for m in s.semantic.metrics(ctx.actor, ctx.workspace_id) if m.time_column]
    covered: dict[str, date] = {}
    readable: list[MetricSpec] = []
    skipped: list[str] = []
    for spec in specs:
        try:
            d, r = brief_mod._covered(ctx, spec)
        except Exception:  # noqa: BLE001
            skipped.append(spec.title)
            continue
        evidence.append(_evidence(r, f"{spec.dataset}: how far the data runs"))
        readable.append(spec)
        if d:
            covered[spec.dataset] = max(covered.get(spec.dataset, d), d)
    if not covered:
        return {"empty": True, "skipped": skipped, "evidence": evidence}
    anchor = pd.Series(list(covered.values())).mode().max()
    p, note = brief_mod._period(u["question"], u.get("grain") or "month", anchor, fs)
    prev, ya = periods.previous(p, fs), periods.year_ago(p, fs)

    datasets: dict[str, dict[str, Any]] = {}
    for spec in readable:
        d = datasets.setdefault(spec.dataset, {"time": spec.time_column, "dims": [], "measures": set()})
        d["dims"] += [x for x in spec.dimensions if x not in d["dims"]]
        d["measures"] |= _measures(spec)
    arrivals = []
    for name, d in sorted(datasets.items()):
        try:
            arrivals.append(_arrival(ctx, name, d["time"], d["dims"], sorted(d["measures"]), p, prev, evidence))
        except Exception:  # noqa: BLE001
            skipped.append(name)
    ties = []
    rels = []
    if getattr(s, "catalog", None) is not None:
        try:
            rels = [r for r in s.catalog.relationships(ctx.actor, ctx.workspace_id)
                    if r["kind"] == "overlap" and (r["from_dataset"] in datasets or r["to_dataset"] in datasets)]
        except Exception:  # noqa: BLE001 - no catalog access means no second source to tie to
            rels = []
    for rel in rels:
        time_col = datasets.get(rel["from_dataset"], datasets.get(rel["to_dataset"], {})).get("time")
        if not time_col or time_col not in (rel.get("evidence") or {}).get("grain", []):
            continue
        try:
            ties.append(_tie_out(ctx, rel, time_col, p, evidence))
        except Exception:  # noqa: BLE001
            skipped.append(f"{rel['from_dataset']} vs {rel['to_dataset']}")
    flux: list[dict[str, Any]] = []
    for spec in readable:
        flux += _flux(ctx, spec, p, prev, ya, fs, evidence)
    # one line per segment: its largest move, with the other metrics that moved with it (one story, not three)
    segs: dict[tuple[str, str], dict[str, Any]] = {}
    for x in sorted(flux, key=lambda x: -abs(x["z"])):
        k = (x["dimension"], x["member"])
        if k in segs:
            segs[k]["also"].append(x["metric"])
        else:
            segs[k] = {**x, "also": []}
    flux = list(segs.values())
    return {"period": {"label": p.label, "start": p.start.isoformat(), "end": p.end.isoformat(), "grain": p.grain},
            "prior_label": prev.label, "year_ago_label": ya.label, "note": note, "anchor": anchor.isoformat(),
            "arrivals": arrivals, "ties": ties, "pairs": len(rels), "flux": flux, "skipped": skipped,
            "data_through": {k: v.isoformat() for k, v in covered.items()}, "evidence": evidence}


def _money(v: float | None) -> str:
    return "none" if v is None else f"{v:,.2f}"


def result(ctx: Context) -> dict[str, Any]:
    o = ctx.outputs
    u, c = o["understand"], o["close"]
    specs = {m.name: m for m in ctx.services.semantic.metrics(ctx.actor, ctx.workspace_id)}
    if c.get("empty"):
        answer = ("There is nothing to check yet: no approved metric with a date can be read here. An admin can approve "
                  "metrics under Brain, then ask again.")
        return brief_mod._shape(u["question"], u, answer, [], [], [], [], [], [], c, status="needs_input")
    p = c["period"]["label"]
    checks: list[dict[str, Any]] = []
    findings: list[str] = []
    actions: list[str] = []
    rows: list[list[Any]] = []

    late = [a for a in c["arrivals"] if not a["rows"]]
    thin = [a for a in c["arrivals"] if a["rows"] and not a["complete"]]
    for a in c["arrivals"]:
        detail = f"{a['rows']:,} rows in {p}" + (f" (usually about {a['usual_rows']:,.0f})" if a["usual_rows"] else "")
        if a["missing_segments"]:
            detail += f"; no rows for {', '.join(a['missing_segments'][:4])}"
        checks.append({"name": f"arrived:{a['dataset']}", "passed": a["complete"], "detail": detail, "severity": "error"})
        rows.append(["Data arrived complete", a["dataset"], "pass" if a["complete"] else "attention", detail])
        clean = not a["duplicates"] and not a["null_amounts"]
        cd = ("no duplicate rows or missing amounts" if clean else
              ", ".join(([f"{a['duplicates']} duplicate row{'s' if a['duplicates'] != 1 else ''}"] if a["duplicates"] else [])
                        + [f"{n} missing {m}" for m, n in a["null_amounts"].items()]))
        if a["rows"]:
            checks.append({"name": f"clean:{a['dataset']}", "passed": clean, "detail": cd, "severity": "error"})
            rows.append(["Clean", a["dataset"], "pass" if clean else "attention", cd])
    for t in c["ties"]:
        if t["ties"]:
            d = f"{t['a']} and {t['b']} agree on {', '.join(t['measures'])} for all {t['matched_keys']} keys"
        else:
            n = t["broken_keys"]
            first = t["breaks"][0]
            d = (f"{n} key{'s' if n != 1 else ''} do{'es' if n == 1 else ''} not match; for example {first['key']} "
                 f"{first['measure']}: {t['a']} {_money(first[t['a']])}, {t['b']} {_money(first[t['b']])} ({first['why']})")
            by_key: dict[str, list[dict[str, Any]]] = {}
            for br in t["breaks"]:
                by_key.setdefault(br["key"], []).append(br)
            for key, brs in list(by_key.items())[:6]:
                br = brs[0]
                findings.append(f"Tie-out break, {key}: {br['measure']} {_money(br[t['a']])} in {t['a']} vs "
                                f"{_money(br[t['b']])} in {t['b']} ({br['why']})"
                                + (f"; {', '.join(x['measure'] for x in brs[1:])} differ too" if len(brs) > 1 else "") + ".")
            actions.append(f"Ask the owner of {t['b']} about the {n} key{'s' if n != 1 else ''} that do not tie before "
                           f"{p} is reported.")
        checks.append({"name": f"ties_out:{t['a']}~{t['b']}", "passed": t["ties"], "detail": d, "severity": "error"})
        rows.append(["Ties out", f"{t['a']} vs {t['b']}", "pass" if t["ties"] else "attention", d])
    flux = c["flux"][:FLUX_LIMIT]
    for x in flux:
        spec = specs.get(x["metric"])
        if not spec:
            continue
        findings.append(f"Flux: {spec.title}, {x['member']} ({x['dimension'].replace('_', ' ')}): "
                        f"{brief_mod._fmt(x['value'], spec)} in {p} vs {brief_mod._fmt(x['base'], spec)} in {x['label']} "
                        f"({brief_mod._change_text(x['change'], spec)}; it usually moves "
                        f"{brief_mod._change_text(x['usual'], spec)})"
                        + (f"; {', '.join(specs[m].title.lower() if not specs[m].title[:2].isupper() else specs[m].title for m in x['also'] if m in specs)} "
                           "moved with it" if x.get("also") else "") + ".")
    checks.append({"name": "flux", "passed": not flux, "severity": "warning",
                   "detail": (f"{len(c['flux'])} segment line{'s' if len(c['flux']) != 1 else ''} moved more than usual"
                              if c["flux"] else "no segment line moved more than usual")})
    rows.append(["Flux review", "every metric by segment", "pass" if not flux else "comment needed",
                 f"{len(c['flux'])} line{'s' if len(c['flux']) != 1 else ''} to explain" if c["flux"] else "nothing unusual"])
    if flux:
        x = flux[0]
        name = specs[x["metric"]].title if x["metric"] in specs else x["metric"]
        actions.insert(0, f"Ask why {name.lower() if not name[:2].isupper() else name} moved in {x['member']} "
                          f"between {x['label']} and {p}, and record the explanation with the close.")
    if late:
        actions.insert(0, f"Load {p} for {', '.join(a['dataset'] for a in late)}, then run the checks again.")
    if thin:
        actions.append(f"Confirm with the data owner that {', '.join(a['dataset'] for a in thin)} is complete for {p}.")
    if c["skipped"]:
        checks.append({"name": "access_scope", "passed": True, "severity": "warning",
                       "detail": f"not checked (not readable with your access): {', '.join(c['skipped'])}"})

    attention = [r for r in rows if r[2] != "pass"]
    if not attention:
        answer = f"{p} looks ready to close: every check passed and no segment line moved more than usual."
    else:
        parts = []
        if late:
            parts.append(f"{', '.join(a['dataset'] for a in late)} has no data for {p} yet")
        if thin:
            parts.append(f"{', '.join(a['dataset'] for a in thin)} may be incomplete")
        dirty = [r for r in rows if r[0] == "Clean" and r[2] != "pass"]
        if dirty:
            parts.append(f"{', '.join(r[1] for r in dirty)} has duplicate rows or missing amounts")
        broken = [t for t in c["ties"] if not t["ties"]]
        for t in broken:
            parts.append(f"{t['a']} and {t['b']} do not tie on {t['broken_keys']} key{'s' if t['broken_keys'] != 1 else ''}")
        if c["flux"]:
            parts.append(f"{len(c['flux'])} segment line{'s' if len(c['flux']) != 1 else ''} moved more than usual and "
                         f"need{'s' if len(c['flux']) == 1 else ''} a comment")
        passed = len(rows) - len(attention)
        answer = (f"{p} is not ready to close yet: {'; '.join(parts)}. "
                  f"{passed} of {len(rows)} checks passed.")
        tied = [t for t in c["ties"] if t["ties"]]
        if tied:
            answer += " " + "; ".join(f"{t['a']} ties to {t['b']}" for t in tied) + "."
    implications = []
    if c["flux"]:
        x = c["flux"][0]
        name = specs[x["metric"]].title if x["metric"] in specs else x["metric"]
        implications.append(f"The largest unexplained move is {name} in {x['member']}; a reader of the {p} numbers will "
                            "ask about it first.")
    if any(not t["ties"] for t in c["ties"]):
        implications.append("Figures reported from one source will not match the other until the breaks are resolved.")
    tables = [{"title": f"Close checks, {p}", "columns": ["check", "on", "result", "detail"], "rows": rows}]
    if flux:
        tables.append({"title": f"Lines to explain, {p}", "columns": ["metric", "segment", p, "against", "change"],
                       "rows": [[specs[x["metric"]].title if x["metric"] in specs else x["metric"],
                                 f"{x['member']} ({x['dimension'].replace('_', ' ')})",
                                 brief_mod._fmt(x["value"], specs[x["metric"]]) if x["metric"] in specs else x["value"],
                                 x["label"],
                                 brief_mod._change_text(x["change"], specs[x["metric"]]) if x["metric"] in specs else x["change"]]
                                for x in flux]})
    for t in c["ties"]:
        if t["breaks"]:
            tables.append({"title": f"Tie-out breaks, {t['a']} vs {t['b']}",
                           "columns": ["key", "measure", t["a"], t["b"], "why"],
                           "rows": [[b["key"], b["measure"], b[t["a"]], b[t["b"]], b["why"]] for b in t["breaks"][:50]]})
    today = datetime.now(UTC).date()
    assumptions = [f"Lyst reports on the close; it does not post journals, change data or mark {p} closed.",
                   f"A tie-out matches amounts per key within {TOLERANCE:.1%}; a period with fewer than {ROWS_LOW:.0%} of "
                   f"its recent median rows is flagged as possibly incomplete; a segment line needs a comment when its "
                   f"change is at least {FLUX_Z:g} robust standard deviations from that line's own usual change and at least "
                   f"{MATERIAL_PTS * 100:g} point (a rate) or {MATERIAL_REL:.0%} (an amount).",
                   "Data through: " + ", ".join(f"{k} {v} ({brief_mod._ago(date.fromisoformat(v), today)})"
                                                for k, v in sorted(c["data_through"].items())) + "."]
    if c.get("note"):
        assumptions.insert(0, c["note"])
    if not c.get("pairs"):
        assumptions.append("No second source carries the same measures at the same grain, so there was nothing to tie "
                           "out against.")
    out = brief_mod._shape(u["question"], u, answer, findings, implications, actions, tables, [], checks, c,
                           status="complete", assumptions=assumptions)
    out["mode"] = "close"
    out["supporting"]["plan"] = [{"capability": "close_checks", "agent": "quality"}]
    out["supporting"]["workflow"]["strategy"] = "close"
    out["supporting"]["brief"] = None
    out["supporting"]["close"] = {"period": c["period"], "ready": not attention,
                                  "ties": [{k: t[k] for k in ("a", "b", "ties", "matched_keys", "broken_keys")}
                                           for t in c["ties"]], "flux": len(c["flux"])}
    # a failed check is a finding of the close, not a failure of the analysis: the answer is still verified
    out["confidence"] = {"level": "verified", "basis": "the close checks below are the answer; each was computed",
                         "checks": checks}
    return out


__all__ = ["ASKS", "asks_for_close", "close_checks", "result", "understanding"]
