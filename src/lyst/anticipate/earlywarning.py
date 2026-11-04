# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Early warning on monitored metrics: a developing adverse trend found before it crosses the monitor's alert line, then
investigated before anyone is told.

Scope is what people already authorized: metrics with an approved, enabled monitor, read as the monitor's creator. The
monitor still raises its own alert when a period crosses its threshold; this module looks for what comes before that.

Detection (deterministic, on complete periods only; at least 12):

* **Change point**: a new level of at least three periods that began in the last five (binary segmentation with a
  BIC-style penalty, `analytics.changepoint`), at least 2 sigma in the adverse direction.
* **Sustained trend**: the last four periods each moved the adverse way and the total move is at least 1.5 robust sigma.
* **Forecast drift**: refit without the last three periods; all three land on the adverse side of the forecast and at
  least two outside its 80% interval.
* **Projected breach**: the forecast of the next two periods crosses the monitor's alert line (trailing median plus or
  minus its sigma threshold).

A change point and a forecast drift are two views of one level shift, so evidence is counted in three kinds (level,
trend, projection). A warning needs two kinds, or a change point of at least 4 sigma. Suppressed before anything is
surfaced: stale data (the series ends more than two periods ago), a move that repeats the same periods last year
(seasonal), one kind of evidence only, and a period the monitor already alerted on. The thresholds are calibrated in
``scripts/bench_earlywarning.py`` (false-positive rate on noise, detection rate and lead on planted declines).

At level 3 the warning is investigated as a read-only background task (kind ``investigation``, maintenance priority,
deadline before the next period is due). A verified finding (the investigation completed and attributed the move) is
pushed once; an investigation that cannot attribute it closes the warning silently as a likely false positive. At level 2
the warning waits in the activity center, marked unverified.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..analytics import changepoint, timeseries
from ..security import Principal

KINDS = ("early_warning",)
MIN_POINTS = 12
SIGMA_MOVE = 1.5
HORIZON = 2        # periods ahead the projection looks for a breach of the alert line
STRONG = 4.0       # a change point this large stands on its own
# a change point and a forecast drift are two views of one level shift, so they count once
FAMILY = {"change_point": "level", "forecast_drift": "level", "sustained_trend": "trend", "projected_breach": "projection"}
LEVEL = 2  # detection reads metric data: preparation, not mere suggestion


def _aware(ts):
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=UTC)


def _label(ts: pd.Timestamp, grain: str) -> str:
    return ts.strftime("%b %Y") if grain == "month" else (f"Q{(ts.month - 1) // 3 + 1} {ts.year}" if grain == "quarter"
                                                         else ts.strftime("%d %b %Y"))


def _robust_sigma(x: np.ndarray) -> float:
    d = np.diff(x)
    mad = float(np.median(np.abs(d - np.median(d)))) if len(d) else 0.0
    s = mad / 0.6745 / np.sqrt(2) if mad > 0 else float(np.std(x, ddof=1) or 0.0)
    return s or 1e-12


def observe(svc, ws: str, now: datetime) -> list[dict[str, Any]]:
    db = svc.stores.workspace(ws)
    seen: set[tuple[str, str]] = set()
    out = []
    for m in db.query("SELECT * FROM monitors WHERE enabled ORDER BY created_at"):
        key = (m["metric"], m["grain"])
        if key in seen or m["grain"] not in ("month", "quarter", "week"):
            continue
        seen.add(key)
        out.append({"kind": "monitored_metric", "monitor": m})
    return out


def _owner(svc, user_id: str) -> Principal | None:
    r = svc.stores.control.query("SELECT id, email FROM users WHERE id = ?", [user_id])
    return Principal(r[0]["id"], r[0]["email"]) if r else None


