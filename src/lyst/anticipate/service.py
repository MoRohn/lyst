# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The Anticipative Intelligence Engine.

One cycle per workspace walks Observe -> Contextualize -> Forecast -> Anticipate -> Prioritize -> Prepare -> Verify ->
Surface -> Learn over the anticipators registered in ``self.anticipators``. Each anticipator is a module with
``observe(svc, ws, now)`` (signals from authorized metadata), ``candidates(svc, ws, now, signals)`` (contextualized,
scored candidates with reasons and dependencies) and optionally ``prepare(svc, ws, cand, now, automate=...)``
(bounded, read-only preparation that also verifies itself).

Everything is deterministic and local: no model call, no network request. Cycles run on the worker tick at most every
``LYST_ANTICIPATE_EVERY`` seconds (default 300) and immediately, for invalidation, after a dataset or definition changes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import notices
from ..security import Principal
from . import attention, capability, earlywarning, health, learning, model, nextaction, policy, recurring

log = logging.getLogger("lyst.anticipate")

USER_ACTIONS = ("dismiss", "snooze", "useful", "not_useful", "incorrect")
# pushed to the inbox only when Lyst found something to act on before the need; a ready item stays in the activity center
SURFACE_VERDICTS = ("blocked",)
SURFACE_MIN_SCORE = 40.0


def combine(s: dict[str, float], learned: float = 1.0) -> float:
    """0-100. Value, likelihood, evidence and urgency multiply (any one near zero sinks the item); cost, risk and the
    interruption it would cause subtract. ``learned`` is the kind's observed usefulness relative to the prior."""
    base = s["value"] * s["likelihood"] * (0.5 + 0.5 * s["evidence"]) * (0.4 + 0.6 * s["urgency"]) * learned
    return round(max(100 * base - 10 * s.get("cost", 0) - 15 * s.get("risk", 0) - 5 * s.get("interruption", 0), 0.0), 2)


