# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""One event model for everything that happens in Lyst, read by webhooks, the SDK, the UI and in-process consumers.

Where events come from
    * **Derived** (most of them): the audit log (every governed action) and the tasks table (every analysis run). A
      collector reads both from a stored cursor, so a new audited action becomes a public event by adding one line to
      ``CATALOG`` and no service is instrumented twice.
    * **Emitted** (facts the audit log cannot express, such as a dataset's schema changing): a service *stages* the
      event in the same workspace transaction as the state change it describes (``stage``, a transactional outbox in
      the workspace database, workspace migration 110), or emits it after the fact (``emit``). The collector moves
      outbox rows into ``platform_events``; a crash between the two writes publishes the row again and the event id
      keeps it from being stored twice.
    * **External**: an admin (or an integration's token) posts an ``external.*`` event through the API.

The envelope (``Event``, envelope version ``SCHEMA``)
    ``seq`` is the workspace stream's monotonic cursor; ``id`` is stable and unique; ``type`` and ``type_version``
    name the contract; ``correlation_id`` groups everything one root cause led to and ``causation_id`` names the event
    that directly caused this one; ``depth`` counts the hops from the root; ``sensitivity`` follows the subject's
    classification; ``dedupe_key`` makes a repeated fact one event; ``related`` lists other objects it concerns.
    An event carries ids, versions, counts and outcomes only, never an answer's text or a row of data. Consumers
    re-read the subject through the API under their own permissions.

Processing (at least once, never assumed exactly once)
    A durable consumer (``consume``) is a named handler with a stored cursor and an inbox (control migration 110). Each
    event is handed to it until the handler returns; success is recorded per (consumer, event), so a redelivered
    event is skipped. A failure is retried with backoff up to the consumer's attempt cap, then dead-lettered (a poison
    event stops only itself, never the stream). Handlers must be idempotent; ``inbox`` state is what makes a
    duplicate delivery harmless, not a promise that duplicates never happen.

Chains
    An event emitted because of another inherits its correlation id and is one hop deeper. Past ``MAX_DEPTH`` hops the
    event is refused and an ``event.chain_stopped`` record is written instead, so rules that trigger one another stop.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .security import RANK, Guard, Principal
from .store import Stores

log = logging.getLogger("lyst.events")
SCHEMA = 2  # envelope version; 2 added correlation, causation, depth, sensitivity, dedupe key, related, type version
SENSITIVITY = ("public", "internal", "confidential", "restricted")
SOURCES = ("audit", "task", "service", "api", "rule", "schedule", "system")
_TYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def max_depth() -> int:
    return max(1, int(os.environ.get("LYST_EVENT_MAX_DEPTH", "6")))


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
    "publication.publish": ("publication.published", "publication"),
    "publication.revoke": ("publication.revoked", "publication"),
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
# emitted by services through the outbox (type -> what it means); listed so /v1/events/types is complete
EMITTED: dict[str, str] = {
    "dataset.schema_changed": "a new dataset version added, removed or retyped columns",
    "asset.health_changed": "a registered deliverable became current, stale, invalidated or broken",
    "metric.deviated": "a monitor found a validated deviation (robust shift, forecast deviation, quality check)",
    "anticipation.found": "anticipation recorded something worth preparing or watching",
    "event.chain_stopped": "an event was refused because its causal chain reached the depth limit",
    "event.dead_lettered": "a durable consumer gave up on an event after its attempt cap",
}
# audit outcomes that still make an event (a failed contract check is news; a denied request is not)
ANY_OUTCOME = {"contract.check", "connection.sync", "policy.export", "policy.read"}
# event types every member may read; the rest (membership, approvals, others' tasks) need audit.read
MEMBER_TYPES = ("dataset.*", "ingest.*", "document.*", "metric.*", "contract.*", "connection.synced", "asset.*",
                "dashboard.*", "artifact.*", "workflow.*", "scenario.*", "decision.*", "experiment.*", "publication.*")


class Subject(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    id: str = Field(min_length=1, max_length=200)


class Event(BaseModel):
    """The canonical envelope, as stored and as served (API, SSE, webhooks). ``docs/events/envelope.schema.json``."""

    model_config = ConfigDict(populate_by_name=True)

    seq: int
    id: str
    type: str
    schema_: int = Field(SCHEMA, alias="schema")
    type_version: int = 1
    workspace_id: str | None
    occurred_at: str
    actor: str | None
    subject: Subject
    data: dict[str, Any] = {}
    source: Literal["audit", "task", "service", "api", "rule", "schedule", "system"]
    correlation_id: str
    causation_id: str | None = None
    depth: int = 0
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    dedupe_key: str | None = None
    related: list[Subject] = []


class ChainLimit(RuntimeError):
    """Raised by ``stage`` with ``strict=True`` when the causal chain is already ``max_depth`` hops deep."""


class PermanentFailure(Exception):
    """Raised by a consumer's handler when retrying cannot help: the event is dead-lettered at once."""


def types() -> list[str]:
    return sorted({t for t, _ in CATALOG.values()} | set(TASK_STATES.values()) | set(EMITTED))


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


def _json(v: Any, default: Any) -> Any:
    if v is None:
        return default
    return json.loads(v) if isinstance(v, str) else v


def row_to_event(r: dict[str, Any]) -> dict[str, Any]:
    at = r["occurred_at"]
    return {"seq": r["seq"], "id": r["id"], "type": r["type"], "schema": SCHEMA,
            "type_version": r.get("type_version") or 1, "workspace_id": r["workspace_id"],
            "occurred_at": at.isoformat() if hasattr(at, "isoformat") else str(at),
            "actor": r["actor"], "subject": {"kind": r["subject_kind"], "id": r["subject_id"]},
            "data": _json(r["data"], {}), "source": r["source"],
            "correlation_id": r.get("correlation_id") or r["id"], "causation_id": r.get("causation_id"),
            "depth": r.get("depth") or 0, "sensitivity": r.get("sensitivity") or "internal",
            "dedupe_key": r.get("dedupe_key"), "related": _json(r.get("related"), [])}


def lineage(cause: dict[str, Any] | None) -> tuple[str | None, str | None, int]:
    """(correlation id, causation id, depth) for an event caused by ``cause`` (an event dict, or None for a root)."""
    if not cause:
        return None, None, 0
    return cause.get("correlation_id") or cause["id"], cause["id"], int(cause.get("depth") or 0) + 1


@dataclass
class Consumer:
    name: str
    handler: Callable[[dict[str, Any]], Any]
    types: list[str] | None = None
    max_attempts: int = 5
    stats: dict[str, int] = field(default_factory=lambda: {"done": 0, "retry": 0, "dead": 0, "skipped": 0})


def backoff(attempts: int) -> timedelta:
    """5 s, 30 s, 2 min, 10 min, then 30 min between attempts."""
    return timedelta(seconds=(5, 30, 120, 600)[attempts - 1] if attempts <= 4 else 1800)


class EventService:
    SWEEP_SECONDS = 30.0  # every outbox is swept at least this often (crash recovery); emits nudge it at once

    def __init__(self, stores: Stores, guard: Guard) -> None:
        self.stores = stores
        self.guard = guard
        self.subscribers: list[Callable[[dict[str, Any]], None]] = []  # in-memory, best effort (cheap nudges)
        self.consumers: dict[str, Consumer] = {}  # durable, at least once (see consume)
        self._lock = threading.Lock()
        self._dispatching = threading.Lock()
        self._new = threading.Condition()
        self._pending: set[str] = set()  # workspaces whose outbox has rows to publish
        self._last_sweep = -1e9

    # -- emission (the transactional outbox) ------------------------------------------------

    def stage(self, c, ws: str, type_: str, *, subject: tuple[str, str], data: dict[str, Any] | None = None,
              actor: str | None = None, source: str = "service", cause: dict[str, Any] | None = None,
              dedupe_key: str | None = None, sensitivity: str = "internal", related: list[tuple[str, str]] | None = None,
              type_version: int = 1, strict: bool = False) -> str | None:
        """Write an event to workspace ``ws``'s outbox on ``c``, the caller's open workspace transaction, so the event
        exists exactly when the state change it describes commits. Returns the outbox id; None when the dedupe key was
        already used or the chain is too deep (then an ``event.chain_stopped`` record is staged instead)."""
        if not _TYPE.match(type_):
            raise ValueError(f"invalid event type {type_!r}")
        if source not in SOURCES:
            raise ValueError(f"invalid event source {source!r}")
        if sensitivity not in SENSITIVITY:
            raise ValueError(f"invalid sensitivity {sensitivity!r}")
        correlation, causation, depth = lineage(cause)
        dedupe = (dedupe_key or "").strip()[:400] or None
        if depth > max_depth():
            if strict:
                raise ChainLimit(f"the causal chain of {cause and cause.get('id')} is already {depth - 1} hops deep")
            log.warning("event %s refused: chain %s reached depth %s", type_, correlation, depth)
            stop_key = f"chain_stopped:{correlation}:{type_}"
            if c.execute("SELECT 1 FROM event_outbox WHERE dedupe_key = ?", [stop_key]).fetchall():
                return None
            self._insert(c, ws, "event.chain_stopped", subject, {"refused_type": type_, "depth": depth,
                                                                 "limit": max_depth()},
                         None, "system", correlation, causation, depth - 1, stop_key, "internal", [], 1)
            return None
        if dedupe and c.execute("SELECT 1 FROM event_outbox WHERE dedupe_key = ?", [dedupe]).fetchall():
            return None
        return self._insert(c, ws, type_, subject, _small(data or {}), actor, source, correlation, causation, depth,
                            dedupe, sensitivity, [{"kind": k, "id": str(i)} for k, i in (related or [])][:50],
                            type_version)

    def _insert(self, c, ws, type_, subject, data, actor, source, correlation, causation, depth, dedupe, sensitivity,
                related, type_version) -> str:
        oid = "e_" + uuid.uuid4().hex[:16]
        c.execute("INSERT INTO event_outbox (id, type, type_version, actor, subject_kind, subject_id, data, source, "
                  "correlation_id, causation_id, depth, sensitivity, dedupe_key, related) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  [oid, type_, type_version, actor, subject[0], str(subject[1])[:200], json.dumps(data, default=str),
                   source, correlation or f"o:{ws}:{oid}", causation, depth, sensitivity, dedupe,
                   json.dumps(related)])
        self._pending.add(ws)
        return oid

    def emit(self, ws: str, type_: str, **kw: Any) -> dict[str, Any] | None:
        """``stage`` in a transaction of its own, then publish. Returns the stored event, or None when it was a
        duplicate or refused. For a fact that must commit with a state change, call ``stage`` inside that write."""
        oid = self.stores.workspace(ws).write(lambda c: self.stage(c, ws, type_, **kw))
        if oid is None:
            return None
        self.collect()
        rows = self.stores.control.query("SELECT * FROM platform_events WHERE id = ?", [f"o:{ws}:{oid}"])
        return row_to_event(rows[0]) if rows else None

    def external(self, actor: Principal, ws: str, type_: str, *, subject: tuple[str, str], data: dict[str, Any] | None,
                 dedupe_key: str | None, sensitivity: str = "internal") -> dict[str, Any]:
        """An integration's event (``external.<system>.<what>``), posted through the API by a workspace admin."""
        self.guard.require(actor, ws, "workspace.admin", type_)
        if not type_.startswith("external."):
            raise ValueError("an event posted through the API must be named external.<system>.<what>")
        ev = self.emit(ws, type_, subject=subject, data=data, actor=actor.user_id, source="api",
                       dedupe_key=dedupe_key, sensitivity=sensitivity)
        if ev is None and dedupe_key:
            rows = self.stores.control.query("SELECT * FROM platform_events WHERE workspace_id = ? AND dedupe_key = ?",
                                             [ws, dedupe_key.strip()[:400]])
            if rows:
                return row_to_event(rows[0]) | {"duplicate": True}
        if ev is None:
            raise ValueError("the event was not accepted")
        return ev

    # -- collection -------------------------------------------------------------------------

    def _cursor(self, name: str, default: str) -> str:
        v = self.stores.control.scalar("SELECT position FROM event_cursors WHERE name = ?", [name])
        return v if v is not None else default

    def collect(self) -> list[dict[str, Any]]:
        """Turn new audit lines, task state changes and outbox rows into events. Safe from any thread, any time."""
        with self._lock:
            new = self._collect_audit() + self._collect_tasks() + self._collect_outbox()
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

    def _sensitivity(self, ws: str | None, kind: str, sid: str | None, cache: dict) -> str:
        if not ws or kind != "dataset" or not sid:
            return "internal"
        key = (ws, sid)
        if key not in cache:
            try:
                level = self.stores.workspace(ws).scalar("SELECT classification FROM datasets WHERE name = ?", [sid])
            except Exception:  # noqa: BLE001 - a workspace that cannot be opened keeps the default
                level = None
            cache[key] = level if level in SENSITIVITY else "internal"
        return cache[key]

    def _collect_audit(self) -> list[dict[str, Any]]:
        ctl = self.stores.control
        after = int(self._cursor("audit", "0"))
        rows = ctl.query("SELECT id, occurred_at, actor, workspace_id, action, target, outcome, detail FROM audit_log "
                         "WHERE id > ? ORDER BY id LIMIT 2000", [after])
        if not rows:
            return []
        items, cache = [], {}
        for r in rows:
            mapped = CATALOG.get(r["action"])
            if not mapped or (r["outcome"] != "ok" and r["action"] not in ANY_OUTCOME):
                continue
            data = _small(_json(r["detail"], {}))
            if r["outcome"] != "ok":
                data["outcome"] = r["outcome"]
            eid = f"a:{r['id']}"
            items.append({"id": eid, "type": mapped[0], "workspace_id": r["workspace_id"], "occurred_at": r["occurred_at"],
                          "actor": r["actor"], "subject_kind": mapped[1], "subject_id": r["target"], "data": data,
                          "source": "audit", "correlation_id": eid,
                          "sensitivity": self._sensitivity(r["workspace_id"], mapped[1], r["target"], cache)})
        return self._write(items, ("audit", str(rows[-1]["id"])))

    def _collect_tasks(self) -> list[dict[str, Any]]:
        ctl = self.stores.control
        since = self._cursor("tasks", "1970-01-01 00:00:00+00")
        rows = ctl.query("SELECT id, workspace_id, created_by, kind, status, updated_at, "
                         "CASE WHEN status = 'done' THEN result->>'$.status' END AS outcome, error, "
                         "request->'$.trigger' AS trigger "
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
            eid = f"t:{r['id']}:{r['status']}"
            # a task an event started continues that event's chain, so rules that start tasks cannot loop for ever
            trig = _json(r["trigger"], None)
            correlation, causation, depth = lineage(trig if isinstance(trig, dict) and trig.get("id") else None)
            items.append({"id": eid, "type": TASK_STATES[r["status"]], "workspace_id": r["workspace_id"],
                          "occurred_at": r["updated_at"], "actor": "system", "subject_kind": "task", "subject_id": r["id"],
                          "data": data, "source": "task", "correlation_id": correlation or f"task:{r['id']}",
                          "causation_id": causation, "depth": depth})
        return self._write(items, ("tasks", str(rows[-1]["updated_at"])))

    def _collect_outbox(self) -> list[dict[str, Any]]:
        mono = time.monotonic()
        if mono - self._last_sweep >= self.SWEEP_SECONDS:
            self._last_sweep = mono
            targets = {w["id"] for w in self.stores.control.query("SELECT id FROM workspaces")} | self._pending
        else:
            targets = set(self._pending)
        self._pending.clear()
        out: list[dict[str, Any]] = []
        for ws in sorted(targets):
            try:
                db = self.stores.workspace(ws)
                rows = db.query("SELECT * FROM event_outbox WHERE published_at IS NULL ORDER BY n LIMIT 500")
            except Exception:  # noqa: BLE001 - a workspace that cannot be opened is retried on the next sweep
                log.exception("event outbox of %s could not be read", ws)
                continue
            if not rows:
                continue
            items = [{"id": f"o:{ws}:{r['id']}", "type": r["type"], "type_version": r["type_version"], "workspace_id": ws,
                      "occurred_at": r["occurred_at"], "actor": r["actor"], "subject_kind": r["subject_kind"],
                      "subject_id": r["subject_id"], "data": _json(r["data"], {}), "source": r["source"],
                      "correlation_id": r["correlation_id"], "causation_id": r["causation_id"], "depth": r["depth"],
                      "sensitivity": r["sensitivity"], "dedupe_key": r["dedupe_key"], "related": _json(r["related"], [])}
                     for r in rows]
            out += self._write(items, None)
            ids = [r["id"] for r in rows]
            db.execute(f"UPDATE event_outbox SET published_at = now() WHERE id IN ({','.join('?' * len(ids))})", ids)
            if len(rows) == 500:
                self._pending.add(ws)
        return out

    def _write(self, items: list[dict[str, Any]], cursor: tuple[str, str] | None) -> list[dict[str, Any]]:
        def tx(c):
            created = []
            for it in items:
                if c.execute("SELECT 1 FROM platform_events WHERE id = ?", [it["id"]]).fetchall():
                    continue
                if it.get("dedupe_key") and c.execute("SELECT 1 FROM platform_events WHERE workspace_id = ? AND dedupe_key = ?",
                                                      [it["workspace_id"], it["dedupe_key"]]).fetchall():
                    continue
                seq = c.execute("SELECT nextval('platform_event_seq')").fetchone()[0]
                c.execute("INSERT INTO platform_events (seq, id, type, workspace_id, occurred_at, actor, subject_kind, "
                          "subject_id, data, source, type_version, correlation_id, causation_id, depth, sensitivity, "
                          "dedupe_key, related) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          [seq, it["id"], it["type"], it["workspace_id"], it["occurred_at"], it["actor"],
                           it["subject_kind"], it["subject_id"], json.dumps(it["data"], default=str), it["source"],
                           it.get("type_version") or 1, it.get("correlation_id") or it["id"], it.get("causation_id"),
                           it.get("depth") or 0, it.get("sensitivity") or "internal", it.get("dedupe_key"),
                           json.dumps(it.get("related") or [])])
                created.append(seq)
            if cursor:
                c.execute("INSERT OR REPLACE INTO event_cursors (name, position) VALUES (?, ?)", list(cursor))
            return created

        seqs = self.stores.control.write(tx)
        if not seqs:
            return []
        rows = self.stores.control.query(
            f"SELECT * FROM platform_events WHERE seq IN ({','.join('?' * len(seqs))}) ORDER BY seq", seqs)
        return [row_to_event(r) for r in rows]

    # -- durable consumers ------------------------------------------------------------------

    def consume(self, name: str, handler: Callable[[dict[str, Any]], Any], *, types: list[str] | None = None,
                max_attempts: int = 5) -> Consumer:
        """Register a durable consumer. A new consumer starts at the current end of the stream (it does not replay
        history unless ``replay`` is called). The handler must be idempotent: delivery is at least once."""
        if not re.match(r"^[a-z][a-z0-9_.-]{1,60}$", name):
            raise ValueError("invalid consumer name")
        self.consumers[name] = Consumer(name, handler, types, max(1, max_attempts))
        return self.consumers[name]

    def _position(self, name: str) -> int:
        pos = self.stores.control.scalar("SELECT position FROM event_consumers WHERE name = ?", [name])
        if pos is None:
            pos = int(self.stores.control.scalar("SELECT coalesce(max(seq), 0) FROM platform_events") or 0)
            self.stores.control.execute("INSERT OR IGNORE INTO event_consumers (name, position) VALUES (?, ?)", [name, pos])
        return int(pos)

    def dispatch(self, *, limit: int = 200) -> dict[str, int]:
        """Hand new and due-for-retry events to every durable consumer. Called from the worker tick; one at a time."""
        if not self._dispatching.acquire(blocking=False):
            return {"handled": 0}
        try:
            self.collect()
            handled = 0
            for c in list(self.consumers.values()):
                handled += self._dispatch_one(c, limit)
            return {"handled": handled}
        finally:
            self._dispatching.release()

    def _dispatch_one(self, c: Consumer, limit: int) -> int:
        ctl = self.stores.control
        pos = self._position(c.name)
        rows = ctl.query("SELECT * FROM platform_events WHERE seq > ? ORDER BY seq LIMIT ?", [pos, limit])
        n = 0
        batch: list[list[Any]] = []
        mine = [r["id"] for r in rows if matches(r["type"], c.types)]
        known = {x["event_id"]: x for x in ctl.query(  # one read of this page's inbox records, not one per event
            f"SELECT event_id, status, attempts FROM event_inbox WHERE consumer = ? AND event_id IN ({','.join('?' * len(mine))})",
            [c.name, *mine])} if mine else {}
        for r in rows:
            ev = row_to_event(r)
            if matches(ev["type"], c.types):
                # handled events are recorded in batches with the cursor (one write per BATCH events); a crash
                # re-delivers at most one batch, and the inbox (or the handler's own idempotency) makes that harmless
                self._process(c, ev, advance=True, batch=batch, prior=[known[ev["id"]]] if ev["id"] in known else [])
                n += 1
                if len(batch) >= self.BATCH:
                    self._flush(c, batch)
        self._flush(c, batch)
        if rows and (not n or not matches(row_to_event(rows[-1])["type"], c.types)):
            ctl.execute("UPDATE event_consumers SET position = ?, updated_at = now() WHERE name = ?", [rows[-1]["seq"], c.name])
        due = ctl.query("SELECT p.* FROM event_inbox i JOIN platform_events p ON p.id = i.event_id WHERE i.consumer = ? "
                        "AND i.status = 'retry' AND i.next_attempt_at <= now() ORDER BY p.seq LIMIT ?", [c.name, limit])
        for r in due:
            self._process(c, row_to_event(r))
            n += 1
        return n

    BATCH = 50  # handled events recorded per write

    def _flush(self, c: Consumer, batch: list[list[Any]]) -> None:
        """Record a batch of handled events and move the cursor past the last one, in one transaction."""
        if not batch:
            return
        rows, top = list(batch), max(b[3] for b in batch)
        batch.clear()

        def tx(x):
            x.executemany("INSERT OR REPLACE INTO event_inbox (consumer, event_id, workspace_id, seq, status, attempts, "
                          "last_error, next_attempt_at, duration_ms, updated_at) VALUES (?, ?, ?, ?, 'done', ?, NULL, NULL, ?, now())",
                          [b for b in rows if b[4] is not None])
            x.execute("UPDATE event_consumers SET position = greatest(position, ?), updated_at = now() WHERE name = ?",
                      [top, c.name])

        self.stores.control.write(tx)

    def _process(self, c: Consumer, ev: dict[str, Any], *, advance: bool = False,
                 batch: list[list[Any]] | None = None, prior: list[dict[str, Any]] | None = None) -> None:
        ctl = self.stores.control
        pending = batch if (advance and batch is not None) else None
        move = (lambda x: x.execute("UPDATE event_consumers SET position = greatest(position, ?), updated_at = now() "
                                    "WHERE name = ?", [ev["seq"], c.name])) if advance else (lambda x: None)
        if prior is None:
            prior = ctl.query("SELECT status, attempts FROM event_inbox WHERE consumer = ? AND event_id = ?", [c.name, ev["id"]])
        if prior and prior[0]["status"] in ("done", "dead"):
            c.stats["skipped"] += 1  # a redelivery: the first delivery's outcome stands
            if pending is not None:
                pending.append([c.name, ev["id"], ev["workspace_id"], ev["seq"], None, None])  # cursor only
            else:
                ctl.write(move)
            return
        attempts = (prior[0]["attempts"] if prior else 0) + 1
        started = time.monotonic()
        try:
            c.handler(ev)
        except Exception as exc:  # noqa: BLE001 - recorded, retried or dead-lettered; never stops the stream
            permanent = isinstance(exc, PermanentFailure) or attempts >= c.max_attempts
            status = "dead" if permanent else "retry"
            err = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("consumer %s %s event %s (attempt %s): %s", c.name, "dead-lettered" if permanent else "will retry",
                        ev["id"], attempts, err)
            if pending is not None:
                self._flush(c, pending)  # earlier handled events are recorded before the cursor passes this one

            def fail(x):
                x.execute("INSERT OR REPLACE INTO event_inbox (consumer, event_id, workspace_id, seq, status, attempts, "
                          "last_error, next_attempt_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())",
                          [c.name, ev["id"], ev["workspace_id"], ev["seq"], status, attempts, err,
                           None if permanent else datetime.now(UTC) + backoff(attempts)])
                move(x)

            ctl.write(fail)
            c.stats[status] += 1
            if permanent and ev["workspace_id"] and not ev["type"].startswith("event."):
                self.emit(ev["workspace_id"], "event.dead_lettered", subject=("event", str(ev["seq"])),
                          data={"consumer": c.name, "event_type": ev["type"], "attempts": attempts},
                          source="system", dedupe_key=f"dead:{c.name}:{ev['id']}")
            return
        if pending is not None:
            pending.append([c.name, ev["id"], ev["workspace_id"], ev["seq"], attempts, int((time.monotonic() - started) * 1000)])
            c.stats["done"] += 1
            return

        def done(x):
            x.execute("INSERT OR REPLACE INTO event_inbox (consumer, event_id, workspace_id, seq, status, attempts, "
                      "last_error, next_attempt_at, duration_ms, updated_at) VALUES (?, ?, ?, ?, 'done', ?, NULL, NULL, ?, now())",
                      [c.name, ev["id"], ev["workspace_id"], ev["seq"], attempts, int((time.monotonic() - started) * 1000)])
            move(x)

        ctl.write(done)
        c.stats["done"] += 1

    def dead_letters(self, actor: Principal, ws: str, limit: int = 100) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "audit.read")
        rows = self.stores.control.query(
            "SELECT i.consumer, i.attempts, i.last_error, i.updated_at, p.* FROM event_inbox i JOIN platform_events p "
            "ON p.id = i.event_id WHERE i.workspace_id = ? AND i.status = 'dead' ORDER BY i.updated_at DESC LIMIT ?",
            [ws, min(max(limit, 1), 500)])
        return [{"consumer": r["consumer"], "attempts": r["attempts"], "error": r["last_error"],
                 "failed_at": str(r["updated_at"]), "event": row_to_event(r)} for r in rows]

    def retry_dead(self, actor: Principal, ws: str, consumer: str, seq: int) -> dict[str, Any]:
        """Put a dead-lettered event back in its consumer's queue (after the cause was fixed)."""
        self.guard.require(actor, ws, "workspace.admin", f"{consumer}:{seq}")
        eid = self.stores.control.scalar("SELECT id FROM platform_events WHERE seq = ? AND workspace_id = ?", [seq, ws])
        n = self.stores.control.scalar("SELECT count(*) FROM event_inbox WHERE consumer = ? AND event_id = ? AND status = 'dead'",
                                       [consumer, eid]) if eid else 0
        if not n:
            raise KeyError(f"{consumer}:{seq}")
        self.stores.control.execute("UPDATE event_inbox SET status = 'retry', attempts = 0, next_attempt_at = now() "
                                    "WHERE consumer = ? AND event_id = ?", [consumer, eid])
        self.guard.audit(actor, ws, "event.retry", f"{consumer}:{seq}", "ok")
        return {"consumer": consumer, "seq": seq, "status": "retry"}

    # -- reading ----------------------------------------------------------------------------

    def since(self, ws: str, after: int = 0, *, types: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Events of one workspace after a cursor, unfiltered by permission (in-process consumers)."""
        rows = self.stores.control.query("SELECT * FROM platform_events WHERE workspace_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                                         [ws, after, min(limit, 1000) * (4 if types else 1)])
        return [e for e in (row_to_event(r) for r in rows) if matches(e["type"], types)][:limit]

    def visible(self, actor: Principal, ws: str, event: dict[str, Any], role: str) -> bool:
        if event.get("workspace_id") != ws:
            return False  # never another workspace's event, whatever the caller's role there
        if RANK[role] >= RANK["admin"]:
            return True
        if event.get("sensitivity") in ("confidential", "restricted"):
            return False  # a confidential dataset's events (even ids and counts) are for its admins
        if event["type"].startswith("task.") or event["type"] == "approval.decided":
            return event["data"].get("created_by") == actor.user_id or event["actor"] == actor.user_id
        if event["type"].startswith("external.") or event["type"].startswith("event."):
            return False
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

    def get(self, actor: Principal, ws: str, seq: int) -> dict[str, Any]:
        """One event with its causal chain (every event sharing its correlation id that this member may see) and how
        each durable consumer processed it."""
        role = self.guard.require(actor, ws, "workspace.read")
        rows = self.stores.control.query("SELECT * FROM platform_events WHERE seq = ? AND workspace_id = ?", [seq, ws])
        if not rows or not self.visible(actor, ws, ev := row_to_event(rows[0]), role):
            raise KeyError(str(seq))
        chain = [row_to_event(r) for r in self.stores.control.query(
            "SELECT * FROM platform_events WHERE workspace_id = ? AND correlation_id = ? ORDER BY seq LIMIT 200",
            [ws, ev["correlation_id"]])]
        processing = [{"consumer": r["consumer"], "status": r["status"], "attempts": r["attempts"],
                       "error": r["last_error"] if RANK[role] >= RANK["admin"] else None,
                       "updated_at": str(r["updated_at"])}
                      for r in self.stores.control.query("SELECT * FROM event_inbox WHERE event_id = ? ORDER BY consumer", [ev["id"]])]
        return {"event": ev, "chain": [e for e in chain if self.visible(actor, ws, e, role)], "processing": processing}

    def prune(self, days: int = 90) -> int:
        n = self.stores.control.scalar("SELECT count(*) FROM platform_events WHERE occurred_at < now() - to_days(CAST(? AS BIGINT))", [days]) or 0
        if n:
            self.stores.control.execute("DELETE FROM event_inbox WHERE event_id IN (SELECT id FROM platform_events "
                                        "WHERE occurred_at < now() - to_days(CAST(? AS BIGINT)))", [days])
            self.stores.control.execute("DELETE FROM platform_events WHERE occurred_at < now() - to_days(CAST(? AS BIGINT))", [days])
        for w in self.stores.control.query("SELECT id FROM workspaces"):
            try:
                self.stores.workspace(w["id"]).execute("DELETE FROM event_outbox WHERE published_at < now() - INTERVAL 7 DAY")
            except Exception:  # noqa: BLE001 - retention is best effort per workspace
                log.exception("outbox prune failed for %s", w["id"])
        return int(n)


__all__ = ["CATALOG", "EMITTED", "SCHEMA", "ChainLimit", "Event", "EventService", "PermanentFailure", "lineage",
           "matches", "types"]