def assess(s: pd.Series, grain: str, threshold_sigma: float, higher_is_better: bool) -> dict[str, Any] | None:
    """The early-warning signals on one series, or None when there is too little history. Pure: tested directly."""
    if len(s) < MIN_POINTS:
        return None
    x = s.to_numpy(dtype=float)
    # noise from the history before the recent window (the window is what is being judged)
    sigma = float(np.std(x[:-4], ddof=1)) or _robust_sigma(x)
    adverse = -1.0 if higher_is_better else 1.0          # the sign of a bad move
    signals: list[dict[str, Any]] = []
    # change point near the end
    for b in changepoint.change_points(x[-12:], min_segment=2):
        idx = len(x) - 12 + b.index
        # a new regime of at least three periods that began in the last five: one odd period is not a regime
        if len(x) - 5 <= idx <= len(x) - 3 and b.shift * adverse > 0 and abs(b.shift) >= 2 * sigma:
            signals.append({"signal": "change_point", "since": _label(s.index[idx], grain), "shift": b.shift,
                            "sigmas": round(abs(b.shift) / sigma, 2), "strong": abs(b.shift) >= STRONG * sigma})
    # sustained trend
    last = x[-5:]
    steps = np.diff(last)
    if (steps * adverse > 0).all() and abs(last[-1] - last[0]) >= SIGMA_MOVE * sigma:
        signals.append({"signal": "sustained_trend", "periods": 4, "move": float(last[-1] - last[0]),
                        "sigmas": round(abs(last[-1] - last[0]) / sigma, 2)})
    # forecast drift and projected breach
    fc_drift = fc_next = None
    try:
        fc_drift = timeseries.forecast(s.iloc[:-3], horizon=3, grain=grain, level=0.8, backtest=0)
        yhat, lo, hi = (fc_drift.table[c].to_numpy() for c in ("yhat", "lo", "hi"))
        actual = x[-3:]
        if ((actual - yhat) * adverse > 0).all() and ((actual < lo) | (actual > hi)).sum() >= 2:
            signals.append({"signal": "forecast_drift", "periods": 3, "method": fc_drift.method,
                            "gap": float(actual[-1] - yhat[-1])})
    except Exception:  # noqa: BLE001,S110 - a model failure is not a signal
        pass
    hist = x[-13:-1] if len(x) > 13 else x[:-1]
    med = float(np.median(hist))
    mad = float(np.median(np.abs(hist - med)))
    zsig = mad / 0.6745 if mad > 0 else float(np.std(hist, ddof=1)) or 1e-12
    line = med + adverse * threshold_sigma * zsig
    latest_z = (x[-1] - med) / zsig
    try:
        fc_next = timeseries.forecast(s, horizon=HORIZON, grain=grain, level=0.8)
        nxt = fc_next.table["yhat"].to_numpy()
        crossing = [i for i, v in enumerate(nxt) if (v - line) * adverse > 0]
        if crossing:
            signals.append({"signal": "projected_breach", "periods_ahead": crossing[0] + 1, "line": line,
                            "projected": float(nxt[crossing[0]]), "lo": float(fc_next.table["lo"].iloc[crossing[0]]),
                            "hi": float(fc_next.table["hi"].iloc[crossing[0]]), "method": fc_next.method,
                            "backtest_mape": fc_next.backtest_mape})
    except Exception:  # noqa: BLE001,S110
        pass
    # seasonal check: the same periods a year earlier moved the same way by a similar amount
    season = timeseries.seasonal_period(grain)
    seasonal = False
    if len(x) >= season + 5:
        prior = x[-season - 5:-season]
        now_move, then_move = x[-1] - x[-5], prior[-1] - prior[0]
        seasonal = bool(np.sign(now_move) == np.sign(then_move) and abs(then_move) >= 0.7 * abs(now_move))
    families = {FAMILY[x["signal"]] for x in signals}
    raise_ = len(families) >= 2 or any(x.get("strong") for x in signals)
    return {"signals": signals, "families": sorted(families), "raise": raise_, "sigma": sigma, "median": med, "line": line, "latest": float(x[-1]), "latest_z": float(latest_z),
            "seasonal": seasonal, "period": s.index[-1], "label": _label(s.index[-1], grain),
            "forecast": ({"method": fc_next.method, "backtest_mape": fc_next.backtest_mape,
                          "next": [{"period": str(r.period.date()), "yhat": float(r.yhat), "lo": float(r.lo), "hi": float(r.hi)}
                                   for r in fc_next.table.itertuples()]} if fc_next is not None else None)}


