# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""One event model for everything that happens in Lyst, read by webhooks, the SDK and in-process consumers.

Events are not emitted by each service. They are derived from two records Lyst already keeps faithfully: the
audit log (every governed action) and the tasks table (every analysis run). A collector on the worker tick reads
both from a stored cursor and writes ``platform_events`` (control migration 40), so a new audited action becomes a
public event by adding one line to ``CATALOG`` and no service is instrumented twice.

An event carries ids, versions, counts and outcomes only, taken from the already-redacted audit detail; never an
answer's text or a row of data. Consumers re-read the subject through the API under their own permissions.

    {"seq": 812, "id": "a:5531", "type": "dataset.version_created", "schema": 1, "workspace_id": "northwind",
     "occurred_at": "2026-11-04T19:02:11+00:00", "actor": "u_9f..", "subject": {"kind": "dataset", "id": "sales"},
     "data": {"version": 3, "rows": 1200}, "source": "audit"}
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .security import RANK, Guard, Principal
from .store import Stores

log = logging.getLogger("lyst.events")
SCHEMA = 1

# audit action -> (event type, subject kind). Subject id is the audit target.
CATALOG: dict[str, tuple[str, str]] = {
    "data.ingest": ("dataset.version_created", "dataset"),
    "ingest.commit": ("ingest.committed", "ingest"),
    "document.add": ("document.added", "document"),
    "task.create": ("task.created", "task"),
    "approval.decide": ("approval.decided", "task"),
    "metric.define": ("metric.defined", "metric"),
    "metric.deprecate": ("metric.deprecated", "metric"),
    "contract.define": ("contract.defined", "dataset"),
    "contract.check": ("contract.checked", "dataset"),
    "contract.retire": ("contract.retired", "dataset"),
    "connection.create": ("connection.created", "connection"),
    "connection.sync": ("connection.synced", "connection"),
    "connection.delete": ("connection.deleted", "connection"),
    "asset.version": ("asset.versioned", "asset"),
    "asset.repair": ("asset.repaired", "asset"),
    "asset.retire": ("asset.retired", "asset"),
    "dashboard.build": ("dashboard.built", "dashboard"),
    "dashboard.refresh": ("dashboard.refreshed", "dashboard"),
    "dashboard.retire": ("dashboard.retired", "dashboard"),
    "artifact.create": ("artifact.created", "artifact"),
    "review.request": ("review.requested", "review"),
    "review.decide": ("review.decided", "review"),
    "assignment.create": ("assignment.created", "assignment"),
    "assignment.complete": ("assignment.completed", "assignment"),
    "monitor.create": ("monitor.created", "monitor"),
    "member.set": ("member.added", "member"),
    "member.remove": ("member.removed", "member"),
    "workflow.compile": ("workflow.compiled", "workflow"),
    "workflow.retire": ("workflow.retired", "workflow"),
    "schedule.create": ("schedule.created", "schedule"),
    "skill.register": ("skill.registered", "skill"),
    "experiment.create": ("experiment.created", "experiment"),
    "experiment.run": ("experiment.ran", "experiment"),
    "experiment.decide": ("experiment.decided", "experiment"),
    "workbench.python": ("workbench.ran", "workbench_run"),
    "workspace.export": ("workspace.exported", "workspace"),
    "workspace.import": ("workspace.imported", "workspace"),
    # operations (quotas, scenarios, decision records)
    "quota.set": ("quota.set", "workspace"),
    "scenario.define": ("scenario.defined", "scenario"),
    "scenario.save": ("scenario.saved", "scenario"),
    "decision.create": ("decision.created", "decision"),
    "decision.submit": ("decision.submitted", "decision"),
    "decision.review": ("decision.reviewed", "decision"),
    "decision.approve": ("decision.approved", "decision"),
    # adaptability (onboarding, classification, governance)
    "onboarding.discover": ("onboarding.discovered", "workspace"),
    "onboarding.accept": ("onboarding.accepted", "proposal"),
    "anticipation.accept": ("anticipation.accepted", "anticipation"),
    "anticipation.settings": ("anticipation.settings_changed", "workspace"),
    "onboarding.profile": ("onboarding.profiled", "workspace"),
    "data.classify": ("dataset.classified", "dataset"),
    "data.policy": ("policy.changed", "dataset"),
    "policy.export": ("policy.export_denied", "workspace"),
    "policy.read": ("policy.read_denied", "dataset"),
}
# events from the tasks table: status -> type
TASK_STATES = {"done": "task.completed", "failed": "task.failed", "awaiting_approval": "task.awaiting_approval"}
# audit outcomes that still make an event (a failed contract check is news; a denied request is not)
ANY_OUTCOME = {"contract.check", "connection.sync", "policy.export", "policy.read"}
# event types every member may read; the rest (membership, approvals, others' tasks) need audit.read
MEMBER_TYPES = ("dataset.*", "ingest.*", "document.*", "metric.*", "contract.*", "connection.synced", "asset.*",
                "dashboard.*", "artifact.*", "workflow.*", "scenario.*", "decision.*", "experiment.*")


