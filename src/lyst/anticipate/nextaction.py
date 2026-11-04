# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Next-best analytical action: specific follow-ups grounded in what a finished analysis actually found.

Each suggestion names the finding it comes from and is checked before it is shown. Nothing generic: a suggestion is made
only when the analysis left a concrete thread to pull, and never for something the person already asked.

* **Look inside the driver.** One member explains most of a change (``share >= 50%``) and the metric has another
  dimension the analysis did not use: "What drove operating margin for region EMEA in Q3 2026 by product line?".
  Verified by counting the rows that question would read; an empty slice is dropped, not shown.
* **Watch the metric.** A change large enough to have drivers, on a metric with no monitor. Accepting requests a
  monitor, which still needs an admin's approval.
* **Make it recurring.** The same question (period aside) asked three or more times in 60 days with no schedule.
  Accepting proposes a schedule at the observed rhythm, which still needs approval.
* **Extend into a forecast.** A quarter or year reviewed on a metric with at least a year of monthly history and no
  forecast asked for recently.

Suggestions are pulled where the person works (under the answer, in Work); they are never pushed to the inbox.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from ..security import Principal

KINDS = ("next_best_action",)
LOOKBACK = timedelta(days=7)
MAX_PER_ANALYSIS = 3
CADENCES = [(86400, "every day"), (604800, "every week"), (2592000, "every month")]


def _aware(ts):
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=UTC)


def _j(v):
    return json.loads(v) if isinstance(v, str) else v


def _words(ident: str) -> str:
    return ident.replace("_", " ")


def observe(svc, ws: str, now: datetime) -> list[dict[str, Any]]:
    rows = svc.stores.control.query(
        "SELECT id, created_by, kind, request, plan, result, created_at FROM tasks WHERE workspace_id = ? AND status = 'done' "
        "AND kind IN ('analysis', 'investigation', 'followup') AND created_at >= ? ORDER BY created_at DESC LIMIT 30",
        [ws, now - LOOKBACK])
    return [{"kind": "analysis_completed", "task": r} for r in rows if r["result"]]


def _spec(svc, ws: str, name: str):
    from ..semantic import MetricSpec  # noqa: PLC0415

    r = svc.stores.workspace(ws).query("SELECT v.spec, m.status FROM metrics m JOIN metric_versions v ON v.name = m.name "
                                       "AND v.version = m.current_version WHERE m.name = ?", [name])
    if not r or r[0]["status"] != "approved":
        return None
    return MetricSpec.model_validate(_j(r[0]["spec"]))


def _parse(task: dict[str, Any]) -> dict[str, Any] | None:
    plan, result = _j(task["plan"]) or {}, _j(task["result"]) or {}
    question = (_j(task["request"]) or {}).get("question", "")
    cur = next((s for s in plan.get("steps", []) if s.get("capability") == "metric_value" and s.get("key") == "current"), None)
    if not cur:
        return None
    out = {"question": question, "metric": cur["args"]["metric"], "period": cur["args"].get("period") or {}, "driver": None}
    for t in result.get("tables", []):
        m = re.match(r"Change in .+ by (\w+)$", t.get("title", ""))
        if not m or "contribution" not in t.get("columns", []):
            continue
        ci = t["columns"].index("contribution")
        rows = [r for r in t["rows"] if isinstance(r[ci], (int, float))]
        total = sum(r[ci] for r in rows)
        if not rows or not total:
            continue
        top = max(rows, key=lambda r: abs(r[ci]))
        share = top[ci] / total
        out["driver"] = {"dimension": m.group(1), "member": str(top[0]), "share": round(share, 3), "contribution": top[ci]}
        break
    return out


def _asked(svc, ws: str, user_id: str) -> list[dict[str, Any]]:
    return svc.stores.control.query("SELECT request, created_at FROM tasks WHERE workspace_id = ? AND created_by = ? AND created_at >= ?",
                                    [ws, user_id, datetime.now(UTC) - timedelta(days=60)])