def candidates(svc, ws: str, now: datetime, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from ..monitoring import _fmt  # noqa: PLC0415

    db = svc.stores.workspace(ws)
    out = []
    for sig in signals:
        m = sig["monitor"]
        owner = _owner(svc, m["created_by"])
        if owner is None:
            continue
        try:
            spec = svc.semantic.get(owner, ws, m["metric"])
            s = svc.services.monitors._history(owner, ws, spec, m["grain"])  # noqa: SLF001 - the monitor's own read path
        except Exception:  # noqa: BLE001,S112 - lost access or an unreadable dataset: nothing to warn about
            continue
        a = assess(s, m["grain"], float(m["threshold_sigma"]), spec.higher_is_better)
        if a is None or not a["signals"]:
            continue
        period_key = a["period"].date().isoformat()
        suppressed = []
        offset = {"month": pd.offsets.MonthBegin(3), "quarter": pd.offsets.QuarterBegin(3), "week": pd.offsets.Week(3)}[m["grain"]]
        if pd.Timestamp(now.replace(tzinfo=None)) > a["period"] + offset:
            suppressed.append(f"the data ends {a['label']}, too long ago to warn about a trend")
        if a["seasonal"]:
            suppressed.append("the same periods last year moved the same way (seasonal)")
        if not a["raise"]:
            suppressed.append("the signals agree on only one kind of evidence")
        if db.scalar("SELECT count(*) FROM findings WHERE metric = ? AND period = ? AND kind IN ('metric_shift', 'forecast_deviation')",
                     [spec.name, period_key]):
            suppressed.append("the monitor already alerted on this period")
        names = [x["signal"] for x in a["signals"]]
        reasons = [f"{spec.title} looks to be deteriorating: " + ", ".join(_describe(x, spec, _fmt) for x in a["signals"]) + "."]
        reasons.append(f"It is {_fmt(a['latest'], spec)} in {a['label']}, {abs(a['latest_z']):.1f} sigma from its trailing median "
                       f"of {_fmt(a['median'], spec)}; the monitor's alert line is {_fmt(a['line'], spec)}.")
        evidence = len(a["families"]) / 3
        out.append({
            "kind": "early_warning", "objective": f"Check the developing change in {spec.title.lower()}",
            "subject_kind": "metric", "subject_id": spec.name, "audience": [m["created_by"]],
            "signals": [{"kind": x["signal"], **{k: v for k, v in x.items() if k != "signal"}} for x in a["signals"]],
            "reasons": reasons, "level_required": LEVEL,
            "scores": {"value": 0.8, "likelihood": round(min(0.3 + 0.2 * len(set(names)), 0.9), 2), "evidence": evidence,
                       "urgency": 0.7 if "projected_breach" in names else 0.5, "cost": 0.1, "risk": 0.0, "interruption": 0.5},
            "actions": [{"id": "investigate", "label": "Investigate now", "effect": "runs a read-only investigation as you"}],
            "depends": {"dataset": {spec.dataset: int(db.scalar("SELECT current_version FROM datasets WHERE name = ?", [spec.dataset]) or 0)},
                        "metric": int(db.scalar("SELECT current_version FROM metrics WHERE name = ?", [spec.name]) or 0),
                        "period": period_key},
            "needed_at": None, "valid_until": now + timedelta(days=35 if m["grain"] != "week" else 8),
            "dedupe_key": f"early|{spec.name}|{m['grain']}|{period_key}",
            "_ctx": {"owner": owner, "spec": spec, "assessment": a, "monitor": m, "suppressed": suppressed},
        })
    return out


def _describe(x: dict[str, Any], spec, fmt) -> str:
    if x["signal"] == "change_point":
        return f"its level shifted from {x['since']} ({x['sigmas']:.1f} sigma)"
    if x["signal"] == "sustained_trend":
        return f"it moved the wrong way four periods running ({x['sigmas']:.1f} sigma in total)"
    if x["signal"] == "forecast_drift":
        return f"the last three periods all landed on the wrong side of the forecast ({x['method']})"
    return (f"the forecast crosses the alert line in {x['periods_ahead']} period(s) (projected {fmt(x['projected'], spec)}, "
            f"80% interval {fmt(x['lo'], spec)} to {fmt(x['hi'], spec)})")


def _question(spec, a: dict[str, Any]) -> str:
    """The analyst's own question form, for the latest complete period, so its period resolution and driver attribution
    apply unchanged."""
    verb = "fall" if spec.higher_is_better else "rise"
    return f"Why did {spec.title.lower()} {verb} in {a['label']}? Test alternative explanations."


def prepare(svc, ws: str, cand: dict[str, Any], now: datetime, *, automate: bool) -> dict[str, Any]:
    ctx = cand["_ctx"]
    a, spec, owner = ctx["assessment"], ctx["spec"], ctx["owner"]
    checks = [{"check": "freshness", "status": "block" if any("ends" in r for r in ctx["suppressed"]) else "ok",
               "detail": f"the series runs to {a['label']}"},
              {"check": "signals", "status": "ok", "detail": f"{len(a['families'])} of 3 kinds of evidence agree ({', '.join(a['families'])})"}]
    prepared = {"metric": spec.name, "assessment": {k: v for k, v in a.items() if k not in ("period",)}}
    if ctx["suppressed"]:
        checks.append({"check": "suppression", "status": "block", "detail": "; ".join(ctx["suppressed"])})
        return {"verdict": "drop", "prepared": prepared, "verification": {"checks": checks, "complete": True},
                "resources": {"compute_ms": 0, "queries": 2, "model_calls": 0, "refreshes": 0},
                "reasons": cand["reasons"] + ["Not raised: " + "; ".join(ctx["suppressed"]) + "."]}
    if not svc.level(ws) >= 3:
        checks.append({"check": "investigation", "status": "warn", "detail": "not investigated: the workspace allows preparation only"})
        return {"verdict": "unverified", "prepared": prepared, "verification": {"checks": checks, "complete": True},
                "scores": {"interruption": 0.0}, "resources": {"compute_ms": 0, "queries": 2, "model_calls": 0, "refreshes": 0},
                "reasons": cand["reasons"] + ["Lyst has not investigated it yet (the workspace allows preparation, not investigation)."]}
    reason = svc.admit(ws, "investigate", now)
    if reason:
        checks.append({"check": "budget", "status": "warn", "detail": reason})
        return {"verdict": "unverified", "prepared": prepared, "verification": {"checks": checks, "complete": True},
                "scores": {"interruption": 0.0}, "resources": {"compute_ms": 0, "queries": 2, "model_calls": 0, "refreshes": 0},
                "reasons": cand["reasons"] + [f"Not investigated: {reason}."]}
    grain = ctx["monitor"]["grain"]
    deadline = now + {"month": timedelta(days=28), "quarter": timedelta(days=85), "week": timedelta(days=6)}[grain]
    tid = svc.submit_background(owner, ws, _question(spec, a), deadline_at=deadline)
    prepared["question"] = _question(spec, a)
    return {"verdict": "pending", "task_id": tid, "prepared": prepared, "verification": {"checks": checks, "complete": False},
            "resources": {"compute_ms": 0, "queries": 2, "model_calls": 0, "refreshes": 0, "tasks": 1}, "reasons": cand["reasons"]}


def resume(svc, ws: str, a: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Called while the investigation runs. None until it finishes; then the verified finding, or a drop."""
    t = svc.stores.control.query("SELECT status, result, error, created_at, updated_at FROM tasks WHERE id = ?", [a["task_id"]])
    if not t or t[0]["status"] in ("queued", "running", "paused", "awaiting_approval"):
        return None
    checks = list((a["verification"] or {}).get("checks", []))
    ms = int(((t[0]["updated_at"] or now) - t[0]["created_at"]).total_seconds() * 1000) if t[0]["created_at"] else 0
    res = {"compute_ms": ms, "queries": 2, "model_calls": 0, "refreshes": 0, "tasks": 1}
    if t[0]["status"] != "done":
        checks.append({"check": "investigation", "status": "block", "detail": f"the investigation {t[0]['status']}: {(t[0]['error'] or '')[:120]}"})
        return {"verdict": "drop", "prepared": a["prepared"], "verification": {"checks": checks, "complete": True}, "resources": res,
                "reasons": a["reasons"] + ["The investigation did not complete, so nothing was raised."]}
    r = json.loads(t[0]["result"]) if isinstance(t[0]["result"], str) else t[0]["result"]
    evidence = [e for e in r.get("evidence", []) if "driver" in e.lower() or "moved" in e.lower() or "went from" in e.lower()]
    level = (r.get("confidence") or {}).get("level")
    if not evidence or level not in ("verified", "verified with warnings"):
        checks.append({"check": "investigation", "status": "block",
                       "detail": f"the investigation could not attribute the move (confidence {level or 'unknown'})"})
        return {"verdict": "drop", "prepared": a["prepared"], "verification": {"checks": checks, "complete": True}, "resources": res,
                "reasons": a["reasons"] + ["The investigation found no clear driver, so this is treated as noise for now."]}
    checks.append({"check": "investigation", "status": "ok", "detail": f"completed with {level} confidence"})
    finding = {"answer": r.get("answer"), "drivers": evidence[:3], "implications": r.get("implications", [])[:2],
               "next_steps": r.get("actions", [])[:2], "task_id": a["task_id"]}
    fc = a["prepared"]["assessment"].get("forecast") or {}
    uncertainty = (f"The next-period forecast ({fc.get('method')}) has an 80% interval; its back-test error was "
                   f"{fc['backtest_mape']:.0%}." if fc.get("backtest_mape") is not None else "The forecast interval is the model's 80% interval.")
    return {"verdict": "verified", "prepared": {**a["prepared"], "finding": finding},
            "verification": {"checks": checks, "complete": True}, "resources": res,
            "scores": {"value": 0.9, "evidence": min(1.0, (a["scores"]["evidence"] + 1) / 2), "interruption": 0.5},
            "reasons": [a["reasons"][0], f"Lyst investigated before telling you: {r.get('answer')}"] + evidence[:2] + [uncertainty]}


def run(svc, actor: Principal, ws: str, a: dict[str, Any], action: str) -> dict[str, Any]:
    if action != "investigate":
        raise ValueError(f"{action} is not available here")
    if a.get("task_id"):
        return {"task_id": a["task_id"]}
    q = (a.get("prepared") or {}).get("question") or f"Investigate why {a['subject_id'].replace('_', ' ')} has been getting worse."
    return {"task_id": svc.runtime.submit(actor, ws, q, kind="investigation")}


SURFACE = ("verified",)


def summary(a: dict[str, Any]) -> str:
    f = (a.get("prepared") or {}).get("finding") or {}
    parts = [a["reasons"][0], f.get("answer") or ""] + f.get("drivers", [])[:1] + f.get("next_steps", [])[:1]
    return " ".join(p for p in parts if p)