class AnticipationService:
    def __init__(self, stores, guard, *, services, runtime, semantic, connections=None) -> None:
        self.stores = stores
        self.guard = guard
        self.services = services
        self.runtime = runtime
        self.semantic = semantic
        self.connections = connections
        self.anticipators = [recurring, nextaction, earlywarning, health, capability]
        self.collab = None  # set by the composition root; health foresight files definition fixes as reviews
        self.every = float(os.environ.get("LYST_ANTICIPATE_EVERY", "300"))
        self._last_tick = 0.0
        self._dirty: dict[str, float] = {}   # workspace -> when an event asked for a fresh look
        self._last_cycle: dict[str, float] = {}
        self.min_gap = float(os.environ.get("LYST_ANTICIPATE_MIN_GAP", "30"))
        self._local = threading.local()

    # -- helpers --------------------------------------------------------------------------------------------------------

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    @staticmethod
    def cadence(seconds: int) -> str:
        from ..monitoring import cadence  # noqa: PLC0415

        return cadence(seconds)

    def model_budget_block(self, ws: str) -> str | None:
        try:
            from .. import operations  # noqa: PLC0415 - present once the operations slice is installed
        except ImportError:
            return None
        return operations.model_budget_block(self.stores.control, ws)

    def learned(self, ws: str, kind: str, actions: list[dict[str, Any]] | None = None) -> float:
        """The family's weight from judged outcomes (see learning.py); 1.0 until people have judged it."""
        w = getattr(self._local, "weights", None)
        if w is None or w[0] != ws:
            w = (ws, learning.weights(self._db(ws)))
            self._local.weights = w
        return w[1].get(learning.family(kind, actions), 1.0)

    # -- settings -------------------------------------------------------------------------------------------------------

    def settings(self, actor: Principal, ws: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        s = policy.load(self._db(ws))
        llm = getattr(self.services, "llm", None)
        external = bool(s.allows("investigate") and llm is not None and llm.settings.external and llm.allowed_for(ws))
        return {**s.public(), "usage_today": policy.usage(self._db(ws)), "external_services": external,
                "methods": ("detection, preparation and ranking are deterministic and run on this installation; "
                            + ("level-3 investigations run the analyst, which may call the workspace's external model for wording"
                               if external else "no model is called and nothing leaves this installation"))}

    def set_settings(self, actor: Principal, ws: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.admin", "anticipation")
        db = self._db(ws)
        before = policy.load(db)
        after = policy.save(db, actor.user_id, before, changes)
        withdrawn = self._withdraw_above(ws, after.level, actor.user_id) if after.level < before.level else 0
        self.guard.audit(actor, ws, "anticipation.settings", ws, "ok",
                         {"level": [before.level, after.level], "withdrawn": withdrawn, **{k: v for k, v in changes.items() if k != "level"}})
        return {**self.settings(actor, ws), "withdrawn": withdrawn}

    def _withdraw_above(self, ws: str, level: int, actor_id: str) -> int:
        """Lowering the level stops what the new level no longer allows: open items that need more are withdrawn and the
        background tasks they queued (not yet started) are cancelled. Finished preparation stays readable."""
        db = self._db(ws)
        rows = db.query(f"SELECT id, task_id, level_required, status FROM anticipations WHERE status IN {model.OPEN}")
        n = 0
        for r in rows:
            if (level == 0 or r["level_required"] > level or (level < policy.OPERATIONS["prepare"] and r["status"] == "candidate")
                    or (level < policy.OPERATIONS["investigate"] and r["status"] == "preparing")):
                model.transition(db, r["id"], "withdrawn", actor=actor_id, detail={"reason": f"anticipation level set to {level}"})
                n += 1
            if r["task_id"] and (level == 0 or level < policy.OPERATIONS["investigate"]):
                self.stores.control.execute("UPDATE tasks SET status = 'cancelled', updated_at = now() WHERE id = ? AND status = 'queued'",
                                            [r["task_id"]])
        return n

    # -- the cycle ------------------------------------------------------------------------------------------------------

    # events that can change what is worth anticipating; anything else is ignored
    WATCH = ("dataset.version_created", "task.completed", "task.failed", "approval.decided", "metric.defined", "metric.deprecated",
             "contract.checked", "connection.synced", "connection.created", "connection.deleted", "member.added", "member.removed",
             "schedule.created", "assignment.created", "assignment.completed", "skill.registered", "monitor.created")

    def on_event(self, event: dict[str, Any]) -> None:
        """Unified event model subscriber: mark the workspace for an early cycle. Cheap and never raises; the cycle itself runs
        on the worker tick, at most once per ``min_gap`` seconds per workspace, so a burst of events costs one cycle."""
        if event.get("type") in self.WATCH and event.get("workspace_id"):
            self._dirty.setdefault(event["workspace_id"], time.monotonic())

    def tick_all(self, now: datetime | None = None, *, force: bool = False) -> int:
        """Worker-tick entry. Workspaces an event marked are cycled within ``min_gap`` seconds; every workspace is cycled at
        least once per ``every`` seconds as a fallback, so nothing depends on continuous polling."""
        mono = time.monotonic()
        full = force or mono - self._last_tick >= self.every
        due = [ws for ws in list(self._dirty) if mono - self._last_cycle.get(ws, -1e9) >= self.min_gap]
        if not full and not due:
            return 0
        prune = full and mono - getattr(self, "_last_prune", -1e9) > 86400
        if prune:
            self._last_prune = mono
        if full:
            self._last_tick = mono
            targets = [w["id"] for w in self.stores.control.query("SELECT id FROM workspaces")]
        else:
            targets = due
        n = 0
        for ws in targets:
            self._dirty.pop(ws, None)
            self._last_cycle[ws] = mono
            try:
                if prune:
                    learning.prune(self._db(ws), now)
                n += self.cycle(ws, now)["anticipated"]
            except Exception:  # noqa: BLE001 - one workspace never stops the others
                log.exception("anticipation cycle failed for %s", ws)
        return n

    def cycle(self, ws: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        db = self._db(ws)
        s = policy.load(db)
        out = {"level": s.level, "observed": 0, "anticipated": 0, "invalidated": 0, "prepared": 0, "surfaced": 0, "expired": 0,
               "refused": []}
        if s.level == 0 or getattr(self._local, "busy", False):
            return out
        self._local.busy = True
        try:
            out["expired"] = self._expire(db, now)
            self._local.weights = None
            irrelevant = learning.irrelevant(db, now)
            self._complete(db, now)                                         # learn: did accepted work finish?
            for mod in self.anticipators:
                signals = mod.observe(self, ws, now)                       # observe
                out["observed"] += len(signals)
                produced: set[str] = set()
                for cand in mod.candidates(self, ws, now, signals):       # contextualize, forecast, anticipate
                    produced.add(cand["dedupe_key"])
                    if cand["level_required"] > s.level:
                        continue
                    if (learning.family(cand["kind"], cand["actions"]), cand["subject_id"]) in irrelevant \
                            and model.by_key(db, cand["dedupe_key"]) is None:
                        continue                                            # dismissed twice lately: not again for now
                    cand["score"] = combine(cand["scores"], self.learned(ws, cand["kind"], cand["actions"]))   # prioritize
                    ctx = cand.pop("_ctx", None)
                    stored, what = model.upsert(db, cand)
                    if what == "new":
                        out["anticipated"] += 1
                    elif what == "invalidated":
                        out["invalidated"] += 1
                    if stored["status"] != "candidate" or not hasattr(mod, "prepare") or not s.allows("prepare"):
                        continue
                    reason = policy.admit(db, s, "prepare", now)
                    if reason:
                        out["refused"].append(reason)
                        continue
                    cand["_ctx"] = ctx
                    prep = mod.prepare(self, ws, cand, now, automate=s.allows("automate"))   # prepare + verify
                    policy.charge(db, "prepare", prep["resources"]["compute_ms"], now)
                    self._apply(ws, s, mod, stored["id"], cand["scores"], prep, now, out)
                if getattr(mod, "RESOLVES", False):                       # the condition went away: close it
                    for r in db.query(f"SELECT id, dedupe_key FROM anticipations WHERE status IN {model.OPEN} AND kind IN "
                                      f"({', '.join('?' for _ in mod.KINDS)})", list(mod.KINDS)):
                        if r["dedupe_key"] not in produced:
                            model.transition(db, r["id"], "resolved", detail={"reason": "the signal is no longer present"})
                            out["resolved"] = out.get("resolved", 0) + 1
            for mod in self.anticipators:                                   # investigations that were running
                if not hasattr(mod, "resume"):
                    continue
                for r in db.query(f"SELECT * FROM anticipations WHERE status = 'preparing' AND kind IN ({', '.join('?' for _ in mod.KINDS)})",
                                  list(mod.KINDS)):
                    a = model.row(r)
                    prep = mod.resume(self, ws, a, now)
                    if prep is not None:
                        self._apply(ws, s, mod, a["id"], a["scores"], prep, now, out)
            out["surfaced"] += self._surface_all(ws, s, now)                   # surface, most valuable first
        finally:
            self._local.busy = False
        return out

    def _apply(self, ws: str, s: policy.Settings, mod, aid: str, scores: dict[str, float], prep: dict[str, Any], now: datetime,
               out: dict[str, Any]) -> None:
        """Record what preparation produced: drop (say nothing), pending (a background task is verifying it), or a verdict,
        re-scored with what preparation learned, and pushed when the anticipator says that verdict is worth it."""
        db = self._db(ws)
        if prep["verdict"] == "drop":   # verification showed it would not help: say nothing
            model.transition(db, aid, "suppressed", detail={"reason": prep["reasons"][-1], "checks": prep["verification"]["checks"]})
            return
        if prep["verdict"] == "pending":
            db.execute("UPDATE anticipations SET prepared = ?, verification = ?, resources = ? WHERE id = ?",
                       [json.dumps(prep["prepared"], default=str), json.dumps(prep["verification"], default=str),
                        json.dumps(prep["resources"]), aid])
            model.transition(db, aid, "preparing", detail={"task_id": prep["task_id"]}, task_id=prep["task_id"])
            return
        scores = {**scores, **prep.get("scores", {})}   # what preparation learned about value and evidence
        a = model.get(db, aid)
        score = combine(scores, self.learned(ws, a["kind"], a["actions"]))
        model.set_prepared(db, aid, verdict=prep["verdict"], prepared=prep["prepared"], verification=prep["verification"],
                           resources=prep["resources"], reasons=prep["reasons"])
        if prep.get("actions") is not None:   # preparation may add an action it validated (a definition fix)
            db.execute("UPDATE anticipations SET actions = ? WHERE id = ?", [json.dumps(prep["actions"]), aid])
        db.execute("UPDATE anticipations SET scores = ?, score = ? WHERE id = ?", [json.dumps(scores), score, aid])
        out["prepared"] += 1

    # -- services the anticipators use ----------------------------------------------------------------------------------

    def level(self, ws: str) -> int:
        return policy.load(self._db(ws)).level

    def admit(self, ws: str, operation: str, now: datetime | None = None) -> str | None:
        """Admit and charge one ``operation`` against today's budget; the reason when it does not fit."""
        db = self._db(ws)
        reason = policy.admit(db, policy.load(db), operation, now)
        if reason is None:
            policy.charge(db, operation, 0, now)
        return reason

    def submit_background(self, actor: Principal, ws: str, question: str, *, deadline_at: datetime | None = None) -> str:
        """A read-only investigation at maintenance priority: never takes the last worker, and the supervisor fails it cleanly
        when it cannot start before ``deadline_at``."""
        try:
            from .. import operations  # noqa: PLC0415

            prio = operations.priority_for("maintenance")
        except (ImportError, AttributeError, KeyError):
            prio = None
        return self.runtime.submit(actor, ws, question, kind="investigation", autonomy="autonomous", priority=prio,
                                   deadline_at=deadline_at)

    def _complete(self, db, now: datetime) -> None:
        for r in db.query("SELECT id, task_id FROM anticipations WHERE status = 'accepted' AND task_id IS NOT NULL"):
            st = self.stores.control.scalar("SELECT status FROM tasks WHERE id = ?", [r["task_id"]])
            if st == "done":
                model.transition(db, r["id"], "completed", detail={"task_id": r["task_id"]}, outcome="completed")

    def quality(self, actor: Principal, ws: str, *, days: int = 30) -> dict[str, Any]:
        """Anticipation quality from recorded outcomes: relevance, timeliness, accuracy, efficiency, attention, governance."""
        self.guard.require(actor, ws, "audit.read", "anticipation")
        if not 1 <= days <= 400:
            raise ValueError("days must be between 1 and 400")
        return {**learning.quality(self.stores, ws, days), "weights": learning.weights(self._db(ws))}

    def _expire(self, db, now: datetime) -> int:
        rows = db.query(f"SELECT id FROM anticipations WHERE status IN {model.OPEN} AND valid_until IS NOT NULL AND valid_until < ?", [now])
        for r in rows:
            model.transition(db, r["id"], "expired", detail={"at": now.isoformat()})
        return len(rows)

    def _surface_all(self, ws: str, s: policy.Settings, now: datetime) -> int:
        db = self._db(ws)
        eligible = []
        for mod in self.anticipators:
            verdicts = getattr(mod, "SURFACE", SURFACE_VERDICTS)
            rows = db.query(f"SELECT * FROM anticipations WHERE status = 'prepared' AND surfaced_at IS NULL AND score >= ? "
                            f"AND verdict IN ({', '.join('?' for _ in verdicts)}) AND kind IN ({', '.join('?' for _ in mod.KINDS)})",
                            [SURFACE_MIN_SCORE, *verdicts, *mod.KINDS])
            eligible += [(model.row(r), mod) for r in rows]
        n = 0
        for a, mod in sorted(eligible, key=lambda x: -x[0]["score"]):
            n += self._surface(ws, s, a, now, mod)
        return n

    def _surface(self, ws: str, s: policy.Settings, a: dict[str, Any], now: datetime, mod=None) -> int:
        """Push one item to the people whose attention preferences allow it now. Quiet hours and daily caps hold it for its
        audience (retried next cycle); the other reasons leave it in the activity center only."""
        db = self._db(ws)
        admins = {r["user_id"] for r in self.stores.control.query(
            "SELECT user_id FROM memberships WHERE workspace_id = ? AND role IN ('owner', 'admin')", [ws])}
        people = set(a["audience"]) | admins
        decisions = {u: attention.push_decision(db, u, a, now) for u in people}
        allowed = {u for u, d in decisions.items() if d is None}
        held = [u for u in people if decisions[u] in attention.HOLD]
        # the person it is for comes first: while they are in quiet hours or at their cap, nobody is told yet
        if any(decisions.get(u) in attention.HOLD for u in a["audience"]) or (not allowed and held):
            detail = {"held_for": {u: decisions[u] for u in held}}
            last = db.query("SELECT event, detail FROM anticipation_events WHERE anticipation_id = ? ORDER BY id DESC LIMIT 1", [a["id"]])
            if not last or last[0]["event"] != "held" or json.loads(last[0]["detail"]) != detail:
                model.event(db, a["id"], "held", detail=detail)
            return 0
        if not allowed:
            db.execute("UPDATE anticipations SET surfaced_at = ? WHERE id = ?", [now, a["id"]])
            model.event(db, a["id"], "not_pushed", detail={"reasons": decisions})
            return 0
        reason = policy.admit(db, s, "surface", now)
        if reason:
            model.event(db, a["id"], "held", detail={"reason": reason})
            return 0
        summary = mod.summary(a) if hasattr(mod, "summary") else " ".join(a["reasons"][:3])
        fid = notices.raise_notice(self.stores, ws, source=f"anticipate:{a['id']}", kind=f"anticipation.{a['kind']}",
                                   label=a["objective"], summary=summary[:900], metric=f"{a['subject_kind']}:{a['subject_id']}",
                                   priority=round(a["score"] / 10, 2), dedupe_key=f"anticipate|{a['id']}|{a['revision']}",
                                   recipients=allowed, exclude=people - allowed)
        policy.charge(db, "surface", 0, now)
        model.transition(db, a["id"], "surfaced", detail={"finding_id": fid, "to": sorted(allowed),
                                                          "not_to": {u: d for u, d in decisions.items() if d}},
                         finding_id=fid, surfaced_at=now)
        return 1

    # -- attention ------------------------------------------------------------------------------------------------------

    def attention(self, actor: Principal, ws: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        return {**attention.prefs(self._db(ws), actor.user_id), "kinds": list(attention.KINDS)}

    def set_attention(self, actor: Principal, ws: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        p = attention.save(self._db(ws), actor.user_id, changes)
        self.guard.audit(actor, ws, "anticipation.attention", actor.user_id, "ok", {k: v for k, v in changes.items()})
        return {**p, "kinds": list(attention.KINDS)}

    # -- hooks ----------------------------------------------------------------------------------------------------------

    def after_ingest(self, actor: Principal, ws: str, result: dict[str, Any]) -> None:
        self.cycle(ws)

    def after_definition(self, actor: Principal, ws: str, name: str) -> None:
        self.cycle(ws)

    # -- reading --------------------------------------------------------------------------------------------------------

    def _visible(self, actor: Principal, ws: str, a: dict[str, Any]) -> bool:
        return actor.user_id in a["audience"] or self.guard.can(actor, ws, "workspace.admin")

    def list(self, actor: Principal, ws: str, *, status: str = "open", kind: str | None = None, limit: int = 50,
             now: datetime | None = None, subject_id: str | None = None) -> list[dict[str, Any]]:
        """The activity center: prepared work, warnings and suggestions for this person, highest value first."""
        self.guard.require(actor, ws, "workspace.read")
        now = now or datetime.now(UTC)
        statuses = model.OPEN if status == "open" else (status,)
        sql = f"SELECT * FROM anticipations WHERE status IN ({', '.join('?' for _ in statuses)})"
        params: list[Any] = list(statuses)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if subject_id:
            sql += " AND subject_id = ?"
            params.append(subject_id)
        rows = [model.row(r) for r in self._db(ws).query(sql + " ORDER BY score DESC, created_at DESC", params)]
        mine = attention.prefs(self._db(ws), actor.user_id)
        out = []
        for a in rows:
            if not self._visible(actor, ws, a) or not attention.shown(mine, a):
                continue
            if a["status"] == "snoozed" and a["snoozed_until"] and _aware(a["snoozed_until"]) > now:
                continue
            out.append(_public(a))
        return out[:limit]

    def get(self, actor: Principal, ws: str, aid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        a = model.get(self._db(ws), aid)
        if not self._visible(actor, ws, a):
            raise KeyError(aid)
        return {**_public(a), "verification": a["verification"], "prepared": a["prepared"], "depends": a["depends"],
                "resources": a["resources"], "events": model.events(self._db(ws), aid)}

    def explain(self, actor: Principal, ws: str, aid: str) -> dict[str, Any]:
        """Why Lyst surfaced this: the reasons, each grounded in a recorded signal, check or dependency, and how it scored."""
        a = self.get(actor, ws, aid)
        checks = (a["verification"] or {}).get("checks", [])
        detail = list(a["reasons"]) + [f"{c['detail'][0].upper()}{c['detail'][1:]} ({c['status']})." for c in checks if c.get("detail")]
        return {"id": aid, "short": a["reasons"][0] if a["reasons"] else a["objective"], "detail": detail,
                "objective": a["objective"], "why": a["reasons"], "signals": a["signals"],
                "checks": (a["verification"] or {}).get("checks", []), "score": a["score"], "scores": a["scores"],
                "depends_on": a["depends"], "level_required": policy.LEVELS[a["level_required"]],
                "methods": "metadata and deterministic checks; no model was used"}

    # -- acting ---------------------------------------------------------------------------------------------------------

    def act(self, actor: Principal, ws: str, aid: str, action: str, *, until: datetime | None = None, note: str = "",
            now: datetime | None = None) -> dict[str, Any]:
        """A person's response. Offered actions run as that person through the normal guards; feedback is recorded for
        learning. Inferred interest is never authorization: an action runs only because this person asked for it now."""
        db = self._db(ws)
        a = model.get(db, aid)
        if not self._visible(actor, ws, a):
            raise KeyError(aid)
        now = now or datetime.now(UTC)
        offered = {x["id"]: x for x in a["actions"]}
        if action in offered:
            if a["status"] not in model.OPEN:
                raise ValueError(f"this suggestion is {a['status']}")
            done = self._run(actor, ws, a, action)
            task_id = done.get("task_id")
            model.transition(db, aid, "accepted", actor=actor.user_id, detail={"action": action, **done, "note": note},
                             outcome="accepted", decided_by=actor.user_id, decided_at=now, task_id=task_id)
            self.guard.audit(actor, ws, "anticipation.accept", aid, "ok", {"action": action, **done})
            return {"id": aid, "status": "accepted", **done}
        if action == "dismiss":
            self.guard.require(actor, ws, "workspace.read")
            model.transition(db, aid, "dismissed", actor=actor.user_id, detail={"note": note}, outcome="dismissed",
                             decided_by=actor.user_id, decided_at=now)
            return {"id": aid, "status": "dismissed"}
        if action == "snooze":
            until = until or now + timedelta(days=1)
            if until <= now:
                raise ValueError("snooze until a time in the future")
            model.transition(db, aid, "snoozed", actor=actor.user_id, detail={"until": until.isoformat()}, snoozed_until=until)
            return {"id": aid, "status": "snoozed", "until": until.isoformat()}
        if action in ("useful", "not_useful", "incorrect"):
            model.event(db, aid, f"feedback:{action}", actor.user_id, {"note": note})
            if a["status"] in ("accepted", "completed"):
                db.execute("UPDATE anticipations SET outcome = ? WHERE id = ?", [action, aid])
            return {"id": aid, "status": a["status"], "feedback": action}
        raise ValueError(f"unknown action {action}; this item offers {', '.join(offered) or 'no actions'} and {', '.join(USER_ACTIONS)}")

    def _run(self, actor: Principal, ws: str, a: dict[str, Any], action: str) -> dict[str, Any]:
        for mod in self.anticipators:
            if a["kind"] in getattr(mod, "KINDS", ()):
                return mod.run(self, actor, ws, a, action)
        raise ValueError(f"{action} is not available for {a['kind']}")

def _aware(ts):
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=UTC)


def _public(a: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "kind", "objective", "subject_kind", "subject_id", "status", "verdict", "score", "scores", "reasons", "signals",
            "actions", "level_required", "needed_at", "valid_until", "revision", "finding_id", "task_id", "snoozed_until",
            "outcome", "created_at", "prepared_at", "surfaced_at")
    out = {k: a.get(k) for k in keep}
    out["actions"] = list(a["actions"]) + [{"id": x, "label": x.replace("_", " ").capitalize()} for x in ("dismiss", "snooze")]
    return out
