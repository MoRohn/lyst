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
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

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
AUTONOMY = ("assist", "delegate", "autonomous", "governed")
MAX_STEP_ATTEMPTS = 2


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
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- submission and control -------------------------------------------------------------

    def submit(self, actor: Principal, workspace_id: str, question: str, *, autonomy: str | None = None,
               kind: str = "analysis", schedule_id: str | None = None, max_steps: int = 40,
               budget_seconds: int = 600) -> str:
        self.services.guard.require(actor, workspace_id, "task.create")
        autonomy = autonomy or self.db.scalar("SELECT autonomy FROM workspaces WHERE id = ?", [workspace_id]) or "delegate"
        if autonomy not in AUTONOMY:
            raise ValueError("unknown autonomy mode")
        tid = "t_" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO tasks (id, workspace_id, created_by, kind, title, request, status, autonomy, max_steps, "
            "budget_seconds, schedule_id) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            [tid, workspace_id, actor.user_id, kind, question[:120], json.dumps({"question": question}),
             autonomy, max_steps, budget_seconds, schedule_id],
        )
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
            "SELECT id, title, status, autonomy, kind, created_by, created_at, updated_at, schedule_id "
            "FROM tasks WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?", [workspace_id, limit])

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
        self.services.guard.require(actor, ap["workspace_id"], "approval.decide", approval_id)
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
        self.services.guard.audit(actor, ap["workspace_id"], "approval.decide", approval_id, status)
        if ap["task_id"]:
            self.event(ap["task_id"], f"approval_{status}", f"{ap['summary']}: {status}")
        return {"id": approval_id, "status": status}

    # -- schedules --------------------------------------------------------------------------

    def create_schedule(self, actor: Principal, workspace_id: str, title: str, question: str,
                        every_seconds: int) -> dict[str, Any]:
        self.services.guard.require(actor, workspace_id, "schedule.create")
        if every_seconds < 60:
            raise ValueError("the shortest interval is 60 seconds")
        sid = "s_" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO schedules (id, workspace_id, created_by, title, request, every_seconds, next_run_at, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, false)",
            [sid, workspace_id, actor.user_id, title, json.dumps({"question": question}), every_seconds,
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
                started.append(self.submit(p, ws, json.loads(req)["question"], autonomy="autonomous",
                                           kind="scheduled", schedule_id=sid))
            except AccessDenied:
                log.warning("schedule %s owner lost access; skipped", sid)
        return started

    # -- execution --------------------------------------------------------------------------

    def claim(self) -> str | None:
        now = _now()
        until = now + timedelta(seconds=self.lease_seconds)

        def tx(c):
            row = c.execute(
                "SELECT id FROM tasks WHERE status = 'queued' OR (status = 'running' AND lease_until < ?) "
                "ORDER BY CASE WHEN kind = 'scheduled' THEN 1 ELSE 0 END, created_at LIMIT 1", [now]).fetchone()
            if not row:
                return None
            c.execute("UPDATE tasks SET status = 'running', lease_owner = ?, lease_until = ?, attempts = attempts + 1, "
                      "updated_at = now() WHERE id = ?", [self.worker_id, until, row[0]])
            return row[0]

        tid = self.db.write(tx)
        if tid:
            self.event(tid, "claimed", f"Worker {self.worker_id} started")
        return tid

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
        t = self.db.query("SELECT * FROM tasks WHERE id = ?", [task_id])[0]
        owner = self.db.query("SELECT id, email FROM users WHERE id = ?", [t["created_by"]])[0]
        actor = Principal(owner["id"], owner["email"])
        request = json.loads(t["request"])
        ctx = Context(t["workspace_id"], actor, task_id, self.services, request=request)
        done = self.db.query("SELECT seq, capability, args, output, status FROM task_steps WHERE task_id = ? "
                             "ORDER BY seq", [task_id])
        finished = {r["seq"]: r for r in done if r["status"] == "done"}
        steps = list(FIXED_STEPS)
        if t["plan"]:
            steps += json.loads(t["plan"])["steps"]
        started = time.monotonic()
        seq = 0
        while seq < len(steps):
            step = steps[seq]
            key = step["key"]
            if seq in finished:
                ctx.outputs[key] = json.loads(finished[seq]["output"])
                if key == "plan" and not t["plan"]:
                    steps += ctx.outputs["plan"]["steps"]
                seq += 1
                continue
            state = self._renew(task_id)
            if state != "running":
                self.event(task_id, "stopped", f"Stopped before step {seq} ({state})")
                return state
            if seq >= t["max_steps"]:
                return self._fail(task_id, f"step budget of {t['max_steps']} exhausted")
            if time.monotonic() - started > t["budget_seconds"]:
                return self._fail(task_id, f"time budget of {t['budget_seconds']}s exhausted")
            cap = self.services.registry.get(step["capability"])
            if cap.requires_approval and t["autonomy"] != "autonomous":
                approved = self.db.scalar(
                    "SELECT count(*) FROM approvals WHERE task_id = ? AND action = ? AND status = 'approved'",
                    [task_id, f"step:{seq}"])
                if not approved:
                    pending = self.db.scalar(
                        "SELECT count(*) FROM approvals WHERE task_id = ? AND action = ? AND status = 'pending'",
                        [task_id, f"step:{seq}"])
                    if not pending:
                        self.request_approval(t["workspace_id"], actor.user_id, f"step:{seq}",
                                              f"Run {cap.name}: {cap.description}", step.get("args", {}), task_id)
                    self.db.execute("UPDATE tasks SET status = 'awaiting_approval', lease_owner = NULL WHERE id = ?",
                                    [task_id])
                    self.event(task_id, "awaiting_approval", f"{cap.name} needs approval")
                    return "awaiting_approval"
            args = dict(step.get("args") or {})
            if key == "understand":
                args.setdefault("question", request["question"])
            self.event(task_id, "step_started", f"{cap.agent}: {cap.name}", {"seq": seq})
            self.db.execute(
                "INSERT OR REPLACE INTO task_steps (task_id, seq, phase, capability, agent, args, status, started_at, "
                "idempotency_key) VALUES (?, ?, ?, ?, ?, ?, 'running', now(), ?)",
                [task_id, seq, cap.phase, cap.name, cap.agent, json.dumps(args, default=str), f"{task_id}:{seq}"])
            output, error = None, None
            for attempt in range(1, MAX_STEP_ATTEMPTS + 1):
                try:
                    output = self.services.registry.run(cap.name, ctx, args)
                    break
                except (AccessDenied, KeyError, ValueError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    break  # not transient: do not retry
                except Exception as exc:  # transient or unknown: bounded retry
                    error = f"{type(exc).__name__}: {exc}"
                    log.exception("step %s of %s failed (attempt %d)", seq, task_id, attempt)
            if output is None:
                self.db.execute("UPDATE task_steps SET status = 'failed', error = ?, finished_at = now() "
                                "WHERE task_id = ? AND seq = ?", [error, task_id, seq])
                return self._fail(task_id, f"{cap.name} failed: {error}")
            ctx.outputs[key] = output
            payload = json.dumps(output, default=str)

            def commit(c, seq=seq, payload=payload, key=key, output=output):
                c.execute("UPDATE task_steps SET status = 'done', output = ?, finished_at = now() "
                          "WHERE task_id = ? AND seq = ?", [payload, task_id, seq])
                if key == "plan":
                    c.execute("UPDATE tasks SET plan = ? WHERE id = ?", [payload, task_id])

            self.db.write(commit)
            if key == "plan":
                steps += output["steps"]
            self.event(task_id, "step_done", f"{cap.name} done", {"seq": seq})
            seq += 1
        result = ctx.outputs.get("deliver")
        self.db.execute("UPDATE tasks SET status = 'done', result = ?, updated_at = now(), lease_owner = NULL, "
                        "lease_until = NULL WHERE id = ?", [json.dumps(result, default=str), task_id])
        self.event(task_id, "done", (result or {}).get("answer", "Done")[:200])
        return "done"

    def _fail(self, task_id: str, error: str) -> str:
        self.db.execute("UPDATE tasks SET status = 'failed', error = ?, updated_at = now(), lease_owner = NULL "
                        "WHERE id = ?", [error, task_id])
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

    def start(self, workers: int = 2) -> None:
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
                    last_tick = time.monotonic()
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
