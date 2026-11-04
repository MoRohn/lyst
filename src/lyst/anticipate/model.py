# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Prepared work as explicit, auditable objects.

An *anticipation* is one thing Lyst expects to be useful: its objective, the signals behind it, the reasons in plain
words, how it scored, the level it needs, what was prepared and how that was verified, the actions it offers, what it
depends on, when it is needed and until when it is valid. Every change appends an event, so the trail from signal to
outcome can be read back.

Status flow::

    candidate -> (preparing: a background investigation runs) -> prepared -> surfaced -> accepted | dismissed | snoozed -> (completed)
         \\-> withdrawn (level lowered)   \\-> expired (past validity)   any open -> candidate again when a dependency
                                                                         changes (revision + 1, event "invalidated")
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

OPEN = ("candidate", "preparing", "prepared", "surfaced", "snoozed")
TERMINAL = ("accepted", "dismissed", "expired", "withdrawn", "completed", "suppressed", "resolved")
JSON_COLS = ("audience", "signals", "reasons", "scores", "prepared", "verification", "actions", "depends", "resources")


def now() -> datetime:
    return datetime.now(UTC)


def fingerprint(depends: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(depends, sort_keys=True, default=str).encode()).hexdigest()[:16]


def row(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    for c in JSON_COLS:
        if isinstance(out.get(c), str):
            out[c] = json.loads(out[c])
    return out


def event(db, anticipation_id: str, name: str, actor: str = "lyst", detail: dict[str, Any] | None = None) -> None:
    db.execute("INSERT INTO anticipation_events (id, anticipation_id, event, actor, detail) VALUES (nextval('anticipation_event_seq'), ?, ?, ?, ?)",
               [anticipation_id, name, actor, json.dumps(detail or {}, default=str)])


def get(db, anticipation_id: str) -> dict[str, Any]:
    rows = db.query("SELECT * FROM anticipations WHERE id = ?", [anticipation_id])
    if not rows:
        raise KeyError(anticipation_id)
    return row(rows[0])


def by_key(db, key: str) -> dict[str, Any] | None:
    rows = db.query("SELECT * FROM anticipations WHERE dedupe_key = ?", [key])
    return row(rows[0]) if rows else None


def upsert(db, cand: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Record a candidate. Returns the stored object and what happened: ``new``, ``invalidated`` (an open object whose
    dependencies changed, re-opened as a candidate with revision + 1), ``updated`` (scores and reasons refreshed) or
    ``closed`` (already decided, left alone so a dismissed suggestion does not come back for the same occurrence)."""
    j = lambda v: json.dumps(v, default=str)  # noqa: E731
    prior = by_key(db, cand["dedupe_key"])
    if prior is None:
        aid = "ant_" + uuid.uuid4().hex[:12]
        db.execute(
            "INSERT INTO anticipations (id, kind, objective, subject_kind, subject_id, audience, signals, reasons, scores, score, "
            "level_required, status, actions, depends, resources, needed_at, valid_until, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?)",
            [aid, cand["kind"], cand["objective"], cand["subject_kind"], cand["subject_id"], j(cand["audience"]), j(cand["signals"]),
             j(cand["reasons"]), j(cand["scores"]), cand["score"], cand["level_required"], j(cand["actions"]), j(cand["depends"]),
             j({}), cand.get("needed_at"), cand.get("valid_until"), cand["dedupe_key"]])
        event(db, aid, "anticipated", detail={"score": cand["score"], "signals": [s["kind"] for s in cand["signals"]]})
        return get(db, aid), "new"
    if prior["status"] in TERMINAL:
        return prior, "closed"
    changed = fingerprint(prior["depends"]) != fingerprint(cand["depends"])
    if changed and prior["status"] != "candidate":
        what = _diff(prior["depends"], cand["depends"])
        db.execute("UPDATE anticipations SET status = 'candidate', verdict = NULL, prepared = NULL, verification = NULL, "
                   "revision = revision + 1, depends = ?, signals = ?, reasons = ?, scores = ?, score = ?, actions = ?, needed_at = ?, "
                   "valid_until = ?, updated_at = now() WHERE id = ?",
                   [j(cand["depends"]), j(cand["signals"]), j(cand["reasons"]), j(cand["scores"]), cand["score"], j(cand["actions"]),
                    cand.get("needed_at"), cand.get("valid_until"), prior["id"]])
        event(db, prior["id"], "invalidated", detail={"changed": what})
        return get(db, prior["id"]), "invalidated"
    db.execute("UPDATE anticipations SET subject_id = ?, depends = ?, signals = ?, reasons = ?, scores = ?, score = ?, needed_at = ?, "
               "valid_until = ?, updated_at = now() WHERE id = ?",
               [cand["subject_id"], j(cand["depends"]), j(cand["signals"]), j(cand["reasons"]), j(cand["scores"]), cand["score"],
                cand.get("needed_at"), cand.get("valid_until"), prior["id"]])
    return get(db, prior["id"]), "updated"


def _diff(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k), b.get(k), f"{path}.{k}" if path else str(k))
        return out
    return [] if a == b else [f"{path}: {a} -> {b}"]


def set_prepared(db, aid: str, *, verdict: str, prepared: dict[str, Any], verification: dict[str, Any],
                 resources: dict[str, Any], reasons: list[str] | None = None) -> None:
    j = lambda v: json.dumps(v, default=str)  # noqa: E731
    db.execute("UPDATE anticipations SET status = 'prepared', verdict = ?, prepared = ?, verification = ?, resources = ?, "
               + ("reasons = ?, " if reasons is not None else "") + "prepared_at = now(), updated_at = now() WHERE id = ?",
               [verdict, j(prepared), j(verification), j(resources)] + ([j(reasons)] if reasons is not None else []) + [aid])
    event(db, aid, "prepared", detail={"verdict": verdict, "checks": len(verification.get("checks", [])),
                                       "ms": resources.get("compute_ms", 0)})


def transition(db, aid: str, status: str, *, actor: str = "lyst", detail: dict[str, Any] | None = None, **cols: Any) -> None:
    sets = ["status = ?", "updated_at = now()"] + [f"{k} = ?" for k in cols]
    db.execute(f"UPDATE anticipations SET {', '.join(sets)} WHERE id = ?", [status, *cols.values(), aid])
    event(db, aid, status, actor, detail)


def events(db, aid: str) -> list[dict[str, Any]]:
    out = db.query("SELECT event, actor, detail, occurred_at FROM anticipation_events WHERE anticipation_id = ? ORDER BY id", [aid])
    for e in out:
        e["detail"] = json.loads(e["detail"]) if isinstance(e["detail"], str) else e["detail"]
    return out
