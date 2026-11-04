# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Operations: workload priority, workspace quotas, calendar schedules and the task supervisor.

* **Priority.** Every task carries a priority from its kind: work a person is waiting for (an analysis, a workflow
  run) first, then monitor investigations, then scheduled reports, then evaluation and maintenance. The claim takes
  the best priority first and, within it, the workspace with the fewest running tasks, so one busy workspace cannot
  starve another. Background work (priority 2 and up) never takes the last worker: one stays free for people.
* **Quotas.** Per workspace, set by installation administrators (a workspace admin cannot raise their own): running
  tasks at once, queued tasks (admission: a full queue refuses new work with 429 instead of growing without bound),
  monthly model spend and tokens (the model is refused once reached; deterministic analysis still runs), and a ceiling
  on any one task's time budget.
* **Calendar schedules.** "Every Monday at 08:00 Europe/London" is computed in the schedule's own time zone, so a run
  stays at 08:00 local across daylight-saving changes. A local time that does not exist (the spring-forward hour) runs
  at the same wall-clock offset after the jump; a repeated local time (fall back) runs once, at its first occurrence.
* **Supervisor.** Each worker tick looks for steps running far longer than expected, queued work past its deadline,
  approvals nobody has decided and schedules the worker has fallen behind on; it records what it saw and fails only
  what can no longer succeed (a queued task past its deadline). It never marks work done: completion is the runtime's
  verified checkpoint, not a report.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRIORITY = {"analysis": 0, "workflow": 0, "investigation": 1, "scheduled": 2, "evaluation": 3, "maintenance": 3}
BACKGROUND = 2  # priority at and above which a task is background work
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
QUOTA_FIELDS = ("max_running", "max_queued", "monthly_spend_usd", "monthly_tokens", "task_budget_seconds")
ACTIVE = ("queued", "running", "paused", "awaiting_approval")


class QuotaExceeded(Exception):
    """A workspace quota refused the request (mapped to 429)."""

    def __init__(self, message: str, retry_after: int = 30) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def priority_for(kind: str) -> int:
    return PRIORITY.get(kind, 1)


def _now() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------------------------------------- quotas


def default_max_queued() -> int:
    return int(os.environ.get("LYST_WORKSPACE_MAX_QUEUED", "500"))


def quotas(db, workspace_id: str) -> dict[str, Any]:
    rows = db.query("SELECT * FROM workspace_quotas WHERE workspace_id = ?", [workspace_id])
    q = {k: (rows[0][k] if rows else None) for k in QUOTA_FIELDS}
    if q["max_queued"] is None:
        q["max_queued"] = default_max_queued()
    return q


