# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Durable task runtime: leases, checkpoints, resume, approvals, budgets and schedules.

A task is a list of steps. Each step's output is written to ``task_steps`` in the same
transaction that marks it done, so a crash loses at most the step in flight. On restart, or
when a worker's lease expires, another worker claims the task, reloads every finished step's
output and continues with the first unfinished one. Steps that touch the outside world must
be idempotent (they key their writes on the task id); the runtime never re-runs a finished
step.

This ports the intent of Acumane's Inngest jobs and AcuRout pools (interactive work first,
background work deferred) into a process the API owns, so Streamlit sessions can close
without losing work.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import observability as obs
from ..analyst.engine import Services
from ..analyst.registry import Context
from ..security import AccessDenied, Principal
from ..store import Stores

log = logging.getLogger("lyst.runtime")

FIXED_STEPS = [
    {"capability": "understand", "key": "understand"},
    {"capability": "contextualize", "key": "contextualize"},
    {"capability": "plan", "key": "plan"},
]
TERMINAL = {"done", "failed", "cancelled"}
FAILURE_CLASSES = ("permission", "input", "data", "model", "transient")


def short_title(question: str, limit: int = 120) -> str:
    """A list title for a request: whole words, with an ellipsis when cut, never "…factors, te"."""
    text = " ".join(question.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.-")
    return cut + "…"


def classify_failure(exc: BaseException) -> str:
    """What kind of failure, which decides retrying and what the person is told."""
    from ..data.sqlguard import QueryRejected  # noqa: PLC0415
    from ..llm import LLMUnavailable  # noqa: PLC0415

    if isinstance(exc, AccessDenied):
        return "permission"
    if isinstance(exc, QueryRejected | ValueError | KeyError | TypeError):
        return "input"
    if isinstance(exc, LLMUnavailable):
        return "model"
    name = type(exc).__module__ + "." + type(exc).__name__
    if name.startswith("_duckdb") or name.startswith("duckdb"):
        return "data"
    return "transient"
AUTONOMY = ("assist", "delegate", "autonomous", "governed")
MAX_STEP_ATTEMPTS = 2
# A task whose worker died this many times (lease expired while running) is failed instead of re-claimed:
# something in it kills workers, and re-running it forever would starve every other task.
MAX_TAKEOVERS = int(os.environ.get("LYST_TASK_MAX_TAKEOVERS", "5"))


def _now() -> datetime:
    return datetime.now(UTC)


class TaskRuntime:
    def __init__(self, stores: Stores, services: Services, *, worker_id: str | None = None,
                 lease_seconds: int = 30, poll_seconds: float = 0.2) -> None:
        self.stores = stores
        self.services = services
        self.db = stores.control
        self.worker_id = worker_id or f"w_{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.parallelism = max(1, int(os.environ.get("LYST_TASK_PARALLELISM", "4")))
        self.monitors = None  # set by the app; ticked by the worker loop
        self._last_prune = time.monotonic()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- submission and control -------------------------------------------------------------

    def submit(self, actor: Principal, workspace_id: str, question: str, *, autonomy: str | None = None,
               kind: str = "analysis", schedule_id: str | None = None, max_steps: int = 40,
               budget_seconds: int = 600, workflow: dict[str, Any] | None = None,
               idempotency_key: str | None = None) -> str:
        """Queue a task. With an ``idempotency_key``, a repeat of the same submission by the same person in the same
        workspace (a double click, a client retry after a timeout) returns the first task instead of running the
        work twice. The check and the insert are one write transaction, so two concurrent repeats cannot both win."""
        self.services.guard.require(actor, workspace_id, "task.create")
        autonomy = autonomy or self.db.scalar("SELECT autonomy FROM workspaces WHERE id = ?", [workspace_id]) or "delegate"
        if autonomy not in AUTONOMY:
            raise ValueError("unknown autonomy mode")
        tid = "t_" + uuid.uuid4().hex[:12]
        key = (idempotency_key or "").strip()[:200] or None

        def tx(c) -> str | None:
            if key:
                prior = c.execute("SELECT id FROM tasks WHERE workspace_id = ? AND created_by = ? AND idempotency_key = ?",
                                  [workspace_id, actor.user_id, key]).fetchone()
                if prior:
                    return prior[0]
            c.execute(
                "INSERT INTO tasks (id, workspace_id, created_by, kind, title, request, status, autonomy, max_steps, "
                "budget_seconds, schedule_id, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                [tid, workspace_id, actor.user_id, kind, short_title(question), json.dumps({"question": question} | ({"workflow": workflow} if workflow else {})),
                 autonomy, max_steps, budget_seconds, schedule_id, key],
            )
            return None

        if prior := self.db.write(tx):
            return prior
        self.event(tid, "queued", "Task received")
        self.services.guard.audit(actor, workspace_id, "task.create", tid, "ok")
        return tid

    def _owned(self, actor: Principal, task_id: str, action: str = "task.control") -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise KeyError(task_id)
        t = rows[0]
        if not (t["created_by"] == actor.user_id and self.services.guard.can(actor, t["workspace_id"], "task.create")):
            self.services.guard.require(actor, t["workspace_id"], action, task_id)
        return t

    def pause(self, actor: Principal, task_id: str) -> None:
        self._owned(actor, task_id)
        self._set_status(task_id, "paused", only_from=("queued", "running"))
        self.event(task_id, "paused", "Paused by user")

    def resume(self, actor: Principal, task_id: str) -> None:
        self._owned(actor, task_id)
        self._set_status(task_id, "queued", only_from=("paused",))
        self.event(task_id, "resumed", "Resumed by user")

    def cancel(self, actor: Principal, task_id: str) -> None:
        self._owned(actor, task_id)
        self._set_status(task_id, "cancelled", only_from=("queued", "running", "paused", "awaiting_approval"))
        self.event(task_id, "cancelled", "Cancelled by user")

    def retry(self, actor: Principal, task_id: str) -> None:
        self._owned(actor, task_id)
        self._set_status(task_id, "queued", only_from=("failed",))
        self.db.execute("DELETE FROM task_steps WHERE task_id = ? AND status = 'failed'", [task_id])
        self.event(task_id, "retry", "Retry requested")

    def _set_status(self, task_id: str, status: str, only_from: tuple[str, ...]) -> None:
        def tx(c):
            cur = c.execute("SELECT status FROM tasks WHERE id = ?", [task_id]).fetchone()
            if not cur or cur[0] not in only_from:
                raise ValueError(f"task is {cur[0] if cur else 'missing'}; cannot move to {status}")
            c.execute("UPDATE tasks SET status = ?, updated_at = now(), lease_owner = NULL, lease_until = NULL "
                      "WHERE id = ?", [status, task_id])

        self.db.write(tx)

    # -- reads ------------------------------------------------------------------------------

    def get(self, actor: Principal, task_id: str) -> dict[str, Any]:
        t = self.db.query("SELECT * FROM tasks WHERE id = ?", [task_id])
        if not t:
            raise KeyError(task_id)
        task = t[0]
        self.services.guard.require(actor, task["workspace_id"], "workspace.read", task_id)
        for k in ("request", "plan", "result"):
            task[k] = json.loads(task[k]) if task[k] else None
        # "done" means the run finished; the outcome says whether it produced a verified answer, needs the
        # person's input, or finished without passing verification. The UI must never show the last two as Done.
        task["outcome"] = (task["result"] or {}).get("status") if task["status"] == "done" else None
        task["steps"] = [
            {**s, "args": json.loads(s["args"]) if s["args"] else None}
            for s in self.db.query("SELECT task_id, seq, phase, capability, agent, args, status, error, started_at, "
                                   "finished_at FROM task_steps WHERE task_id = ? ORDER BY seq", [task_id])
        ]
        task["events"] = self.db.query(
            "SELECT occurred_at, kind, message FROM task_events WHERE task_id = ? ORDER BY id", [task_id])
        return task

    def list(self, actor: Principal, workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self.services.guard.require(actor, workspace_id, "workspace.read")
        return self.db.query(
            "SELECT id, title, status, CASE WHEN status = 'done' THEN result->>'$.status' END AS outcome, autonomy, kind, "
            "created_by, created_at, updated_at, schedule_id "
            "FROM tasks WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?", [workspace_id, limit])

    def trace(self, actor: Principal, task_id: str) -> dict[str, Any]:
        """Where one task's time, model calls and queries went, step by step, from the records the engine
        already keeps (task_steps, llm_calls, query_history). Read-only; the same access as reading the task.

        Each query and model call records the step that made it. Records from before that (or made on a thread
        the step started itself) are attributed by time, and only when exactly one step was running then."""
        t = self.db.query("SELECT id, workspace_id, status, created_at FROM tasks WHERE id = ?", [task_id])
        if not t:
            raise KeyError(task_id)
        task = t[0]
        self.services.guard.require(actor, task["workspace_id"], "workspace.read", task_id)
        steps = self.db.query(
            "SELECT seq, phase, capability, agent, status, error, started_at, finished_at, "
            "date_diff('millisecond', started_at, finished_at) AS ms FROM task_steps WHERE task_id = ? ORDER BY seq",
            [task_id])
        calls = self.db.query(
            "SELECT occurred_at, purpose, workload, model, provider, tier, attempt, outcome, status, latency_ms, queue_ms, "
            "input_tokens, output_tokens, cost_usd, step AS recorded_step FROM llm_calls WHERE task_id = ? ORDER BY id", [task_id])
        try:
            queries = self.stores.workspace(task["workspace_id"]).query(
                "SELECT occurred_at, sql, rows, ms, outcome, error, step AS recorded_step FROM query_history "
                "WHERE task_id = ? ORDER BY id",
                [task_id])
        except Exception:  # noqa: BLE001 - a workspace file that cannot open must not hide the rest of the trace
            queries = []

        def step_at(ts) -> int | None:
            if ts is None:
                return None
            hits = [s["seq"] for s in steps if s["started_at"] and s["started_at"] <= ts
                    and (s["finished_at"] is None or ts <= s["finished_at"])]
            return hits[0] if len(hits) == 1 else None

        for s in steps:
            s["model_calls"], s["queries"] = 0, 0
        by_seq = {s["seq"]: s for s in steps}
        for rows, field in ((calls, "model_calls"), (queries, "queries")):
            for r in rows:
                rec = r.pop("recorded_step", None)
                r["step"] = rec if rec in by_seq else step_at(r["occurred_at"])
                if r["step"] is not None:
                    by_seq[r["step"]][field] += 1
        first = min((s["started_at"] for s in steps if s["started_at"]), default=None)
        last = max((s["finished_at"] for s in steps if s["finished_at"]), default=None)
        summary = {
            "wall_ms": int((last - first).total_seconds() * 1000) if first and last else None,
            "queued_ms": int((first - task["created_at"]).total_seconds() * 1000) if first and task["created_at"] else None,
            "steps": len(steps),
            "model_calls": len(calls),
            "model_failures": sum(1 for c in calls if c["outcome"] != "ok"),
            "model_ms": sum(c["latency_ms"] or 0 for c in calls),
            "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
            "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
            "cost_usd": round(sum(c["cost_usd"] or 0 for c in calls), 6),
            "queries": len(queries),
            "query_ms": sum(q["ms"] or 0 for q in queries),
            "rejected_queries": sum(1 for q in queries if q["outcome"] != "ok"),
            "slowest_step": max(steps, key=lambda s: s["ms"] or 0)["capability"] if steps else None,
        }
        return {"task_id": task_id, "status": task["status"], "summary": summary, "steps": steps,
                "model_calls": calls, "queries": queries}

    def event(self, task_id: str, kind: str, message: str, data: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO task_events (id, task_id, kind, message, data) VALUES (nextval('task_event_seq'), ?, ?, ?, ?)",
            [task_id, kind, message, json.dumps(data or {}, default=str)])

    # -- approvals --------------------------------------------------------------------------

    def request_approval(self, workspace_id: str, requested_by: str, action: str, summary: str,
                         payload: dict[str, Any], task_id: str | None = None) -> str:
        aid = "ap_" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO approvals (id, task_id, workspace_id, requested_by, action, summary, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [aid, task_id, workspace_id, requested_by, action, summary, json.dumps(payload, default=str)])
        return aid

    def approvals(self, actor: Principal, workspace_id: str, status: str = "pending") -> list[dict[str, Any]]:
        self.services.guard.require(actor, workspace_id, "workspace.read")
        return self.db.query("SELECT * FROM approvals WHERE workspace_id = ? AND status = ? ORDER BY created_at",
                             [workspace_id, status])

    def decide(self, actor: Principal, approval_id: str, approve: bool) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM approvals WHERE id = ?", [approval_id])
        if not rows:
            raise KeyError(approval_id)
        ap = rows[0]
        self.services.guard.require(actor, ap["workspace_id"],
                                    "skill.approve" if ap["action"] == "skill.register" else "approval.decide",
                                    approval_id)
        if ap["requested_by"] == actor.user_id and ap["action"] in ("skill.register",):
            raise AccessDenied("the proposer of a capability cannot approve it")
        if ap["status"] != "pending":
            raise ValueError("already decided")
        status = "approved" if approve else "rejected"

        def tx(c):
            c.execute("UPDATE approvals SET status = ?, decided_by = ?, decided_at = now() WHERE id = ?",
                      [status, actor.user_id, approval_id])
            if ap["action"] == "schedule.activate" and approve:
                sid = json.loads(ap["payload"])["schedule_id"]
                c.execute("UPDATE schedules SET enabled = true WHERE id = ?", [sid])
            if ap["task_id"]:
                c.execute("UPDATE tasks SET status = ?, updated_at = now() WHERE id = ? AND status = 'awaiting_approval'",
                          ["queued" if approve else "cancelled", ap["task_id"]])

        self.db.write(tx)
        if ap["action"] == "monitor.activate" and approve and self.monitors:
            self.monitors.on_approved(actor, ap["workspace_id"], json.loads(ap["payload"])["monitor_id"])
        if ap["action"] == "skill.register" and approve and self.services.skills:
            p = json.loads(ap["payload"])
            self.services.skills.on_approved(actor, ap["workspace_id"], p["name"], int(p["version"]))
        self.services.guard.audit(actor, ap["workspace_id"], "approval.decide", approval_id, status)
        if ap["task_id"]:
            self.event(ap["task_id"], f"approval_{status}", f"{ap['summary']}: {status}")
        return {"id": approval_id, "status": status}

    # -- schedules --------------------------------------------------------------------------

    def create_schedule(self, actor: Principal, workspace_id: str, title: str, question: str,
                        every_seconds: int, *, workflow: dict[str, Any] | None = None) -> dict[str, Any]:
        self.services.guard.require(actor, workspace_id, "schedule.create")
        if every_seconds < 60:
            raise ValueError("the shortest interval is 60 seconds")
        sid = "s_" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO schedules (id, workspace_id, created_by, title, request, every_seconds, next_run_at, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, false)",
            [sid, workspace_id, actor.user_id, title,
             json.dumps({"question": question} | ({"workflow": workflow} if workflow else {})), every_seconds,
             _now() + timedelta(seconds=every_seconds)])
        aid = self.request_approval(workspace_id, actor.user_id, "schedule.activate",
                                    f"Run '{title}' every {every_seconds // 60} min", {"schedule_id": sid})
        self.db.execute("UPDATE schedules SET approval_id = ? WHERE id = ?", [aid, sid])
        self.services.guard.audit(actor, workspace_id, "schedule.create", sid, "pending_approval")
        return {"schedule_id": sid, "approval_id": aid, "enabled": False}

    def schedules(self, actor: Principal, workspace_id: str) -> list[dict[str, Any]]:
        self.services.guard.require(actor, workspace_id, "workspace.read")
        return self.db.query("SELECT * FROM schedules WHERE workspace_id = ? ORDER BY title", [workspace_id])

    def tick_schedules(self, now: datetime | None = None) -> list[str]:
        now = now or _now()

        def tx(c):
            due = c.execute("SELECT id, workspace_id, created_by, request, every_seconds FROM schedules "
                            "WHERE enabled AND next_run_at <= ?", [now]).fetchall()
            for sid, _ws, _by, _req, every in due:
                c.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", [now + timedelta(seconds=every), sid])
            return due

        started = []
        for sid, ws, by, req, _ in self.db.write(tx):
            owner = self.db.query("SELECT id, email FROM users WHERE id = ?", [by])
            if not owner:
                continue
            p = Principal(owner[0]["id"], owner[0]["email"])
            try:
                r = json.loads(req)
                started.append(self.submit(p, ws, r["question"], autonomy="autonomous",
                                           kind="scheduled", schedule_id=sid, workflow=r.get("workflow")))
            except AccessDenied:
                log.warning("schedule %s owner lost access; skipped", sid)
        return started

    def tick_monitors(self) -> int:
        if not self.monitors:
            return 0
        n = 0
        for w in self.db.query("SELECT id FROM workspaces"):
            try:
                n += len(self.monitors.tick(w["id"]))
            except Exception:  # noqa: BLE001
                log.exception("monitor tick failed for %s", w["id"])
        return n

    # -- execution --------------------------------------------------------------------------

    def claim(self) -> str | None:
        now = _now()
        until = now + timedelta(seconds=self.lease_seconds)

        def tx(c):
            # a task whose worker died too often is failed, not re-claimed (see MAX_TAKEOVERS)
            dead = c.execute("SELECT id, takeovers FROM tasks WHERE status = 'running' AND lease_until < ? "
                             "AND takeovers >= ?", [now, MAX_TAKEOVERS]).fetchall()
            for did, n in dead:
                c.execute("UPDATE tasks SET status = 'failed', error = ?, updated_at = now(), lease_owner = NULL, "
                          "lease_until = NULL WHERE id = ?",
                          [f"abandoned: its worker stopped {n} times while running it", did])
            row = c.execute(
                "SELECT id, status FROM tasks WHERE status = 'queued' OR (status = 'running' AND lease_until < ?) "
                "ORDER BY CASE WHEN kind = 'scheduled' THEN 1 ELSE 0 END, created_at LIMIT 1", [now]).fetchone()
            if not row:
                return None, dead
            # attempts counts every claim; takeovers only those of a dead worker's expired lease
            c.execute("UPDATE tasks SET status = 'running', lease_owner = ?, lease_until = ?, attempts = attempts + 1, "
                      "takeovers = takeovers + CASE WHEN status = 'running' THEN 1 ELSE 0 END, "
                      "updated_at = now() WHERE id = ?", [self.worker_id, until, row[0]])
            return row[0], dead

        tid, dead = self.db.write(tx)
        for did, n in dead:
            log.error("task %s abandoned after %s worker takeovers", did, n)
            self.event(did, "failed", f"Abandoned: its worker stopped {n} times while running it")
        if tid:
            self.event(tid, "claimed", f"Worker {self.worker_id} started")
        return tid

    def _finish(self, task_id: str, sets: str, params: list[Any]) -> str | None:
        """A terminal (or gate) write fenced on this worker's lease: a cancel, pause or takeover that landed while
        a step ran wins, and the stale worker's write is dropped. Returns None when written, else the status found."""

        def tx(c):
            row = c.execute("SELECT status, lease_owner FROM tasks WHERE id = ?", [task_id]).fetchone()
            if not row or row[0] != "running" or row[1] != self.worker_id:
                return row[0] if row else "missing"
            c.execute(f"UPDATE tasks SET {sets}, updated_at = now() WHERE id = ?", [*params, task_id])
            return None

        return self.db.write(tx)

    def _heartbeat(self, task_id: str, stop: threading.Event) -> None:
        """Keep the lease alive while a step runs: a step longer than the lease must not let a second worker
        claim the task and run the same step again."""
        interval = max(0.2, self.lease_seconds / 3)
        while not stop.wait(interval):
            try:
                if self._renew(task_id) != "running":
                    return
            except Exception:  # noqa: BLE001 - the wave loop's own renew reports the state
                log.warning("lease renewal for %s failed", task_id, exc_info=True)

    def _renew(self, task_id: str) -> str:
        def tx(c):
            row = c.execute("SELECT status, lease_owner FROM tasks WHERE id = ?", [task_id]).fetchone()
            if row[0] != "running" or row[1] != self.worker_id:
                return row[0] if row[1] in (None, self.worker_id) else "lost"
            c.execute("UPDATE tasks SET lease_until = ? WHERE id = ?",
                      [_now() + timedelta(seconds=self.lease_seconds), task_id])
            return "running"

        return self.db.write(tx)

    def run_task(self, task_id: str) -> str:
        ctx_token = obs.task_id.set(task_id)
        try:
            with obs.tracer().start_as_current_span("task") as span:
                span.set_attribute("lyst.task_id", task_id)
                stop = threading.Event()
                beat = threading.Thread(target=self._heartbeat, args=(task_id, stop), daemon=True,
                                        name=f"lease-{task_id[-6:]}")
                beat.start()
                try:
                    state = self._run_task(task_id)
                finally:
                    stop.set()
                    beat.join(timeout=5)
                span.set_attribute("lyst.status", state)
        finally:
            obs.task_id.reset(ctx_token)
        if state in ("done", "failed", "cancelled"):
            obs.TASKS.labels(state).inc()
        return state

    def _run_task(self, task_id: str) -> str:
        t = self.db.query("SELECT * FROM tasks WHERE id = ?", [task_id])[0]
        owner = self.db.query("SELECT id, email FROM users WHERE id = ?", [t["created_by"]])[0]
        actor = Principal(owner["id"], owner["email"])
        obs.user_id.set(actor.user_id)  # the task runs as its owner: personal credentials and row policies follow
        request = json.loads(t["request"])
        ctx = Context(t["workspace_id"], actor, task_id, self.services, request=request)
        done = self.db.query("SELECT seq, capability, args, output, status FROM task_steps WHERE task_id = ? "
                             "ORDER BY seq", [task_id])
        finished = {r["seq"]: r for r in done if r["status"] == "done"}
        skipped = {r["seq"] for r in done if r["status"] == "skipped"}
        steps: list[dict[str, Any]] = list(FIXED_STEPS)
        if t["plan"]:
            steps += json.loads(t["plan"])["steps"]
        for seq, row in finished.items():
            if seq < len(steps):
                ctx.outputs[steps[seq]["key"]] = json.loads(row["output"])
        started = time.monotonic()
        completed: set[int] = set(finished) | skipped
        key_done: set[str] = {steps[i]["key"] for i in completed if i < len(steps)}
        from opentelemetry import context as otel_context  # noqa: PLC0415

        otel_ctx = otel_context.get_current()
        pool = ThreadPoolExecutor(max_workers=self.parallelism, thread_name_prefix=f"task-{task_id[-6:]}")
        abandon = False  # a step overran the time budget: do not wait for its thread on the way out
        try:
            while len(completed) < len(steps):
                state = self._renew(task_id)
                if state != "running":
                    self.event(task_id, "stopped", f"Stopped with {len(completed)} of {len(steps)} steps done ({state})")
                    return state
                if len(completed) >= t["max_steps"]:
                    return self._fail(task_id, f"step budget of {t['max_steps']} exhausted")
                if time.monotonic() - started > t["budget_seconds"]:
                    return self._fail(task_id, f"time budget of {t['budget_seconds']}s exhausted")
                ready = [i for i, st in enumerate(steps) if i not in completed and self._deps_met(i, steps, key_done)]
                if not ready:
                    return self._fail(task_id, "no runnable step: a dependency failed or the plan is cyclic")
                # approval gates are checked before anything in this wave starts
                for i in ready:
                    gate = self._approval_gate(t, actor, task_id, i, steps[i])
                    if gate:
                        return gate
                futures = {pool.submit(self._run_step, ctx, t, task_id, i, steps[i], request, otel_ctx): i for i in ready}
                # The budget holds while steps run, not only between waves: a hung step (a stalled connector, model or
                # query) otherwise kept its task running and its worker busy for as long as it hung, with the
                # heartbeat renewing the lease all the while.
                remaining = max(0.0, t["budget_seconds"] - (time.monotonic() - started))
                try:
                    finished_now = list(as_completed(futures, timeout=remaining))
                except FuturesTimeout:
                    late = sorted(steps[i]["capability"] for f, i in futures.items() if not f.done())
                    abandon = True
                    self.db.execute(
                        f"UPDATE task_steps SET status = 'failed', error = ?, finished_at = now() WHERE task_id = ? "
                        f"AND status = 'running' AND seq IN ({', '.join('?' * len(futures))})",
                        [f"timeout: still running when the {t['budget_seconds']}s time budget ran out", task_id, *futures.values()])
                    return self._fail(task_id, f"time budget of {t['budget_seconds']}s exhausted while "
                                               f"{', '.join(late)} {'was' if len(late) == 1 else 'were'} still running")
                for fut in finished_now:
                    i = futures[fut]
                    outcome = fut.result()
                    if outcome["status"] == "failed":
                        return self._fail(task_id, f"{steps[i]['capability']} failed ({outcome['failure_class']}): "
                                                   f"{outcome['error']}")
                    completed.add(i)
                    key_done.add(steps[i]["key"])
                    if outcome["status"] == "done" and steps[i]["key"] == "plan":
                        steps += outcome["output"]["steps"]
        finally:
            # an overrun step's thread cannot be killed; leave it to finish on its own. Its checkpoint cannot revive the
            # task: the task is failed, and _finish only moves a task this worker still holds as running.
            pool.shutdown(wait=not abandon, cancel_futures=True)
        result = ctx.outputs.get("deliver")
        found = self._finish(task_id, "status = 'done', result = ?, lease_owner = NULL, lease_until = NULL",
                             [json.dumps(result, default=str)])
        if found is not None:
            self.event(task_id, "stopped", f"Finished all steps but the task was already {found}; result discarded")
            return found
        self.event(task_id, "done", (result or {}).get("answer", "Done")[:200])
        return "done"

    @staticmethod
    def _deps_met(i: int, steps: list[dict[str, Any]], key_done: set[str]) -> bool:
        after = steps[i].get("after")
        if after is None:  # no declared dependencies: run after the previous step, as before
            return i == 0 or steps[i - 1]["key"] in key_done
        return all(k in key_done for k in after)

    def _approval_gate(self, t, actor, task_id, seq, step) -> str | None:
        cap = self.services.registry.get(step["capability"])
        if not (cap.requires_approval and t["autonomy"] != "autonomous"):
            return None
        approved = self.db.scalar("SELECT count(*) FROM approvals WHERE task_id = ? AND action = ? AND status = 'approved'",
                                  [task_id, f"step:{seq}"])
        if approved:
            return None
        pending = self.db.scalar("SELECT count(*) FROM approvals WHERE task_id = ? AND action = ? AND status = 'pending'",
                                 [task_id, f"step:{seq}"])
        if not pending:
            self.request_approval(t["workspace_id"], actor.user_id, f"step:{seq}", f"Run {cap.name}: {cap.description}",
                                  step.get("args", {}), task_id)
        found = self._finish(task_id, "status = 'awaiting_approval', lease_owner = NULL, lease_until = NULL", [])
        if found is not None:
            return found
        self.event(task_id, "awaiting_approval", f"{cap.name} needs approval")
        return "awaiting_approval"

    def _run_step(self, ctx: Context, t, task_id: str, seq: int, step: dict[str, Any], request, otel_ctx=None) -> dict[str, Any]:
        """One step, on a pool thread: runs, retries when transient, commits its checkpoint."""
        from opentelemetry import context as otel_context  # noqa: PLC0415

        ctx_token = obs.task_id.set(task_id)
        user_token = obs.user_id.set(ctx.actor.user_id)
        step_token = obs.step_seq.set(seq)  # queries and model calls made by this step record it
        otel_token = otel_context.attach(otel_ctx) if otel_ctx is not None else None
        try:
            cap = self.services.registry.get(step["capability"])
            key = step["key"]
            args = dict(step.get("args") or {})
            if key == "understand":
                args.setdefault("question", request["question"])
            self.event(task_id, "step_started", f"{cap.agent}: {cap.name}", {"seq": seq})
            self.db.execute(
                "INSERT OR REPLACE INTO task_steps (task_id, seq, phase, capability, agent, args, status, started_at, "
                "idempotency_key) VALUES (?, ?, ?, ?, ?, ?, 'running', now(), ?)",
                [task_id, seq, cap.phase, cap.name, cap.agent, json.dumps(args, default=str), f"{task_id}:{seq}"])
            output, error, fclass = None, None, None
            step_start = time.perf_counter()
            span_cm = obs.tracer().start_as_current_span(f"step:{cap.name}")
            span = span_cm.__enter__()
            try:
                span.set_attribute("lyst.task_id", task_id)
                span.set_attribute("lyst.agent", cap.agent)
                for attempt in range(1, MAX_STEP_ATTEMPTS + 1):
                    try:
                        output = self.services.registry.run(cap.name, ctx, args)
                        break
                    except Exception as exc:  # noqa: BLE001 - classified below
                        fclass = classify_failure(exc)
                        error = f"{type(exc).__name__}: {exc}"
                        if fclass != "transient" or attempt == MAX_STEP_ATTEMPTS:
                            if fclass not in ("permission", "input"):
                                log.exception("step %s of %s failed (%s)", seq, task_id, fclass)
                            break
                        log.warning("step %s of %s failed transiently, retrying", seq, task_id)
                obs.STEP_SECONDS.labels(cap.name).observe(time.perf_counter() - step_start)
                span.set_attribute("lyst.outcome", "done" if output is not None else (fclass or "failed"))
            finally:
                # always closed, even if the step raised past the retry loop, so no span is left for the GC
                span_cm.__exit__(None, None, None)
            if output is None:
                if cap.optional:
                    self.db.execute("UPDATE task_steps SET status = 'skipped', error = ?, finished_at = now() "
                                    "WHERE task_id = ? AND seq = ?", [f"{fclass}: {error}", task_id, seq])
                    self.event(task_id, "step_skipped", f"{cap.name} skipped ({fclass})", {"seq": seq, "error": error})
                    return {"status": "skipped", "failure_class": fclass, "error": error}
                self.db.execute("UPDATE task_steps SET status = 'failed', error = ?, finished_at = now() "
                                "WHERE task_id = ? AND seq = ?", [f"{fclass}: {error}", task_id, seq])
                return {"status": "failed", "failure_class": fclass, "error": error}
            ctx.outputs[key] = output
            payload = json.dumps(output, default=str)

            def commit(c):
                owner = c.execute("SELECT lease_owner, status FROM tasks WHERE id = ?", [task_id]).fetchone()
                if not owner or owner[0] not in (self.worker_id, None):
                    return False  # another worker took the task over; its run owns the checkpoints now
                if owner[1] != "running":
                    return False  # the task ended while this step ran (budget overrun, cancel): keep its record as is
                c.execute("UPDATE task_steps SET status = 'done', output = ?, finished_at = now() "
                          "WHERE task_id = ? AND seq = ?", [payload, task_id, seq])
                if key == "plan":
                    c.execute("UPDATE tasks SET plan = ? WHERE id = ?", [payload, task_id])
                return True

            if not self.db.write(commit):
                return {"status": "failed", "failure_class": "transient", "error": "lease lost to another worker"}
            self.event(task_id, "step_done", f"{cap.name} done", {"seq": seq})
            return {"status": "done", "output": output}
        finally:
            obs.task_id.reset(ctx_token)
            obs.user_id.reset(user_token)
            obs.step_seq.reset(step_token)
            if otel_token is not None:
                try:
                    otel_context.detach(otel_token)
                except ValueError:  # a step ran code inside its own contextvars Context; nothing to restore
                    pass

    def _fail(self, task_id: str, error: str) -> str:
        found = self._finish(task_id, "status = 'failed', error = ?, lease_owner = NULL, lease_until = NULL", [error])
        if found is not None:
            return found
        self.event(task_id, "failed", error)
        return "failed"

    def run_once(self) -> str | None:
        tid = self.claim()
        if tid:
            self.run_task(tid)
        return tid

    def drain(self, limit: int = 100) -> int:
        n = 0
        while n < limit and self.run_once():
            n += 1
        return n

    # -- workers ----------------------------------------------------------------------------

    def recover_orphans(self) -> int:
        """At engine start, tasks still marked running belong to a process that is gone: DuckDB's file lock lets one
        process at a time open these files, so no other live engine can hold their leases. Expire those leases now
        instead of leaving the work idle until they time out (up to ``lease_seconds``). The normal claim path then
        takes them over, so takeovers are still counted and a task that keeps killing its worker is still abandoned."""
        owner = self.worker_id

        def tx(c) -> list[str]:
            rows = c.execute("SELECT id FROM tasks WHERE status = 'running' AND coalesce(lease_owner, '') <> ? "
                             "AND lease_until > ?", [owner, _now()]).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                c.execute(f"UPDATE tasks SET lease_until = ? WHERE id IN ({', '.join('?' * len(ids))})",
                          [_now() - timedelta(seconds=1), *ids])
            return ids

        ids = self.db.write(tx)
        for tid in ids:
            self.event(tid, "recovered", "The engine restarted while this task ran; resuming from its last finished step")
        if ids:
            log.warning("recovered %d task(s) interrupted by the previous engine process", len(ids))
        return len(ids)

    def start(self, workers: int = 2) -> None:
        self.recover_orphans()
        for i in range(workers):
            th = threading.Thread(target=self._loop, name=f"lyst-worker-{i}", daemon=True)
            th.start()
            self._threads.append(th)

    def _loop(self) -> None:
        last_tick = 0.0
        while not self._stop.is_set():
            try:
                if time.monotonic() - last_tick > 5:
                    self.tick_schedules()
                    self.tick_monitors()
                    last_tick = time.monotonic()
                if time.monotonic() - self._last_prune > 86400:
                    self._last_prune = time.monotonic()
                    try:
                        self.stores.prune(event_days=int(os.environ.get("LYST_RETENTION_DAYS_EVENTS", "90")),
                                          audit_days=int(os.environ.get("LYST_RETENTION_DAYS_AUDIT", "400")))
                    except Exception:  # noqa: BLE001
                        log.exception("retention prune failed")
                if not self.run_once():
                    self._stop.wait(self.poll_seconds)
            except Exception:
                log.exception("worker loop error")
                self._stop.wait(1)

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=10)
        self._threads.clear()
