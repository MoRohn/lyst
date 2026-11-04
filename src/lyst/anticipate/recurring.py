# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Anticipating approved recurring work (a monthly finance review, a weekly data-heavy workflow).

Signal: an enabled, approved schedule whose next occurrence falls inside its *lead window*. The window is just in time:
long enough to fix what the checks find (three times the slowest recent run, at least an hour, at most 24 hours),
so Lyst does not prepare days ahead of data that will still change.

Preparation (level 2, read-only, as the schedule's owner): which datasets and definitions the run depends on, whether the
owner may still run it, whether data arrived since the last run, whether a connection behind the data is failing,
whether a data contract is broken, whether a definition changed, whether a compiled workflow went stale (the run would
fail), how long it usually takes and whether recent runs failed. The verdict is ``ready``, ``ready_with_warnings`` or
``blocked``.

At level 4 the preparation may also refresh a connection-backed input that has not synced since the last run (an
already-configured, approved sync; a new dataset version, never an edit of an old one).

The offered actions are deliberately narrow: run the same approved request now, as the person who accepts. Nothing here
distributes a report, edits records or changes a definition.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from ..security import Principal

KINDS = ("recurring_prep",)
# a blocker is worth an interruption; ready work is quiet
_AFTER = {"blocked": {"value": 0.9, "evidence": 0.9, "interruption": 1.0},
          "ready_with_warnings": {"value": 0.7, "evidence": 0.85},
          "ready": {"value": 0.5, "evidence": 0.9}}
MIN_LEAD = timedelta(hours=1)
MAX_LEAD = timedelta(hours=24)


def _aware(ts) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _ago(ts: datetime, now: datetime) -> str:
    s = (now - ts).total_seconds()
    if s < 3600:
        return f"{max(int(s // 60), 1)} min ago"
    if s < 86400 * 2:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def _until(ts: datetime, now: datetime) -> str:
    s = (ts - now).total_seconds()
    if s <= 0:
        return "now"
    if s < 3600:
        return f"in {max(int(s // 60), 1)} min"
    return f"in {s / 3600:.0f} h" if s < 86400 * 2 else f"in {s / 86400:.0f} days"


def history(control, schedule_id: str, limit: int = 10) -> list[dict[str, Any]]:
    return control.query("SELECT id, status, error, created_at, updated_at FROM tasks WHERE schedule_id = ? "
                         "ORDER BY created_at DESC LIMIT ?", [schedule_id, limit])


def lead_window(runs: list[dict[str, Any]]) -> timedelta:
    durations = [(r["updated_at"] - r["created_at"]).total_seconds() for r in runs if r["status"] == "done" and r["updated_at"]]
    slowest = max(durations, default=0.0)
    return min(max(timedelta(seconds=3 * slowest), MIN_LEAD), MAX_LEAD)


def observe(svc, ws: str, now: datetime) -> list[dict[str, Any]]:
    """One signal per enabled schedule whose next occurrence is inside its lead window."""
    out = []
    for s in svc.stores.control.query("SELECT * FROM schedules WHERE workspace_id = ? AND enabled", [ws]):
        nxt = _aware(s["next_run_at"])
        if nxt is None:
            continue
        runs = history(svc.stores.control, s["id"])
        lead = lead_window(runs)
        if nxt - now > lead:
            continue
        out.append({"kind": "recurring_due", "schedule": s, "next_run_at": nxt, "lead_s": lead.total_seconds(), "runs": runs})
    return out


# -- dependencies ------------------------------------------------------------------------------------------------------

def _owner(svc, user_id: str) -> Principal | None:
    r = svc.stores.control.query("SELECT id, email FROM users WHERE id = ?", [user_id])
    return Principal(r[0]["id"], r[0]["email"]) if r else None


def _matches(question: str, spec) -> bool:
    from ..catalog import tokens  # noqa: PLC0415

    q = set(tokens(question))
    words = set(tokens(spec.name))
    if words and words <= q:
        return True
    title = set(tokens(spec.title))
    if title and title <= q:
        return True
    return any(set(tokens(s)) and set(tokens(s)) <= q for s in spec.synonyms)


def dependencies(svc, ws: str, owner: Principal, schedule: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The datasets and metrics the run relies on: the compiled workflow's recorded dependencies when it has one, else the
    metrics its question names plus the datasets the last successful run actually queried."""
    req = json.loads(schedule["request"]) if isinstance(schedule["request"], str) else schedule["request"]
    db = svc.stores.workspace(ws)
    datasets: set[str] = set()
    metrics: set[str] = set()
    workflow = None
    if req.get("workflow"):
        wf = db.query("SELECT id, version, deps, status, invalid_reason FROM workflows WHERE id = ? ORDER BY version DESC LIMIT 1",
                      [req["workflow"]["id"]])
        if wf:
            workflow = wf[0]
            deps = json.loads(workflow["deps"]) if isinstance(workflow["deps"], str) else workflow["deps"]
            datasets |= set(deps.get("datasets", {}))
            metrics |= set(deps.get("metrics", {}))
    from ..semantic import MetricSpec  # noqa: PLC0415

    # every defined metric, approved or not: a question that names a metric which has since been retired is a blocker to
    # find now, not a dependency to lose sight of
    specs = []
    for r in db.query("SELECT v.spec FROM metrics m JOIN metric_versions v ON v.name = m.name AND v.version = m.current_version"):
        try:
            specs.append(MetricSpec.model_validate_json(r["spec"]) if isinstance(r["spec"], str) else MetricSpec.model_validate(r["spec"]))
        except Exception:  # noqa: BLE001,S112 - an unreadable old spec is not this run's dependency
            continue
    for spec in specs:
        if _matches(req.get("question", ""), spec):
            metrics.add(spec.name)
    by_name = {s.name: s for s in specs}
    datasets |= {by_name[m].dataset for m in metrics if m in by_name}
    known = [r["name"] for r in db.query("SELECT name FROM datasets")]
    last_ok = next((r for r in runs if r["status"] == "done"), None)
    if last_ok:
        for r in db.query("SELECT sql FROM query_history WHERE task_id = ? AND outcome = 'ok'", [last_ok["id"]]):
            sql = r["sql"]
            for name in known:
                if re.search(rf'(?<![\w"]){re.escape(name)}(?![\w"])|"{re.escape(name)}"', sql):
                    datasets.add(name)
    return {"question": req.get("question", ""), "datasets": sorted(datasets), "metrics": sorted(metrics), "workflow": workflow,
            "specs": by_name}


def depends(svc, ws: str, owner: Principal | None, deps: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any]:
    """What this preparation's validity rests on; any change re-opens it."""
    db = svc.stores.workspace(ws)
    return {
        "datasets": {d: int(db.scalar("SELECT current_version FROM datasets WHERE name = ?", [d]) or 0) for d in deps["datasets"]},
        "metrics": {m: int(db.scalar("SELECT current_version FROM metrics WHERE name = ? AND status = 'approved'", [m]) or 0)
                    for m in deps["metrics"]},
        "workflow": (f"{deps['workflow']['id']}:{deps['workflow']['version']}:{deps['workflow']['status']}" if deps["workflow"] else None),
        "role": svc.guard.role(owner, ws) if owner else None,
        "occurrence": str(_aware(schedule["next_run_at"])),
    }


# -- candidate ------------------------------------------------------------------------------------------------------------

def candidates(svc, ws: str, now: datetime, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for sig in signals:
        s, nxt = sig["schedule"], sig["next_run_at"]
        owner = _owner(svc, s["created_by"])
        deps = dependencies(svc, ws, owner, s, sig["runs"]) if owner else {"question": "", "datasets": [], "metrics": [],
                                                                            "workflow": None, "specs": {}}
        hours = max((nxt - now).total_seconds() / 3600, 0.0)
        reasons = [f"'{s['title']}' is approved to run {svc.cadence(s['every_seconds'])}; the next run is "
                   f"{nxt.strftime('%d %b %H:%M UTC')} ({_until(nxt, now)})."]
        if deps["datasets"]:
            reasons.append("It reads " + ", ".join(deps["datasets"]) + ".")
        scores = {"value": 0.6, "likelihood": 1.0, "evidence": 0.5, "urgency": round(1 / (1 + hours / 6), 3), "cost": 0.02,
                  "risk": 0.0, "interruption": 0.2}
        out.append({
            "kind": "recurring_prep", "objective": f"Get '{s['title']}' ready before it runs",
            "subject_kind": "schedule", "subject_id": s["id"], "audience": [s["created_by"]],
            "signals": [{"kind": "recurring_due", "schedule_id": s["id"], "next_run_at": nxt.isoformat(),
                         "lead_window_s": sig["lead_s"], "recent_runs": len(sig["runs"])}],
            "reasons": reasons, "scores": scores, "level_required": 1,
            "actions": [{"id": "run_now", "label": "Run it now", "effect": "starts the same approved request now, as you"}],
            "depends": depends(svc, ws, owner, deps, s), "needed_at": nxt, "valid_until": nxt + timedelta(hours=1),
            "dedupe_key": f"recurring_prep|{s['id']}|{nxt.isoformat()}",
            "_ctx": {"schedule": s, "owner": owner, "deps": deps, "runs": sig["runs"]},
        })
    return out


# -- preparation ---------------------------------------------------------------------------------------------------------

def prepare(svc, ws: str, cand: dict[str, Any], now: datetime, *, automate: bool) -> dict[str, Any]:
    """Run the read-only readiness checks. Returns verdict, prepared, verification, resources and refreshed reasons."""
    t0 = time.perf_counter()
    ctx = cand["_ctx"]
    owner, deps, runs = ctx["owner"], ctx["deps"], ctx["runs"]
    db = svc.stores.workspace(ws)
    checks: list[dict[str, Any]] = []
    reasons = list(cand["reasons"])
    refreshed: list[dict[str, Any]] = []

    def check(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    if owner is None or not svc.guard.can(owner, ws, "task.create") or not svc.guard.can(owner, ws, "data.query"):
        check("access", "block", "the schedule's owner can no longer run analyses in this workspace")
    else:
        check("access", "ok", "the schedule's owner may still run it")
    last_run = _aware(runs[0]["created_at"]) if runs else None
    if not deps["datasets"]:
        check("inputs", "warn", "Lyst could not tell which datasets this run reads; it will plan them when it runs")
    for name in deps["datasets"]:
        cur = db.query("SELECT d.current_version AS v, dv.created_at AS at_, dv.row_count AS n FROM datasets d JOIN dataset_versions dv "
                       "ON dv.dataset = d.name AND dv.version = d.current_version WHERE d.name = ?", [name])
        if not cur:
            check(f"dataset:{name}", "block", f"{name} is no longer available")
            continue
        updated = _aware(cur[0]["at_"])
        sync = svc.stores.control.query(
            "SELECT s.connection_id, s.source_table, s.cursor_column, c.name, c.status, c.consecutive_failures, c.failure_category "
            "FROM connection_syncs s JOIN connections c ON c.id = s.connection_id WHERE s.workspace_id = ? AND s.dataset = ?", [ws, name])
        if last_run and updated <= last_run:
            detail = f"no new data in {name} since the last run {_ago(last_run, now)} (v{cur[0]['v']}, updated {_ago(updated, now)})"
            status = "warn"
            if sync and automate and not sync[0]["consecutive_failures"] and owner is not None:
                try:
                    r = svc.connections.sync(owner, ws, sync[0]["connection_id"], source_table=sync[0]["source_table"], dataset=name,
                                             cursor_column=sync[0]["cursor_column"])
                    refreshed.append({"dataset": name, "version": r.version, "rows_added": r.rows_added})
                    detail, status = f"refreshed {name} from {sync[0]['name']} before the run ({r.rows_added} new rows, v{r.version})", "ok"
                except Exception as exc:  # noqa: BLE001 - a failed refresh is a finding, not a crash
                    detail += f"; refreshing from {sync[0]['name']} failed: {str(exc)[:120]}"
            check(f"freshness:{name}", status, detail)
        else:
            check(f"freshness:{name}", "ok", f"{name} was updated {_ago(updated, now)} (v{cur[0]['v']}, {cur[0]['n']:,} rows)"
                  + (", after the last run" if last_run else ""))
        if sync and sync[0]["consecutive_failures"]:
            check(f"source:{name}", "warn", f"the connection {sync[0]['name']} behind {name} failed its last "
                                            f"{sync[0]['consecutive_failures']} attempt(s) ({sync[0]['failure_category'] or 'error'})")
        cc = db.query("SELECT outcome, dataset_version FROM contract_checks WHERE dataset = ? ORDER BY checked_at DESC LIMIT 1", [name])
        if cc and cc[0]["outcome"] in ("breaking", "fail"):
            check(f"contract:{name}", "warn", f"the latest version of {name} broke its data contract ({cc[0]['outcome']})")
    for m in deps["metrics"]:
        mv = db.query("SELECT m.current_version AS v, m.status, mv.created_at AS at_, mv.note FROM metrics m JOIN metric_versions mv "
                      "ON mv.name = m.name AND mv.version = m.current_version WHERE m.name = ?", [m])
        if not mv or mv[0]["status"] != "approved":
            check(f"definition:{m}", "block", f"the metric {m} is no longer approved")
        elif last_run and _aware(mv[0]["at_"]) > last_run:
            check(f"definition:{m}", "warn", f"the definition of {m} changed to v{mv[0]['v']} since the last run"
                                             + (f" ({mv[0]['note']})" if mv[0]["note"] else "") + "; results may not compare directly")
        else:
            check(f"definition:{m}", "ok", f"{m} v{mv[0]['v']} is approved")
    if deps["workflow"] is not None and owner is not None:
        wf = svc.services.workflows.get(owner, ws, deps["workflow"]["id"])
        reason = (wf.get("invalid_reason") or wf["status"]) if wf["status"] != "active" else svc.services.workflows.staleness(owner, ws, wf)
        if reason:
            check("workflow", "block", f"the compiled workflow cannot run as recorded: {reason}")
        else:
            check("workflow", "ok", f"the compiled workflow v{wf['version']} still matches its inputs")
    failed = [r for r in runs[:3] if r["status"] == "failed"]
    if failed:
        check("history", "warn", f"{len(failed)} of the last {min(len(runs), 3)} runs failed; most recent: {(failed[0]['error'] or '')[:120]}")
    durations = sorted((r["updated_at"] - r["created_at"]).total_seconds() for r in runs if r["status"] == "done" and r["updated_at"])
    estimate = {"runs_seen": len(runs), "p50_s": durations[len(durations) // 2] if durations else None,
                "max_s": durations[-1] if durations else None}
    block = svc.model_budget_block(ws)
    if block:
        check("budget", "block", block)
    verdict = "blocked" if any(c["status"] == "block" for c in checks) else (
        "ready_with_warnings" if any(c["status"] == "warn" for c in checks) else "ready")
    problems = [c["detail"] for c in checks if c["status"] != "ok"]
    if problems:
        reasons += [p[0].upper() + p[1:] + "." for p in problems[:3]]
    elif last_run:
        reasons.append(f"Its inputs have new data since the last run {_ago(last_run, now)}.")
    ms = int((time.perf_counter() - t0) * 1000)
    return {"verdict": verdict, "scores": _AFTER[verdict],
            "prepared": {"datasets": deps["datasets"], "metrics": deps["metrics"], "question": deps["question"], "estimate": estimate,
                         "refreshed": refreshed},
            "verification": {"checks": checks, "complete": True},
            "resources": {"compute_ms": ms, "queries": 0, "model_calls": 0, "refreshes": len(refreshed)},
            "reasons": reasons}


def run(svc, actor: Principal, ws: str, a: dict[str, Any], action: str) -> dict[str, Any]:
    if action != "run_now":
        raise ValueError(f"{action} is not available here")
    sched = svc.stores.control.query("SELECT request, workspace_id FROM schedules WHERE id = ?", [a["subject_id"]])
    if not sched or sched[0]["workspace_id"] != ws:
        raise KeyError(a["subject_id"])
    req = json.loads(sched[0]["request"])
    return {"task_id": svc.runtime.submit(actor, ws, req["question"], kind="analysis", workflow=req.get("workflow"))}