def types() -> list[str]:
    return sorted({t for t, _ in CATALOG.values()} | set(TASK_STATES.values()))


def matches(event_type: str, patterns: list[str] | None) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(event_type, p) for p in patterns)


def _small(detail: Any) -> dict[str, Any]:
    """Scalars and short lists of scalars from an (already redacted) audit detail."""
    out: dict[str, Any] = {}
    if not isinstance(detail, dict):
        return out
    for k, v in list(detail.items())[:20]:
        if isinstance(v, bool | int | float) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:200]
        elif isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str | int | float | bool) for x in v):
            out[k] = [x[:120] if isinstance(x, str) else x for x in v]
    return out


def row_to_event(r: dict[str, Any]) -> dict[str, Any]:
    return {"seq": r["seq"], "id": r["id"], "type": r["type"], "schema": SCHEMA, "workspace_id": r["workspace_id"],
            "occurred_at": r["occurred_at"].isoformat() if hasattr(r["occurred_at"], "isoformat") else str(r["occurred_at"]),
            "actor": r["actor"], "subject": {"kind": r["subject_kind"], "id": r["subject_id"]},
            "data": json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {}), "source": r["source"]}


class EventService:
    def __init__(self, stores: Stores, guard: Guard) -> None:
        self.stores = stores
        self.guard = guard
        self.subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        self._new = threading.Condition()

    # -- collection -------------------------------------------------------------------------

    def _cursor(self, name: str, default: str) -> str:
        v = self.stores.control.scalar("SELECT position FROM event_cursors WHERE name = ?", [name])
        return v if v is not None else default

    def collect(self) -> list[dict[str, Any]]:
        """Turn new audit lines and task state changes into events. Safe to call from any thread, any time."""
        with self._lock:
            new = self._collect_audit() + self._collect_tasks()
        if new:
            with self._new:
                self._new.notify_all()
            for ev in new:
                for fn in self.subscribers:
                    try:
                        fn(ev)
                    except Exception:  # noqa: BLE001 - a consumer never blocks the stream
                        log.exception("event subscriber failed on %s", ev["id"])
        return new

    def _collect_audit(self) -> list[dict[str, Any]]:
        ctl = self.stores.control
        after = int(self._cursor("audit", "0"))
        rows = ctl.query("SELECT id, occurred_at, actor, workspace_id, action, target, outcome, detail FROM audit_log "
                         "WHERE id > ? ORDER BY id LIMIT 2000", [after])
        if not rows:
            return []
        items = []
        for r in rows:
            mapped = CATALOG.get(r["action"])
            if not mapped or (r["outcome"] != "ok" and r["action"] not in ANY_OUTCOME):
                continue
            detail = json.loads(r["detail"]) if isinstance(r["detail"], str) else (r["detail"] or {})
            data = _small(detail)
            if r["outcome"] != "ok":
                data["outcome"] = r["outcome"]
            items.append((f"a:{r['id']}", mapped[0], r["workspace_id"], r["occurred_at"], r["actor"], mapped[1],
                          r["target"], data, "audit"))
        return self._write(items, ("audit", str(rows[-1]["id"])))

    def _collect_tasks(self) -> list[dict[str, Any]]:
        ctl = self.stores.control
        since = self._cursor("tasks", "1970-01-01 00:00:00+00")
        rows = ctl.query("SELECT id, workspace_id, created_by, kind, status, updated_at, "
                         "CASE WHEN status = 'done' THEN result->>'$.status' END AS outcome, error "
                         "FROM tasks WHERE updated_at >= CAST(? AS TIMESTAMPTZ) - INTERVAL 5 SECOND "
                         "AND status IN ('done', 'failed', 'awaiting_approval') ORDER BY updated_at LIMIT 2000", [since])
        if not rows:
            return []
        items = []
        for r in rows:
            data = {"kind": r["kind"], "created_by": r["created_by"]}
            if r["outcome"]:
                data["outcome"] = r["outcome"]
            if r["status"] == "failed" and r["error"]:
                data["error"] = str(r["error"])[:200]
            items.append((f"t:{r['id']}:{r['status']}", TASK_STATES[r["status"]], r["workspace_id"], r["updated_at"],
                          "system", "task", r["id"], data, "task"))
        return self._write(items, ("tasks", str(rows[-1]["updated_at"])))

    def _write(self, items: list[tuple], cursor: tuple[str, str]) -> list[dict[str, Any]]:
        def tx(c):
            created = []
            for eid, etype, ws, at, actor, skind, sid, data, source in items:
                if c.execute("SELECT 1 FROM platform_events WHERE id = ?", [eid]).fetchall():
                    continue
                seq = c.execute("SELECT nextval('platform_event_seq')").fetchone()[0]
                c.execute("INSERT INTO platform_events (seq, id, type, workspace_id, occurred_at, actor, subject_kind, "
                          "subject_id, data, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          [seq, eid, etype, ws, at, actor, skind, sid, json.dumps(data, default=str), source])
                created.append(seq)
            c.execute("INSERT OR REPLACE INTO event_cursors (name, position) VALUES (?, ?)", list(cursor))
            return created

        seqs = self.stores.control.write(tx)
        if not seqs:
            return []
        rows = self.stores.control.query(
            f"SELECT * FROM platform_events WHERE seq IN ({','.join('?' * len(seqs))}) ORDER BY seq", seqs)
        return [row_to_event(r) for r in rows]

    # -- reading ----------------------------------------------------------------------------

    def since(self, ws: str, after: int = 0, *, types: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Events of one workspace after a cursor, unfiltered by permission (in-process consumers)."""
        rows = self.stores.control.query("SELECT * FROM platform_events WHERE workspace_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                                         [ws, after, min(limit, 1000) * (4 if types else 1)])
        return [e for e in (row_to_event(r) for r in rows) if matches(e["type"], types)][:limit]

    def visible(self, actor: Principal, ws: str, event: dict[str, Any], role: str) -> bool:
        if RANK[role] >= RANK["admin"]:
            return True
        if event["type"].startswith("task.") or event["type"] == "approval.decided":
            return event["data"].get("created_by") == actor.user_id or event["actor"] == actor.user_id
        return matches(event["type"], list(MEMBER_TYPES))

    def list(self, actor: Principal, ws: str, *, after: int = 0, types: list[str] | None = None, limit: int = 100,
             wait: float = 0) -> dict[str, Any]:
        """Events this member may see, after ``after``. ``wait`` seconds (at most 25) long-polls for the next one."""
        role = self.guard.require(actor, ws, "workspace.read")
        if after < 0:  # "from now": the current end of the stream, no events
            self.collect()
            return {"events": [], "next": int(self.stores.control.scalar(
                "SELECT coalesce(max(seq), 0) FROM platform_events WHERE workspace_id = ?", [ws]) or 0)}
        deadline = time.monotonic() + min(max(wait, 0), 25)
        while True:
            self.collect()
            out = [e for e in self.since(ws, after, types=types, limit=limit * 2) if self.visible(actor, ws, e, role)][:limit]
            if out or time.monotonic() >= deadline:
                last = self.stores.control.scalar("SELECT max(seq) FROM platform_events WHERE workspace_id = ?", [ws]) or 0
                cursor = out[-1]["seq"] if out else max(after, int(last))
                return {"events": out, "next": cursor}
            with self._new:
                self._new.wait(timeout=min(1.0, max(0.05, deadline - time.monotonic())))

    def prune(self, days: int = 90) -> int:
        n = self.stores.control.scalar("SELECT count(*) FROM platform_events WHERE occurred_at < now() - to_days(CAST(? AS BIGINT))", [days]) or 0
        if n:
            self.stores.control.execute("DELETE FROM platform_events WHERE occurred_at < now() - to_days(CAST(? AS BIGINT))", [days])
        return int(n)


__all__ = ["CATALOG", "EventService", "matches", "types"]