def set_quotas(db, workspace_id: str, actor_id: str, values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for k in QUOTA_FIELDS:
        if k not in values:
            continue
        v = values[k]
        if v is not None:
            v = float(v) if k == "monthly_spend_usd" else int(v)
            if v < 0 or (k in ("max_running", "max_queued", "task_budget_seconds") and v == 0):
                raise ValueError(f"{k} must be positive (or empty for no limit)")
        clean[k] = v
    if not db.scalar("SELECT count(*) FROM workspaces WHERE id = ?", [workspace_id]):
        raise KeyError(workspace_id)

    def tx(c):
        cur = c.execute("SELECT workspace_id FROM workspace_quotas WHERE workspace_id = ?", [workspace_id]).fetchone()
        if not cur:
            c.execute("INSERT INTO workspace_quotas (workspace_id) VALUES (?)", [workspace_id])
        for k, v in clean.items():
            c.execute(f"UPDATE workspace_quotas SET {k} = ? WHERE workspace_id = ?", [v, workspace_id])
        c.execute("UPDATE workspace_quotas SET updated_by = ?, updated_at = now() WHERE workspace_id = ?",
                  [actor_id, workspace_id])

    db.write(tx)
    return quotas(db, workspace_id)


def month_start(now: datetime | None = None) -> datetime:
    now = now or _now()
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def model_usage(db, workspace_id: str, since: datetime | None = None) -> dict[str, float]:
    row = db.query("SELECT count(*) AS calls, coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) AS tokens, "
                   "coalesce(sum(cost_usd), 0) AS cost FROM llm_calls WHERE workspace_id = ? AND occurred_at >= ?",
                   [workspace_id, since or month_start()])[0]
    return {"calls": int(row["calls"]), "tokens": int(row["tokens"]), "cost_usd": round(float(row["cost"]), 6)}


def model_budget_block(db, workspace_id: str) -> str | None:
    """Why this workspace may not call a model now (its monthly budget is used), or None."""
    q = quotas(db, workspace_id)
    if q["monthly_spend_usd"] is None and q["monthly_tokens"] is None:
        return None
    used = model_usage(db, workspace_id)
    if q["monthly_spend_usd"] is not None and used["cost_usd"] >= q["monthly_spend_usd"]:
        return (f"this workspace reached its monthly model budget (${used['cost_usd']:.2f} of "
                f"${q['monthly_spend_usd']:.2f}); analysis continues without model wording")
    if q["monthly_tokens"] is not None and used["tokens"] >= q["monthly_tokens"]:
        return (f"this workspace reached its monthly model token budget ({used['tokens']:,} of "
                f"{q['monthly_tokens']:,}); analysis continues without model wording")
    return None


# ----------------------------------------------------------------------------- calendar schedules


def parse_weekdays(value: str | list[str] | None) -> list[int] | None:
    if not value:
        return None
    items = value.split(",") if isinstance(value, str) else list(value)
    out = []
    for item in items:
        key = str(item).strip().lower()[:3]
        if key not in WEEKDAYS:
            raise ValueError(f"unknown weekday {item!r}; use mon, tue, wed, thu, fri, sat, sun")
        out.append(WEEKDAYS.index(key))
    return sorted(set(out))


def parse_at(value: str) -> time:
    try:
        h, m = (int(x) for x in value.strip().split(":"))
        return time(h, m)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("time of day must be HH:MM, for example 08:00") from exc


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {name!r}; use an IANA name such as Europe/London") from exc


def next_calendar_run(after: datetime, at: str, weekdays: list[int] | None, tz: str | None) -> datetime:
    """The first occurrence strictly after ``after`` of local time ``at`` on ``weekdays`` (every day when empty) in
    zone ``tz``, as UTC. ``fold=0`` gives the documented DST behaviour: a nonexistent local time maps to the same
    offset as before the jump (02:30 in a spring-forward gap runs at 03:30 local), an ambiguous one to its first
    occurrence, so a fall-back day runs once."""
    z, t = zone(tz), parse_at(at)
    local = after.astimezone(z)
    for d in range(0, 9):
        day = local.date() + timedelta(days=d)
        if weekdays and day.weekday() not in weekdays:
            continue
        cand = datetime.combine(day, t).replace(tzinfo=z, fold=0).astimezone(UTC)
        if cand > after:
            return cand
    raise ValueError("no next run could be computed")  # unreachable with a valid weekday list


def schedule_next(row: dict[str, Any], after: datetime) -> datetime:
    if row.get("at_time"):
        return next_calendar_run(after, row["at_time"], parse_weekdays(row.get("weekdays")), row.get("timezone"))
    return after + timedelta(seconds=int(row["every_seconds"]))


def occurrences_between(row: dict[str, Any], first_due: datetime, now: datetime, cap: int = 10_000) -> tuple[int, datetime]:
    """(due occurrences from ``first_due`` up to ``now``, the next occurrence after ``now``)."""
    n, nxt = 0, first_due
    while nxt <= now and n < cap:
        n += 1
        nxt = schedule_next(row, nxt)
    if nxt <= now:  # capped: jump straight past now
        nxt = schedule_next(row, now)
    return n, nxt


def describe_schedule(row: dict[str, Any]) -> str:
    from .monitoring import cadence  # noqa: PLC0415

    if row.get("at_time"):
        days = parse_weekdays(row.get("weekdays"))
        when = "every day" if not days or len(days) == 7 else "every " + ", ".join(
            ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[d] for d in days)
        return f"{when} at {row['at_time']} ({row.get('timezone') or 'UTC'})"
    return cadence(int(row["every_seconds"]))


# ----------------------------------------------------------------------------- supervisor


def stall_seconds() -> int:
    return int(os.environ.get("LYST_STALL_SECONDS", "300"))


def supervise(runtime, now: datetime | None = None) -> dict[str, Any]:
    """One supervision pass. Records an event the first time it sees each condition; acts only on queued work whose
    deadline has passed (it can no longer succeed). Returns what it found."""
    db = runtime.db
    now = now or _now()
    out: dict[str, Any] = {"stalled": [], "expired": [], "stale_approvals": [], "overdue_schedules": []}

    stall = stall_seconds()
    rows = db.query(
        "SELECT s.task_id, s.seq, s.capability, s.started_at, t.workspace_id, t.budget_seconds FROM task_steps s "
        "JOIN tasks t ON t.id = s.task_id WHERE s.status = 'running' AND t.status = 'running' AND s.started_at < ?",
        [now - timedelta(seconds=stall)])
    for r in rows:
        seen = db.scalar("SELECT count(*) FROM task_events WHERE task_id = ? AND kind = 'stalled' "
                         "AND CAST(data->>'$.seq' AS INTEGER) = ?", [r["task_id"], r["seq"]])
        running_for = int((now - r["started_at"]).total_seconds())
        item = {"task_id": r["task_id"], "workspace_id": r["workspace_id"], "step": r["capability"],
                "running_seconds": running_for, "budget_seconds": r["budget_seconds"]}
        out["stalled"].append(item)
        if not seen:
            runtime.event(r["task_id"], "stalled",
                          f"{r['capability']} has run for {running_for}s with no result; the time budget "
                          f"({r['budget_seconds']}s) will stop it if it does not finish", {"seq": r["seq"]})

    def expire(c):
        ids = [x[0] for x in c.execute("SELECT id FROM tasks WHERE status = 'queued' AND deadline_at IS NOT NULL "
                                       "AND deadline_at < ?", [now]).fetchall()]
        for tid in ids:
            c.execute("UPDATE tasks SET status = 'failed', error = 'missed its deadline before a worker could start it', "
                      "updated_at = now() WHERE id = ? AND status = 'queued'", [tid])
        return ids

    for tid in db.write(expire):
        out["expired"].append(tid)
        runtime.event(tid, "failed", "Missed its deadline before a worker could start it")

    for a in db.query("SELECT id, workspace_id, action, summary, created_at FROM approvals WHERE status = 'pending' "
                      "AND created_at < ? ORDER BY created_at", [now - timedelta(hours=24)]):
        out["stale_approvals"].append({**a, "waiting_hours": round((now - a["created_at"]).total_seconds() / 3600, 1)})

    for s in db.query("SELECT id, workspace_id, title, next_run_at FROM schedules WHERE enabled AND next_run_at < ?",
                      [now - timedelta(minutes=10)]):
        out["overdue_schedules"].append({**s, "late_minutes": int((now - s["next_run_at"]).total_seconds() // 60)})
    return out


# ----------------------------------------------------------------------------- report


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(p * (len(v) - 1))))], 2)