def candidates(svc, ws: str, now: datetime, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from ..workflows import pattern  # noqa: PLC0415

    db = svc.stores.workspace(ws)
    out: list[dict[str, Any]] = []
    for sig in signals:
        t = sig["task"]
        parsed = _parse(t)
        if not parsed:
            continue
        spec = _spec(svc, ws, parsed["metric"])
        if spec is None:
            continue
        user = t["created_by"]
        asked = _asked(svc, ws, user)
        asked_patterns = {pattern((_j(a["request"]) or {}).get("question", "")) for a in asked}
        base = {"subject_kind": "task", "subject_id": t["id"], "audience": [user], "level_required": 1,
                "valid_until": _aware(t["created_at"]) + LOOKBACK, "needed_at": None,
                "depends": {"dataset": {spec.dataset: int(db.scalar("SELECT current_version FROM datasets WHERE name = ?", [spec.dataset]) or 0)},
                            "metric": {spec.name: int(db.scalar("SELECT current_version FROM metrics WHERE name = ?", [spec.name]) or 0)}}}
        sigs = [{"kind": "analysis_completed", "task_id": t["id"], "question": parsed["question"]}]
        mine: list[dict[str, Any]] = []
        period, label = parsed["period"], parsed["period"].get("label", "")
        d = parsed["driver"]
        q_lower = parsed["question"].lower()
        # 1. look inside the driver
        if d and d["share"] >= 0.5 and " for " not in q_lower:
            used = {d["dimension"]} | {x for x in spec.dimensions if _words(x) in q_lower}
            others = [x for x in spec.dimensions if x not in used]
            if others:
                q = f"What drove {spec.title.lower()} for {_words(d['dimension'])} {d['member']} in {label} by {_words(others[0])}?"
                if pattern(q) not in asked_patterns:
                    mine.append({**base, "kind": "next_best_action", "objective": f"Look inside {d['member']} by {_words(others[0])}",
                                 "signals": sigs + [{"kind": "dominant_driver", **d}],
                                 "reasons": [f"{d['member']} accounts for {d['share']:.0%} of the change in {spec.title.lower()} in {label} "
                                             f"(your question: \"{parsed['question']}\").",
                                             f"The analysis compared {_words(d['dimension'])}s; it has not yet looked inside {d['member']} "
                                             f"by {_words(others[0])}."],
                                 "scores": {"value": round(0.4 + 0.5 * min(d["share"], 1.0), 3), "likelihood": 0.5,
                                            "evidence": min(d["share"], 1.0), "urgency": 0.3, "cost": 0.05, "risk": 0.0, "interruption": 0.0},
                                 "actions": [{"id": "run_now", "label": "Run this analysis", "question": q,
                                              "effect": "asks this question now, as you"}],
                                 "dedupe_key": f"nba|{t['id']}|drill|{pattern(q)}",
                                 "_ctx": {"check": "slice", "actor": user, "spec": spec, "dimension": d["dimension"], "member": d["member"],
                                          "period": period}})
        # 2. watch the metric
        if d and not db.scalar("SELECT count(*) FROM monitors WHERE metric = ? AND (enabled OR approval_id IS NOT NULL)", [spec.name]):
            mine.append({**base, "kind": "next_best_action", "objective": f"Watch {spec.title.lower()} for the next move",
                         "signals": sigs + [{"kind": "material_change_unwatched", "metric": spec.name}],
                         "reasons": [f"{spec.title} moved enough in {label} for Lyst to attribute drivers, and nothing watches it yet.",
                                     "A monitor checks each new period and investigates a material move before telling you."],
                         "scores": {"value": 0.55, "likelihood": 0.5, "evidence": 0.7, "urgency": 0.3, "cost": 0.02, "risk": 0.0,
                                    "interruption": 0.0},
                         "actions": [{"id": "watch", "label": "Watch this metric", "metric": spec.name, "grain": "month",
                                      "effect": "requests a monthly monitor; an admin approves it before it runs"}],
                         "dedupe_key": f"nba|{t['id']}|watch|{spec.name}", "_ctx": {"check": "metric", "spec": spec}})
        # 3. make it recurring
        same = sorted(_aware(a["created_at"]) for a in asked if pattern((_j(a["request"]) or {}).get("question", "")) == pattern(parsed["question"]))
        scheduled = {pattern(_j(s["request"]).get("question", "")) for s in svc.stores.control.query(
            "SELECT request FROM schedules WHERE workspace_id = ?", [ws])}
        if len(same) >= 3 and pattern(parsed["question"]) not in scheduled:
            gap = statistics.median((b - a).total_seconds() for a, b in zip(same, same[1:], strict=False))
            every, words = min(CADENCES, key=lambda c: abs(c[0] - gap))
            mine.append({**base, "kind": "next_best_action", "objective": f"Run \"{parsed['question']}\" {words}",
                         "signals": sigs + [{"kind": "repeated_question", "times": len(same), "median_gap_s": gap}],
                         "reasons": [f"You asked this {len(same)} times in the last 60 days, about {words} apart.",
                                     "A schedule would prepare it for you; Lyst checks its inputs before each run."],
                         "scores": {"value": 0.6, "likelihood": min(0.4 + 0.1 * len(same), 0.9), "evidence": 0.8, "urgency": 0.3,
                                    "cost": 0.05, "risk": 0.0, "interruption": 0.0},
                         "actions": [{"id": "schedule", "label": f"Schedule it {words}", "question": parsed["question"], "every_seconds": every,
                                      "effect": "proposes a schedule; an admin approves it before it runs"}],
                         "dedupe_key": f"nba|repeat|{user}|{pattern(parsed['question'])}", "_ctx": {"check": "none"}})
        # 4. extend into a forecast
        if period.get("grain") in ("quarter", "year") and spec.time_column and not any(
                "forecast" in (_j(a["request"]) or {}).get("question", "").lower()
                and spec.title.lower() in (_j(a["request"]) or {}).get("question", "").lower()
                and _aware(a["created_at"]) >= now - timedelta(days=30) for a in asked):
            q = f"Forecast {spec.title.lower()} for the next 2 quarters"
            mine.append({**base, "kind": "next_best_action", "objective": f"See where {spec.title.lower()} is heading",
                         "signals": sigs + [{"kind": "period_review_without_forecast", "metric": spec.name, "period": label}],
                         "reasons": [f"You reviewed {spec.title.lower()} for {label}; no forecast of it was asked for in the last 30 days.",
                                     "A forecast with its interval shows whether the latest period is the start of a trend."],
                         "scores": {"value": 0.4, "likelihood": 0.5, "evidence": 0.6, "urgency": 0.2, "cost": 0.05, "risk": 0.0,
                                    "interruption": 0.0},
                         "actions": [{"id": "run_now", "label": "Forecast it", "question": q, "effect": "asks this question now, as you"}],
                         "dedupe_key": f"nba|{t['id']}|forecast|{spec.name}", "_ctx": {"check": "history", "actor": user, "spec": spec}})
        out += sorted(mine, key=lambda c: -c["scores"]["value"])[:MAX_PER_ANALYSIS]
    return out


def _q(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def prepare(svc, ws: str, cand: dict[str, Any], now: datetime, *, automate: bool) -> dict[str, Any]:
    """Verify the suggestion would help: the slice has rows, the metric is still approved, the history is long enough.
    Queries run as the person the suggestion is for, through the SQL guard."""
    import time  # noqa: PLC0415

    t0 = time.perf_counter()
    ctx = cand["_ctx"]
    checks: list[dict[str, Any]] = []
    queries = 0
    verdict = "ready"
    if ctx["check"] in ("slice", "history"):
        r = svc.stores.control.query("SELECT id, email FROM users WHERE id = ?", [ctx["actor"]])
        actor = Principal(r[0]["id"], r[0]["email"]) if r else None
        spec = ctx["spec"]
        try:
            if actor is None:
                raise PermissionError("the person is no longer a user")
            if ctx["check"] == "slice":
                p = ctx["period"]
                where = f'"{ctx["dimension"]}" = {_q(ctx["member"])}'
                if spec.time_column and p.get("start") and p.get("end"):
                    where += f' AND "{spec.time_column}" >= {_q(p["start"])} AND "{spec.time_column}" < {_q(p["end"])}'
                n = svc.services.data.query(actor, ws, f'SELECT count(*) FROM "{spec.dataset}" WHERE {where}').rows[0][0]
                queries += 1
                ok = n > 0
                checks.append({"check": "slice", "status": "ok" if ok else "block",
                               "detail": f"{n:,} rows for {_words(ctx['dimension'])} {ctx['member']} in the period"})
            else:
                n = svc.services.data.query(actor, ws, f'SELECT count(DISTINCT date_trunc(\'month\', "{spec.time_column}")) FROM "{spec.dataset}"').rows[0][0]
                queries += 1
                ok = n >= 12
                checks.append({"check": "history", "status": "ok" if ok else "block", "detail": f"{n} months of history"})
            verdict = "ready" if ok else "drop"
        except Exception as exc:  # noqa: BLE001 - denied or unreadable: the suggestion would not help this person
            checks.append({"check": "access", "status": "block", "detail": str(exc)[:160]})
            verdict = "drop"
    elif ctx["check"] == "metric":
        ok = _spec(svc, ws, ctx["spec"].name) is not None
        checks.append({"check": "metric", "status": "ok" if ok else "block", "detail": "the metric is approved" if ok else "no longer approved"})
        verdict = "ready" if ok else "drop"
    reasons = list(cand["reasons"]) + ([] if verdict == "ready" else ["Checked before showing it: " + checks[-1]["detail"] + "."])
    return {"verdict": verdict, "prepared": {"question": next((a.get("question") for a in cand["actions"] if a.get("question")), None)},
            "verification": {"checks": checks, "complete": True},
            "resources": {"compute_ms": int((time.perf_counter() - t0) * 1000), "queries": queries, "model_calls": 0, "refreshes": 0},
            "reasons": reasons}


def run(svc, actor: Principal, ws: str, a: dict[str, Any], action: str) -> dict[str, Any]:
    act = next(x for x in a["actions"] if x["id"] == action)
    if action == "run_now":
        return {"task_id": svc.runtime.submit(actor, ws, act["question"], kind="followup")}
    if action == "watch":
        m = svc.services.monitors.create(actor, ws, metric=act["metric"], grain=act.get("grain", "month"))
        return {"monitor_id": m["monitor_id"], "approval_id": m["approval_id"], "needs_approval": True}
    if action == "schedule":
        title = act["question"][:80]
        s = svc.runtime.create_schedule(actor, ws, title, act["question"], int(act["every_seconds"]))
        return {"schedule_id": s["schedule_id"], "approval_id": s["approval_id"], "needs_approval": True}
    raise ValueError(f"{action} is not available here")