def report(runtime, workspace_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Operational picture for administrators: queues by priority, running work per workspace, what the supervisor
    sees, failures by class, run times by kind, and model use against budgets. ``workspace_id`` scopes it to one."""
    db = runtime.db
    now = now or _now()
    where, params = ("WHERE workspace_id = ?", [workspace_id]) if workspace_id else ("", [])
    and_ws = " AND workspace_id = ?" if workspace_id else ""
    queue = db.query(f"SELECT priority, status, count(*) AS n FROM tasks {where} "
                     f"{'AND' if where else 'WHERE'} status IN ('queued', 'running', 'awaiting_approval', 'paused') "
                     "GROUP BY priority, status ORDER BY priority, status", params)
    per_ws = db.query(f"SELECT workspace_id, count(*) FILTER (WHERE status = 'running') AS running, "
                      f"count(*) FILTER (WHERE status = 'queued') AS queued FROM tasks {where} "
                      f"{'AND' if where else 'WHERE'} status IN ('queued', 'running') GROUP BY workspace_id ORDER BY workspace_id",
                      params)
    since = now - timedelta(hours=24)
    failures: dict[str, int] = {}
    for r in db.query(f"SELECT error FROM tasks WHERE status = 'failed' AND updated_at >= ?{and_ws}", [since, *params]):
        err = r["error"] or ""
        cls = next((c for c in ("permission", "input", "data", "model", "transient") if f"({c})" in err), None)
        if cls is None:
            cls = "budget" if "budget" in err else "deadline" if "deadline" in err else "abandoned" if "abandoned" in err else "other"
        failures[cls] = failures.get(cls, 0) + 1
    durations: dict[str, list[float]] = {}
    for r in db.query(f"SELECT kind, active_seconds, epoch(updated_at) - epoch(created_at) AS wall FROM tasks "
                      f"WHERE status = 'done' AND updated_at >= ?{and_ws}", [now - timedelta(days=7), *params]):
        durations.setdefault(r["kind"], []).append(float(r["active_seconds"] or r["wall"] or 0))
    sup = supervise(runtime, now)
    if workspace_id:
        sup = {k: [x for x in v if (x.get("workspace_id") if isinstance(x, dict) else
                                     db.scalar("SELECT workspace_id FROM tasks WHERE id = ?", [x])) == workspace_id]
               for k, v in sup.items()}
    workspaces = [workspace_id] if workspace_id else [r["id"] for r in db.query("SELECT id FROM workspaces ORDER BY id")]
    usage = []
    for ws in workspaces:
        q = quotas(db, ws)
        used = model_usage(db, ws)
        usage.append({"workspace_id": ws, "quotas": q, "model_month": used,
                      "spend_share": round(used["cost_usd"] / q["monthly_spend_usd"], 4) if q["monthly_spend_usd"] else None,
                      "token_share": round(used["tokens"] / q["monthly_tokens"], 4) if q["monthly_tokens"] else None,
                      "blocked": model_budget_block(db, ws)})
    return {
        "generated_at": now.isoformat(),
        "workers": getattr(runtime, "workers", None),
        "queue": [{"priority": r["priority"], "status": r["status"], "count": r["n"]} for r in queue],
        "workspaces": per_ws,
        "failures_24h": failures,
        "run_seconds_7d": {k: {"count": len(v), "p50": _pct(v, 0.5), "p95": _pct(v, 0.95)} for k, v in sorted(durations.items())},
        "supervisor": sup,
        "usage": usage,
    }


__all__ = ["BACKGROUND", "PRIORITY", "QuotaExceeded", "describe_schedule", "model_budget_block", "model_usage",
           "next_calendar_run", "occurrences_between", "parse_weekdays", "priority_for", "quotas", "report",
           "schedule_next", "set_quotas", "supervise"]
