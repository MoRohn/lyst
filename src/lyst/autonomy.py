# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Event rules and autonomous activities: what a meaningful event should cause, decided by typed, versioned rules
and carried out as one observable lifecycle through the services Lyst already has.

Rules (``RuleSpec``)
    Event-condition-action, in a constrained representation: never Python and never an expression string. A rule
    names the event types it listens to (``on``), an optional condition tree over named envelope fields (``when``:
    comparisons combined with ``all``, ``any``, ``not``; at most 4 levels and 20 comparisons), an optional
    correlation (``correlate``: at least N events of a type within a window, optionally on the same subject),
    repeat suppression (``suppress_seconds`` per subject), a daily cap, an optional business calendar, a priority,
    an autonomy ceiling and one action. Every save is a new version; a rule starts as a draft, can be dry-run against
    the workspace's recent events with no side effects, and is activated by an admin. A rule whose action changes
    something (``retry_task``, ``propose``) or that runs at ``automate`` needs an approval before it is active.

Activities
    Each match becomes one activity, identified by (rule, version, event), so a redelivered event never creates a
    second one. Its stages follow the lifecycle Detect, Interpret, Qualify, Plan, Authorize, Execute, Verify,
    Deliver, Monitor, each recorded with what was decided and why from stored records, never from what a model says.

Autonomy levels (an upper limit, never an authorization)
    ``observe`` records the evaluation only; ``suggest`` creates a suggested activity a person can run;
    ``prepare`` and ``investigate`` run read-only work (notices, impact reviews, read-only analyses);
    ``automate`` may also run approved changing work (bounded task retries). ``propose`` always waits for a person's
    approval, at every level. The rule's owner's permissions are re-checked when the activity executes, and a
    confidential event is acted on only for an owner who may see it.

Actions
    ``notify`` (an inbox notice to the workspace's owners and the rule owner), ``investigate`` (a read-only analysis
    task whose verified briefing is kept as an artifact), ``impact_review`` (the dependents of a dataset or metric,
    from the catalog's impact analysis, with each broken asset's repair proposal), ``retry_task`` (a bounded retry of
    a failed task whose failure is classified recoverable; the task contract's retry cap still applies), and
    ``propose`` (an approval request for an asset repair or an analysis; nothing runs until it is approved).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from .events import matches
from .notices import raise_notice
from .security import RANK, AccessDenied, Principal

log = logging.getLogger("lyst.autonomy")

LEVELS = ("observe", "suggest", "prepare", "investigate", "automate")
STAGES = ("detect", "interpret", "qualify", "plan", "authorize", "execute", "verify", "deliver", "monitor")
# action -> (risk, minimum autonomy level that runs it without a person, permission its owner needs)
ACTIONS: dict[str, tuple[str, str, str]] = {
    "notify": ("low", "prepare", "workspace.read"),
    "impact_review": ("low", "prepare", "data.read"),
    "investigate": ("low", "investigate", "task.create"),
    "retry_task": ("medium", "automate", "task.control"),
    "propose": ("high", "suggest", "task.control"),
}
FIELD = re.compile(r"^(type|source|sensitivity|actor|depth|subject\.kind|subject\.id|data\.[a-z][a-z0-9_]{0,39})$")
PLACEHOLDER = re.compile(r"\{(subject\.id|subject\.kind|type|data\.[a-z][a-z0-9_]{0,39})\}")
RECOVERABLE = ("transient", "model", "timeout")  # failure classes a bounded retry may help
Scalar = str | int | float | bool | None


# -- the rule representation ----------------------------------------------------------------------


class Compare(BaseModel):
    field: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "exists", "missing", "pct_change_gte"]
    value: Scalar | list[Scalar] = None
    other: str | None = Field(None, description="pct_change_gte: the field holding the baseline")

    @field_validator("field", "other")
    @classmethod
    def _field(cls, v: str | None) -> str | None:
        if v is not None and not FIELD.match(v):
            raise ValueError(f"unknown field {v!r}: use type, source, sensitivity, actor, depth, subject.kind, subject.id or data.<key>")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Compare:
        if self.op in ("in", "not_in") and not (isinstance(self.value, list) and len(self.value) <= 50):
            raise ValueError(f"{self.op} takes a list of at most 50 values")
        if self.op in ("gt", "gte", "lt", "lte", "pct_change_gte") and not isinstance(self.value, int | float):
            raise ValueError(f"{self.op} takes a number")
        if self.op == "pct_change_gte" and not self.other:
            raise ValueError("pct_change_gte needs other: the baseline field")
        return self


class All(BaseModel):
    all: list[Condition] = Field(min_length=1, max_length=20)


class Any_(BaseModel):
    any: list[Condition] = Field(min_length=1, max_length=20)


class Not(BaseModel):
    not_: Condition = Field(alias="not")


Condition = Annotated[Compare | All | Any_ | Not, Field(union_mode="left_to_right")]
for _m in (All, Any_, Not):
    _m.model_rebuild()


class Correlate(BaseModel):
    type: str = Field(min_length=1, max_length=120)
    within_seconds: int = Field(ge=60, le=7 * 86400)
    min_count: int = Field(ge=1, le=100)
    same_subject: bool = True


class Calendar(BaseModel):
    weekdays: list[int] | None = Field(None, description="0 = Monday")
    hours: tuple[int, int] | None = Field(None, description="[start, end) local hours")
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v


class Action(BaseModel):
    kind: Literal["notify", "impact_review", "investigate", "retry_task", "propose"]
    question: str | None = Field(None, max_length=400, description="investigate / propose: placeholders {subject.id} {data.x}")
    summary: str | None = Field(None, max_length=300)
    proposal: Literal["asset_repair", "investigate"] | None = None

    @model_validator(mode="after")
    def _needs(self) -> Action:
        if self.kind == "investigate" and not self.question:
            raise ValueError("an investigate action needs a question")
        if self.kind == "propose" and not self.proposal:
            raise ValueError("a propose action names what it proposes: asset_repair or investigate")
        if self.kind == "propose" and self.proposal == "investigate" and not self.question:
            raise ValueError("proposing an investigation needs its question")
        return self


class RuleSpec(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    description: str = Field("", max_length=600)
    on: list[str] = Field(min_length=1, max_length=10)
    when: Condition | None = None
    correlate: Correlate | None = None
    suppress_seconds: int = Field(3600, ge=0, le=30 * 86400)
    max_per_day: int = Field(20, ge=1, le=500)
    calendar: Calendar | None = None
    priority: int = Field(5, ge=1, le=9)
    autonomy: Literal["observe", "suggest", "prepare", "investigate", "automate"] = "suggest"
    action: Action

    @field_validator("on")
    @classmethod
    def _on(cls, v: list[str]) -> list[str]:
        for p in v:
            if not re.match(r"^[a-z*][a-z0-9_.*]{0,119}$", p) or p.startswith("event."):
                raise ValueError(f"invalid event pattern {p!r}")
        return v

    @model_validator(mode="after")
    def _bounded(self) -> RuleSpec:
        leaves = _count(self.when) if self.when is not None else (0, 0)
        if leaves[0] > 20 or leaves[1] > 4:
            raise ValueError("a condition may hold at most 20 comparisons, nested at most 4 levels")
        return self


def _count(c: Any, depth: int = 1) -> tuple[int, int]:
    if isinstance(c, Compare):
        return 1, depth
    kids = c.all if isinstance(c, All) else c.any if isinstance(c, Any_) else [c.not_]
    sub = [_count(k, depth + 1) for k in kids]
    return sum(s[0] for s in sub), max(s[1] for s in sub)


def risk_of(spec: RuleSpec) -> str:
    risk = ACTIONS[spec.action.kind][0]
    return "high" if spec.autonomy == "automate" and risk == "low" and spec.action.kind == "investigate" else risk


def needs_approval(spec: RuleSpec) -> bool:
    return risk_of(spec) != "low" or spec.autonomy == "automate"


# -- evaluation (pure: no side effects) -------------------------------------------------------------


def _get(ev: dict[str, Any], field: str) -> Any:
    if field.startswith("data."):
        return (ev.get("data") or {}).get(field[5:])
    if field.startswith("subject."):
        return (ev.get("subject") or {}).get(field[8:])
    return ev.get(field)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def evaluate(c: Any, ev: dict[str, Any]) -> tuple[bool, str]:
    """Whether condition ``c`` holds for event ``ev``, and why, in words."""
    if isinstance(c, All):
        res = [evaluate(k, ev) for k in c.all]
        return all(r[0] for r in res), " and ".join(r[1] for r in res)
    if isinstance(c, Any_):
        res = [evaluate(k, ev) for k in c.any]
        hit = [r for r in res if r[0]]
        return bool(hit), (hit[0][1] if hit else " or ".join(r[1] for r in res))
    if isinstance(c, Not):
        ok, why = evaluate(c.not_, ev)
        return not ok, f"not ({why})"
    v = _get(ev, c.field)
    op, want = c.op, c.value
    if op == "exists":
        ok = v is not None
    elif op == "missing":
        ok = v is None
    elif op == "eq":
        ok = v == want
    elif op == "ne":
        ok = v != want
    elif op == "in":
        ok = v in want
    elif op == "not_in":
        ok = v not in want
    elif op == "contains":
        ok = isinstance(v, list | str) and want in v
    elif op == "pct_change_gte":
        base = _num(_get(ev, c.other))
        cur = _num(v)
        ok = cur is not None and base not in (None, 0.0) and abs(cur - base) / abs(base) * 100 >= float(want)
        return ok, f"{c.field} changed {((cur - base) / abs(base) * 100):+.1f}% from {c.other}" if ok else \
            f"{c.field} did not change by {want}% from {c.other}"
    else:
        n, w = _num(v), _num(want)
        ok = n is not None and w is not None and {"gt": n > w, "gte": n >= w, "lt": n < w, "lte": n <= w}[op]
    return ok, f"{c.field} {op.replace('_', ' ')} {want!r}" + ("" if ok else f" (was {v!r})")


def fill(template: str, ev: dict[str, Any]) -> str:
    """Substitute the allowed placeholders; anything else stays literal text."""
    return PLACEHOLDER.sub(lambda m: str(_get(ev, m.group(1)) if _get(ev, m.group(1)) is not None else "")[:120], template)


def _calendar_ok(cal: Calendar | None, at: datetime) -> bool:
    if cal is None:
        return True
    local = at.astimezone(ZoneInfo(cal.timezone))
    if cal.weekdays is not None and local.weekday() not in cal.weekdays:
        return False
    return not (cal.hours and not cal.hours[0] <= local.hour < cal.hours[1])


def _at(ev: dict[str, Any]) -> datetime:
    try:
        t = datetime.fromisoformat(str(ev["occurred_at"]))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except ValueError:
        return datetime.now(UTC)


# -- the service ------------------------------------------------------------------------------------


class AutonomyService:
    CONSUMER = "autonomy.rules"
    FOLLOW = "autonomy.follow"

    def __init__(self, lyst) -> None:
        self.lyst = lyst
        self.stores = lyst.stores
        self.guard = lyst.guard

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    def attach(self) -> None:
        ev = self.lyst.events
        ev.consume(self.CONSUMER, self.on_event, types=None, max_attempts=5)
        ev.consume(self.FOLLOW, self.on_follow, types=["task.*", "approval.decided"], max_attempts=5)

    # -- rule management ------------------------------------------------------------------------

    def _rule(self, ws: str, rid: str) -> dict[str, Any]:
        rows = self._db(ws).query("SELECT r.*, v.spec FROM event_rules r JOIN event_rule_versions v ON v.rule_id = r.id "
                                  "AND v.version = r.current_version WHERE r.id = ?", [rid])
        if not rows:
            raise KeyError(rid)
        r = dict(rows[0])
        r["spec"] = json.loads(r["spec"])
        r["risk"] = risk_of(RuleSpec.model_validate(r["spec"]))
        return r

    def rules(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        return [self._rule(ws, r["id"]) for r in self._db(ws).query("SELECT id FROM event_rules WHERE status <> 'retired' ORDER BY name")]

    def save(self, actor: Principal, ws: str, spec: dict[str, Any], rule_id: str | None = None) -> dict[str, Any]:
        """Create a rule (a draft) or a new version of one. A new version of an active rule returns it to draft."""
        self.guard.require(actor, ws, "workspace.admin", rule_id)
        s = RuleSpec.model_validate(spec)
        db = self._db(ws)
        rid = rule_id or "rule_" + uuid.uuid4().hex[:10]

        def tx(c):
            cur = c.execute("SELECT current_version FROM event_rules WHERE id = ?", [rid]).fetchone()
            if rule_id and not cur:
                raise KeyError(rule_id)
            version = (cur[0] + 1) if cur else 1
            c.execute("INSERT INTO event_rule_versions (rule_id, version, spec, created_by) VALUES (?, ?, ?, ?)",
                      [rid, version, s.model_dump_json(by_alias=True), actor.user_id])
            if cur:
                c.execute("UPDATE event_rules SET name = ?, current_version = ?, status = 'draft', approval_id = NULL, "
                          "updated_at = now() WHERE id = ?", [s.name, version, rid])
            else:
                c.execute("INSERT INTO event_rules (id, name, current_version, status, owner) VALUES (?, ?, ?, 'draft', ?)",
                          [rid, s.name, version, actor.user_id])
            return version

        version = db.write(tx)
        self.guard.audit(actor, ws, "rule.save", rid, "ok", {"version": version, "action": s.action.kind, "autonomy": s.autonomy})
        return self._rule(ws, rid)

    def activate(self, actor: Principal, ws: str, rid: str) -> dict[str, Any]:
        """Turn a rule on. A low-risk rule is active at once; one that changes something waits for an approval."""
        self.guard.require(actor, ws, "workspace.admin", rid)
        r = self._rule(ws, rid)
        if r["status"] == "active":
            return r
        spec = RuleSpec.model_validate(r["spec"])
        db = self._db(ws)
        if needs_approval(spec):
            if r["status"] == "pending_approval":
                return r
            aid = self.lyst.runtime.request_approval(ws, actor.user_id, "rule.activate",
                                                     f"Activate rule \"{spec.name}\" v{r['current_version']} ({spec.action.kind}, {spec.autonomy})",
                                                     {"rule_id": rid, "version": r["current_version"]})
            db.execute("UPDATE event_rules SET status = 'pending_approval', approval_id = ?, updated_at = now() WHERE id = ?", [aid, rid])
        else:
            db.execute("UPDATE event_rules SET status = 'active', owner = ?, activated_at = now(), updated_at = now() WHERE id = ?",
                       [actor.user_id, rid])
        self.guard.audit(actor, ws, "rule.activate", rid, "ok", {"version": r["current_version"]})
        return self._rule(ws, rid)

    def deactivate(self, actor: Principal, ws: str, rid: str, *, retire: bool = False) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.admin", rid)
        self._rule(ws, rid)
        self._db(ws).execute("UPDATE event_rules SET status = ?, updated_at = now() WHERE id = ?", ["retired" if retire else "draft", rid])
        self.guard.audit(actor, ws, "rule.retire" if retire else "rule.deactivate", rid, "ok")
        return self._rule(ws, rid)

    def on_decided(self, actor: Principal, ws: str, payload: dict[str, Any], approve: bool, action: str) -> None:
        """Called by the runtime when a person decides a ``rule.activate`` or ``activity.execute`` approval."""
        db = self._db(ws)
        if action == "rule.activate":
            r = self._rule(ws, payload["rule_id"])
            if r["status"] != "pending_approval" or r["current_version"] != payload["version"]:
                return  # the rule changed after the request: that approval no longer covers it
            db.execute("UPDATE event_rules SET status = ?, activated_at = CASE WHEN ? THEN now() END, updated_at = now() WHERE id = ?",
                       ["active" if approve else "draft", approve, r["id"]])
        elif action == "activity.execute":
            act = self._activity(ws, payload["activity_id"])
            if act["status"] != "awaiting_approval":
                return
            if not approve:
                self._stage(ws, act["id"], "authorize", "rejected", f"{actor.email or actor.user_id} rejected it")
                self._set(ws, act["id"], status="cancelled")
                return
            self._stage(ws, act["id"], "authorize", "approved", f"approved by {actor.email or actor.user_id}")
            self._execute(ws, act["id"], approved_by=actor)

    # -- dry run ------------------------------------------------------------------------------

    def simulate(self, actor: Principal, ws: str, spec: dict[str, Any], *, events: list[dict[str, Any]] | None = None,
                 last: int = 500) -> dict[str, Any]:
        """What a rule would have done over the workspace's last ``last`` events (or the given ones), with nothing run."""
        self.guard.require(actor, ws, "workspace.admin")
        s = RuleSpec.model_validate(spec)
        pool = events if events is not None else list(reversed(self.lyst.events.since(ws, int(self.stores.control.scalar(
            "SELECT greatest(coalesce(max(seq), 0) - ?, 0) FROM platform_events WHERE workspace_id = ?", [last * 4, ws]) or 0),
            limit=last * 4)))[:last][::-1]
        out, seen, per_day = [], {}, {}
        for i, ev in enumerate(pool):
            d = self._decide(ws, s, ev, dry={"seen": seen, "per_day": per_day, "pool": pool[:i + 1]})
            if d["decision"] != "ignored":
                out.append({"seq": ev.get("seq"), "type": ev["type"], "subject": ev["subject"], **d})
        counts: dict[str, int] = {}
        for o in out:
            counts[o["decision"]] = counts.get(o["decision"], 0) + 1
        return {"evaluated": len(pool), "counts": counts, "results": out[:200], "risk": risk_of(s),
                "needs_approval": needs_approval(s)}

    # -- deciding -------------------------------------------------------------------------------

    def _decide(self, ws: str, s: RuleSpec, ev: dict[str, Any], rule_id: str | None = None,
                dry: dict[str, Any] | None = None) -> dict[str, Any]:
        """Detect, interpret and qualify one event against one rule. Pure apart from reading recorded history."""
        if not matches(ev["type"], s.on) or ev["type"].startswith("event."):
            return {"decision": "ignored", "why": "not a type this rule listens to"}
        why: list[str] = [f"{ev['type']} on {ev['subject']['kind']} {ev['subject']['id']}"]
        if s.when is not None:
            ok, text = evaluate(s.when, ev)
            if not ok:
                return {"decision": "no_match", "why": text}
            why.append(text)
        if s.correlate is not None:
            n = self._window_count(ws, s.correlate, ev, dry)
            if n < s.correlate.min_count:
                return {"decision": "no_match", "why": f"{n} of {s.correlate.min_count} {s.correlate.type} events within "
                                                       f"{s.correlate.within_seconds}s"}
            why.append(f"{n} {s.correlate.type} events within {s.correlate.within_seconds}s")
        if not _calendar_ok(s.calendar, _at(ev)):
            return {"decision": "outside_calendar", "why": "outside the rule's business hours"}
        if int(ev.get("depth") or 0) >= self._depth_limit():
            return {"decision": "chain_limit", "why": f"the event is {ev.get('depth')} hops from its root; Lyst stops here"}
        subject = f"{ev['subject']['kind']}:{ev['subject']['id']}"
        at = _at(ev)
        if dry is not None:
            last = dry["seen"].get(subject)
            day = at.date().isoformat()
            if last and (at - last).total_seconds() < s.suppress_seconds:
                return {"decision": "suppressed", "why": "the same subject matched within the suppression window"}
            if dry["per_day"].get(day, 0) >= s.max_per_day:
                return {"decision": "capped", "why": f"{s.max_per_day} activities already today"}
            dry["seen"][subject] = at
            dry["per_day"][day] = dry["per_day"].get(day, 0) + 1
        else:
            db = self._db(ws)
            if s.suppress_seconds and db.scalar(
                    "SELECT count(*) FROM activities WHERE rule_id = ? AND subject_kind = ? AND subject_id = ? "
                    "AND status <> 'suppressed' AND created_at > CAST(? AS TIMESTAMPTZ) - to_seconds(CAST(? AS BIGINT))",
                    [rule_id, ev["subject"]["kind"], ev["subject"]["id"], at, s.suppress_seconds]):
                return {"decision": "suppressed", "why": "the same subject matched within the suppression window"}
            if (db.scalar("SELECT count(*) FROM activities WHERE rule_id = ? AND status NOT IN ('suppressed') "
                          "AND created_at > now() - INTERVAL 1 DAY", [rule_id]) or 0) >= s.max_per_day:
                return {"decision": "capped", "why": f"{s.max_per_day} activities already in the last day"}
        level = s.autonomy
        need = ACTIONS[s.action.kind][1]
        if level == "observe":
            decision = "observed"
        elif s.action.kind == "propose":
            decision = "proposed"
        elif ev["type"].startswith("anticipation.") and ACTIONS[s.action.kind][0] != "low":
            decision = "suggested"  # a prediction never starts consequential work by itself: a person decides
        elif LEVELS.index(level) >= LEVELS.index(need):
            decision = "act"
        else:
            decision = "suggested"
        return {"decision": decision, "why": "; ".join(why)}

    def _depth_limit(self) -> int:
        from .events import max_depth  # noqa: PLC0415

        return max_depth()

    def _window_count(self, ws: str, cor: Correlate, ev: dict[str, Any], dry: dict | None) -> int:
        at = _at(ev)
        if dry is not None and dry.get("pool") is not None:  # a dry run counts within the events it was given
            return sum(1 for e in dry["pool"] if matches(e["type"], [cor.type])
                       and 0 <= (at - _at(e)).total_seconds() < cor.within_seconds
                       and (not cor.same_subject or e["subject"] == ev["subject"]))
        sql = ("SELECT type, subject_kind, subject_id FROM platform_events WHERE workspace_id = ? AND occurred_at <= ? "
               "AND occurred_at > CAST(? AS TIMESTAMPTZ) - to_seconds(CAST(? AS BIGINT))")
        params: list[Any] = [ws, at, at, cor.within_seconds]
        if cor.same_subject:
            sql += " AND subject_kind = ? AND subject_id = ?"
            params += [ev["subject"]["kind"], ev["subject"]["id"]]
        rows = self.stores.control.query(sql + " LIMIT 1000", params)
        return sum(1 for r in rows if matches(r["type"], [cor.type]))

    # -- the consumer -----------------------------------------------------------------------------

    def on_event(self, ev: dict[str, Any]) -> None:
        """Durable consumer: evaluate every active rule of the event's workspace. Idempotent by (rule, version, event)."""
        ws = ev.get("workspace_id")
        if not ws or ev["type"].startswith("event."):
            return
        db = self._db(ws)
        for r, s in self._active(ws):
            if not matches(ev["type"], s.on):
                continue
            key = f"{r['id']}:v{r['current_version']}:{ev['id']}"
            if db.scalar("SELECT count(*) FROM activities WHERE dedupe_key = ?", [key]):
                continue  # this event was already handled by this rule version (a redelivery)
            d = self._decide(ws, s, ev, rule_id=r["id"])
            if d["decision"] == "no_match":
                continue  # a non-match is the common case and not worth a record; a dry run shows them
            row = [r["id"], r["current_version"], ev["id"], ev["seq"], d["decision"], d["why"][:500]]
            acting = d["decision"] not in ("ignored", "outside_calendar", "suppressed", "capped", "chain_limit")

            def note(c, row=row, acting=acting, rid=r["id"]):
                c.execute("INSERT INTO rule_evaluations (rule_id, rule_version, event_id, event_seq, decision, why) "
                          "VALUES (?, ?, ?, ?, ?, ?)", row)
                if acting:
                    c.execute("UPDATE event_rules SET hits = hits + 1, last_matched_at = now() WHERE id = ?", [rid])

            db.write(note)
            if acting:  # held-back matches open no activity, so a repeat or a loop costs nothing
                self._open(ws, r, s, ev, d, key)

    def _active(self, ws: str) -> list[tuple[dict[str, Any], RuleSpec]]:
        """The workspace's active rules, parsed once and reused until any rule changes (a cheap fingerprint read)."""
        db = self._db(ws)
        stamp = db.query("SELECT count(*) AS n, max(updated_at) AS at, sum(current_version) AS v FROM event_rules")[0]
        key = (stamp["n"], str(stamp["at"]), stamp["v"])
        cache = self.__dict__.setdefault("_rule_cache", {})
        hit = cache.get(ws)
        if hit and hit[0] == key:
            return hit[1]
        rows = db.query("SELECT r.id, r.current_version, r.owner, v.spec FROM event_rule_versions v JOIN event_rules r "
                        "ON v.rule_id = r.id AND v.version = r.current_version WHERE r.status = 'active'")
        parsed = [(r, RuleSpec.model_validate_json(r["spec"])) for r in rows]
        cache[ws] = (key, parsed)
        return parsed

    def _open(self, ws: str, r: dict[str, Any], s: RuleSpec, ev: dict[str, Any], d: dict[str, Any], key: str) -> str | None:
        aid = "act_" + uuid.uuid4().hex[:12]
        status = {"observed": "observed", "suppressed": "suppressed", "capped": "suppressed", "chain_limit": "suppressed",
                  "suggested": "suggested", "proposed": "planned", "act": "planned"}[d["decision"]]
        # in words for people; the exact condition stays in the Interpret stage
        happened = self.happened(ws, ev)
        explanation = (f"{happened[:1].upper()}{happened[1:]}, so \"{s.name}\" "
                       + {"observed": "noted it without acting",
                          "suppressed": "did not act again so soon",
                          "suggested": f"suggests that Lyst {self._describe(s, ev, ws)}",
                          "planned": f"will {self._describe(s, ev, ws)}"}[status] + ".")
        stages = [{"stage": "detect", "status": "done", "detail": f"{ev['type']} (event {ev['seq']})"},
                  {"stage": "interpret", "status": "done", "detail": d["why"][:300]},
                  {"stage": "qualify", "status": "done" if status in ("planned", "suggested") else status,
                   "detail": d["decision"]}]
        if status in ("planned", "suggested"):
            stages.append({"stage": "plan", "status": "done", "detail": self._describe(s, ev, ws)})

        def tx(c):
            if c.execute("SELECT 1 FROM activities WHERE dedupe_key = ?", [key]).fetchall():
                return False
            c.execute("INSERT INTO activities (id, rule_id, rule_version, event_id, event_seq, correlation_id, depth, "
                      "subject_kind, subject_id, status, action, autonomy, risk, priority, explanation, stages, owner, "
                      "dedupe_key, sensitivity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [aid, r["id"], r["current_version"], ev["id"], ev["seq"], ev.get("correlation_id") or ev["id"],
                       int(ev.get("depth") or 0), ev["subject"]["kind"], ev["subject"]["id"], status, s.action.kind,
                       s.autonomy, risk_of(s), s.priority, explanation, json.dumps(stages), r["owner"], key,
                       ev.get("sensitivity") or "internal"])
            self.lyst.events.stage(c, ws, "activity.opened", subject=("activity", aid), source="rule", cause=ev,
                                   data={"rule": r["id"], "action": s.action.kind, "status": status, "priority": s.priority},
                                   related=[(ev["subject"]["kind"], ev["subject"]["id"])])
            return True

        if not self._db(ws).write(tx):
            return None
        if status == "planned":
            self._execute(ws, aid)
        return aid

    def title(self, ws: str, kind: str, oid: str) -> str:
        """What people call an object (a dataset's title, a report's title), falling back to its id."""
        sql = {"dataset": ("workspace", "SELECT title FROM datasets WHERE name = ?"),
               "asset": ("workspace", "SELECT title FROM assets WHERE id = ?"),
               "metric": ("workspace", "SELECT v.spec->>'$.title' FROM metrics m JOIN metric_versions v ON v.name = m.name "
                                       "AND v.version = m.current_version WHERE m.name = ?"),
               "task": ("control", "SELECT title FROM tasks WHERE id = ?"),
               "connection": ("control", "SELECT name FROM connections WHERE id = ?")}.get(kind)
        if not sql:
            return oid
        try:
            db = self._db(ws) if sql[0] == "workspace" else self.stores.control
            return str(db.scalar(sql[1], [oid]) or oid)
        except Exception:  # noqa: BLE001 - a missing table or object keeps the id
            return oid

    def happened(self, ws: str, ev: dict[str, Any]) -> str:
        """The event in a sentence."""
        kind, oid, d = ev["subject"]["kind"], ev["subject"]["id"], ev.get("data") or {}
        name = self.title(ws, kind, oid)
        t = ev["type"]
        if t == "dataset.schema_changed":
            parts = [f"{len(d[k])} column{'s' * (len(d[k]) != 1)} {w}" for k, w in
                     (("added", "added"), ("removed", "removed"), ("retyped", "changed type")) if d.get(k)]
            return f"{name} has a new version with {', '.join(parts) or 'a different schema'}"
        if t == "asset.health_changed":
            return f"{name} became {d.get('to', 'out of date')}"
        if t == "task.failed":
            return f"the analysis \"{name}\" failed"
        if t == "contract.checked":
            return f"{name} did not pass its data contract"
        if t == "metric.defined":
            return f"the definition of {name} changed"
        if t == "connection.synced":
            return f"syncing {name} failed"
        if t.startswith("activity."):
            return "another activity started"
        words = t.split(".", 1)[-1].replace(".", " ").replace("_", " ")
        return f"{words} was reported for {name}"

    def _describe(self, s: RuleSpec, ev: dict[str, Any], ws: str | None = None) -> str:
        a = s.action
        subj = self.title(ws, ev["subject"]["kind"], ev["subject"]["id"]) if ws else f"{ev['subject']['kind']} {ev['subject']['id']}"
        return {"notify": f"tell the workspace's owners about {subj}",
                "impact_review": f"list what depends on {subj} and how to repair it",
                "investigate": f"investigate \"{fill(a.question or '', ev)}\"",
                "retry_task": f"retry \"{subj}\" once, if its failure is recoverable",
                "propose": ("ask for approval to repair " + subj) if a.proposal == "asset_repair"
                else f"ask for approval to investigate \"{fill(a.question or '', ev)}\""}[a.kind]

    # -- executing --------------------------------------------------------------------------------

    def _owner(self, uid: str) -> Principal:
        rows = self.stores.control.query("SELECT id, email FROM users WHERE id = ?", [uid])
        if not rows:
            raise AccessDenied("the rule's owner no longer exists")
        return Principal(rows[0]["id"], rows[0]["email"])

    def _execute(self, ws: str, aid: str, approved_by: Principal | None = None) -> None:
        act = self._activity(ws, aid)
        spec = RuleSpec.model_validate_json(self._db(ws).scalar(
            "SELECT spec FROM event_rule_versions WHERE rule_id = ? AND version = ?", [act["rule_id"], act["rule_version"]]))
        ev = self._event(act)
        # Authorize: the owner's permission now, the event's sensitivity, and approval for anything that changes things
        try:
            owner = self._owner(act["owner"])
            role = self.guard.require(owner, ws, ACTIONS[spec.action.kind][2], aid)
            if act["sensitivity"] in ("confidential", "restricted") and RANK[role] < RANK["admin"]:
                raise AccessDenied("the event concerns confidential data its rule's owner may not act on")
        except AccessDenied as exc:
            self._stage(ws, aid, "authorize", "denied", str(exc)[:300])
            self._set(ws, aid, status="blocked")
            return
        if spec.action.kind == "propose" and approved_by is None:
            if act["approval_id"]:
                return
            ap = self.lyst.runtime.request_approval(ws, act["owner"], "activity.execute",
                                                    f"{self._describe(spec, ev, ws)[:1].upper()}{self._describe(spec, ev, ws)[1:]} "
                                                    f"(rule \"{spec.name}\")", {"activity_id": aid})
            self._stage(ws, aid, "authorize", "awaiting_approval", f"approval {ap}")
            self._set(ws, aid, status="awaiting_approval", approval_id=ap)
            return
        if approved_by is None:
            self._stage(ws, aid, "authorize", "done", f"{owner.email}: {role}; autonomy {spec.autonomy}")
        actor = approved_by or owner
        try:
            out = self._run(ws, aid, spec, ev, actor)
        except (ValueError, KeyError, AccessDenied) as exc:
            self._stage(ws, aid, "execute", "failed", str(exc)[:300])
            self._set(ws, aid, status="failed")
            return
        self._stage(ws, aid, "execute", "done", out.get("detail", ""))
        if out.get("task_id"):
            self._set(ws, aid, status="running", task_id=out["task_id"], outcome=out)
            self._stage(ws, aid, "verify", "waiting", f"task {out['task_id']}")
        else:
            self._stage(ws, aid, "verify", "done", out.get("verified", "recorded"))
            self._stage(ws, aid, "deliver", "done", out.get("delivered", ""))
            self._set(ws, aid, status="completed", outcome=out)

    def _run(self, ws: str, aid: str, spec: RuleSpec, ev: dict[str, Any], actor: Principal) -> dict[str, Any]:
        a = spec.action
        rt = self.lyst.runtime
        trigger = {"id": ev["id"], "correlation_id": ev.get("correlation_id") or ev["id"], "depth": int(ev.get("depth") or 0),
                   "activity_id": aid, "rule_id": spec.name}
        if a.kind == "notify":
            fid = raise_notice(self.stores, ws, source=f"rule:{aid}", kind="rule", label=spec.name[:120],
                               summary=(a.summary and fill(a.summary, ev)) or self._activity(ws, aid)["explanation"][:400],
                               priority=float(spec.priority), dedupe_key=f"activity:{aid}", recipients={actor.user_id})
            return {"notice_id": fid, "detail": "inbox notice to the workspace's owners", "verified": "notice recorded",
                    "delivered": f"notice {fid}"}
        if a.kind == "impact_review":
            kind, oid = ev["subject"]["kind"], ev["subject"]["id"]
            if kind == "dataset":
                cols = (ev.get("data") or {}).get("removed") or None
                affected = self.lyst.catalog.impact(actor, ws, oid, cols if isinstance(cols, list) else None)
            elif kind == "asset":
                affected = [{"kind": "asset", "name": oid}]
            else:
                affected = []
            repairs = []
            for x in affected:
                aid_ = x.get("name") if x.get("kind") not in ("metric", "workflow", "monitor", "analysis") else None
                if aid_ and str(aid_).startswith("a_"):
                    try:
                        repairs.append(self.lyst.assets.propose(actor, ws, aid_))
                    except (KeyError, AccessDenied):
                        pass
            items = [{k: x.get(k) for k in ("kind", "name", "title", "status", "why")} for x in affected][:50]
            fid = None
            if items:
                fid = raise_notice(self.stores, ws, source=f"rule:{aid}", kind="rule", label=spec.name[:120],
                                   summary=f"{len(items)} item(s) depend on {kind} {oid}: "
                                           + ", ".join(f"{i['kind']} {i.get('title') or i['name']}" for i in items[:5]),
                                   priority=float(spec.priority), dedupe_key=f"activity:{aid}", recipients={actor.user_id})
            return {"affected": items, "repairs": repairs[:20], "notice_id": fid,
                    "detail": f"{len(items)} dependent item(s), {sum(1 for r in repairs if r.get('automatic'))} repairable",
                    "verified": "computed from the catalog's recorded dependencies", "delivered": "impact list"}
        if a.kind == "investigate" or (a.kind == "propose" and a.proposal == "investigate"):
            q = fill(a.question or "", ev)
            tid = rt.submit(actor, ws, q, idempotency_key=f"activity:{aid}",
                            extra={"trigger": trigger, "initiated_from": {"page": "rule", "object": ev["subject"],
                                                                          "action": f"rule:{spec.name}"[:120]}})
            return {"task_id": tid, "question": q, "detail": f"analysis task {tid}"}
        if a.kind == "retry_task":
            tid = ev["subject"]["id"]
            cls = self._failure_class(tid)
            if cls not in RECOVERABLE:
                raise ValueError(f"the failure is {cls or 'unclassified'}, not recoverable by retrying")
            rt.retry(actor, tid)
            return {"task_id": tid, "detail": f"retried task {tid} ({cls} failure)"}
        if a.kind == "propose" and a.proposal == "asset_repair":
            if ev["subject"]["kind"] != "asset":
                raise ValueError("an asset repair proposal needs an asset event")
            out = self.lyst.assets.repair(actor, ws, ev["subject"]["id"])
            return {**{k: v for k, v in out.items() if k in ("asset_id", "action", "status", "task_id", "version")},
                    "detail": f"repair: {out.get('action')}", "verified": str(out.get("status")), "delivered": "asset version"}
        raise ValueError(f"unknown action {a.kind}")

    def _failure_class(self, task_id: str) -> str | None:
        steps = self.stores.control.query("SELECT error FROM task_steps WHERE task_id = ? AND status = 'failed' "
                                          "ORDER BY seq DESC LIMIT 1", [task_id])
        err = (steps[0]["error"] if steps else None) or self.stores.control.scalar("SELECT error FROM tasks WHERE id = ?", [task_id]) or ""
        head = err.split(":", 1)[0].strip().lower()
        if head in RECOVERABLE:
            return head
        low = err.lower()
        if "timed out" in low or "timeout" in low:
            return "timeout"
        return head or None

    # -- following up: verify and deliver when the work ends ----------------------------------------

    def on_follow(self, ev: dict[str, Any]) -> None:
        """Durable consumer: when a task an activity started ends, verify its result and deliver what it produced."""
        ws = ev.get("workspace_id")
        if not ws or ev["subject"]["kind"] != "task" or not ev["type"].startswith("task."):
            return
        tid = ev["subject"]["id"]
        db = self._db(ws)
        for r in db.query("SELECT id FROM activities WHERE task_id = ? AND status = 'running'", [tid]):
            task = self.stores.control.query("SELECT status, result, error FROM tasks WHERE id = ?", [tid])
            if not task:
                continue
            t = task[0]
            if t["status"] == "done":
                result = json.loads(t["result"]) if isinstance(t["result"], str) else (t["result"] or {})
                verified = result.get("status") == "complete"
                self._stage(ws, r["id"], "verify", "done" if verified else "failed",
                            "every error-severity check passed" if verified else f"result is {result.get('status')}")
                art = self.stores.control.query("SELECT id FROM artifacts WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", [tid])
                self._stage(ws, r["id"], "deliver", "done" if verified else "skipped",
                            f"artifact {art[0]['id']}" if art else "findings in the task's result")
                self._stage(ws, r["id"], "monitor", "done", "no further action")
                asset = db.scalar("SELECT id FROM assets WHERE ref_kind = 'artifact' AND ref_id = ?", [art[0]["id"]]) if art else None
                self._set(ws, r["id"], status="completed" if verified else "needs_attention",
                          outcome_patch={"artifact_id": art[0]["id"] if art else None, "asset_id": asset, "verified": verified})
            elif t["status"] == "failed":
                self._stage(ws, r["id"], "verify", "failed", (t["error"] or "the task failed")[:300])
                self._set(ws, r["id"], status="failed")
            elif t["status"] == "awaiting_approval":
                self._stage(ws, r["id"], "authorize", "awaiting_approval", "a step of the task needs approval")

    # -- reading and controlling ------------------------------------------------------------------

    def _activity(self, ws: str, aid: str) -> dict[str, Any]:
        rows = self._db(ws).query("SELECT * FROM activities WHERE id = ?", [aid])
        if not rows:
            raise KeyError(aid)
        a = dict(rows[0])
        a["stages"] = json.loads(a["stages"] or "[]")
        a["outcome"] = json.loads(a["outcome"]) if a.get("outcome") else None
        a["subject_title"] = self.title(ws, a["subject_kind"], a["subject_id"])
        for k in ("created_at", "updated_at"):
            a[k] = str(a[k]) if a.get(k) is not None else None
        return a

    def _event(self, act: dict[str, Any]) -> dict[str, Any]:
        from .events import row_to_event  # noqa: PLC0415

        rows = self.stores.control.query("SELECT * FROM platform_events WHERE id = ?", [act["event_id"]])
        if not rows:
            raise KeyError(act["event_id"])
        return row_to_event(rows[0])

    def _stage(self, ws: str, aid: str, stage: str, status: str, detail: str) -> None:
        def tx(c):
            row = c.execute("SELECT stages FROM activities WHERE id = ?", [aid]).fetchone()
            stages = [s for s in json.loads(row[0] or "[]") if s["stage"] != stage]
            stages.append({"stage": stage, "status": status, "detail": detail, "at": datetime.now(UTC).isoformat()})
            stages.sort(key=lambda s: STAGES.index(s["stage"]))
            c.execute("UPDATE activities SET stages = ?, updated_at = now() WHERE id = ?", [json.dumps(stages), aid])

        self._db(ws).write(tx)

    def _set(self, ws: str, aid: str, *, status: str, task_id: str | None = None, approval_id: str | None = None,
             outcome: dict[str, Any] | None = None, outcome_patch: dict[str, Any] | None = None) -> None:
        def tx(c):
            row = c.execute("SELECT status, outcome, correlation_id, depth, event_id, event_seq FROM activities WHERE id = ?", [aid]).fetchone()
            cur = json.loads(row[1]) if row[1] else {}
            new = outcome if outcome is not None else ({**cur, **outcome_patch} if outcome_patch else cur)
            c.execute("UPDATE activities SET status = ?, task_id = coalesce(?, task_id), approval_id = coalesce(?, approval_id), "
                      "outcome = ?, updated_at = now() WHERE id = ?",
                      [status, task_id, approval_id, json.dumps(new, default=str) if new else None, aid])
            if status != row[0]:
                cause = {"id": row[4], "correlation_id": row[2], "depth": row[3]}
                self.lyst.events.stage(c, ws, "activity.changed", subject=("activity", aid), source="rule", cause=cause,
                                       data={"from": row[0], "to": status, "task_id": task_id}, strict=False)

        self._db(ws).write(tx)

    def activities(self, actor: Principal, ws: str, *, status: str | None = None, subject: tuple[str, str] | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
        role = self.guard.require(actor, ws, "workspace.read")
        sql, params = "SELECT id FROM activities WHERE status <> 'suppressed'", []
        if status:
            sql, params = "SELECT id FROM activities WHERE status = ?", [status]
        if subject:
            sql += " AND subject_kind = ? AND subject_id = ?"
            params += list(subject)
        rows = self._db(ws).query(sql + " ORDER BY created_at DESC LIMIT ?", [*params, min(max(limit, 1), 500)])
        out = [self._activity(ws, r["id"]) for r in rows]
        return [a for a in out if RANK[role] >= RANK["admin"] or a["sensitivity"] not in ("confidential", "restricted")]

    def get(self, actor: Principal, ws: str, aid: str) -> dict[str, Any]:
        a = next((x for x in self.activities(actor, ws, limit=500) if x["id"] == aid), None) or None
        if a is None:
            role = self.guard.require(actor, ws, "workspace.read")
            a = self._activity(ws, aid)
            if a["sensitivity"] in ("confidential", "restricted") and RANK[role] < RANK["admin"]:
                raise KeyError(aid)
        ev = self._event(a)
        flow = [{"node": "trigger", "label": ev["type"], "ref": ev["seq"]},
                {"node": "rule", "label": a["rule_id"], "ref": a["rule_version"]},
                {"node": "decision", "label": next((s["detail"] for s in a["stages"] if s["stage"] == "qualify"), "")},
                {"node": "task", "label": a.get("task_id") or a["action"]},
                {"node": "verification", "label": next((s["status"] for s in a["stages"] if s["stage"] == "verify"), "pending")},
                {"node": "artifact", "label": (a.get("outcome") or {}).get("artifact_id")}]
        return a | {"event": ev, "flow": flow}

    def run(self, actor: Principal, ws: str, aid: str) -> dict[str, Any]:
        """Run a suggested activity now (a person's go-ahead), or retry a failed or blocked one."""
        a = self._activity(ws, aid)
        spec = RuleSpec.model_validate_json(self._db(ws).scalar(
            "SELECT spec FROM event_rule_versions WHERE rule_id = ? AND version = ?", [a["rule_id"], a["rule_version"]]))
        self.guard.require(actor, ws, ACTIONS[spec.action.kind][2], aid)
        if a["status"] not in ("suggested", "failed", "blocked"):
            raise ValueError(f"this activity is {a['status']}; it can be run only when suggested, failed or blocked")
        if spec.action.kind == "propose":
            self._set(ws, aid, status="planned")
            self._execute(ws, aid)
        else:
            self._stage(ws, aid, "authorize", "done", f"run by {actor.email or actor.user_id}")
            self._set(ws, aid, status="planned")
            try:
                out = self._run(ws, aid, spec, self._event(a), actor)
            except (ValueError, KeyError, AccessDenied) as exc:
                self._stage(ws, aid, "execute", "failed", str(exc)[:300])
                self._set(ws, aid, status="failed")
                return self._activity(ws, aid)
            self._stage(ws, aid, "execute", "done", out.get("detail", ""))
            if out.get("task_id"):
                self._set(ws, aid, status="running", task_id=out["task_id"], outcome=out)
            else:
                self._stage(ws, aid, "verify", "done", out.get("verified", "recorded"))
                self._stage(ws, aid, "deliver", "done", out.get("delivered", ""))
                self._set(ws, aid, status="completed", outcome=out)
        self.guard.audit(actor, ws, "activity.run", aid, "ok")
        return self._activity(ws, aid)

    def dismiss(self, actor: Principal, ws: str, aid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "task.control", aid)
        a = self._activity(ws, aid)
        if a["status"] in ("completed", "cancelled"):
            return a
        if a.get("task_id") and a["status"] == "running":
            try:
                self.lyst.runtime.cancel(actor, a["task_id"])
            except (ValueError, KeyError):
                pass
        self._set(ws, aid, status="cancelled")
        self.guard.audit(actor, ws, "activity.dismiss", aid, "ok")
        return self._activity(ws, aid)

    def evaluations(self, actor: Principal, ws: str, rid: str, limit: int = 100) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.admin", rid)
        return [dict(r) | {"evaluated_at": str(r["evaluated_at"])} for r in self._db(ws).query(
            "SELECT * FROM rule_evaluations WHERE rule_id = ? ORDER BY id DESC LIMIT ?", [rid, min(max(limit, 1), 500)])]

    def prune(self, days: int = 30) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        for w in self.stores.control.query("SELECT id FROM workspaces"):
            self._db(w["id"]).execute("DELETE FROM rule_evaluations WHERE evaluated_at < ?", [cutoff])


# -- templates: the starting rules a workspace can adopt (as drafts) --------------------------------------

TEMPLATES: dict[str, dict[str, Any]] = {
    "data_quality": {
        "name": "Investigate a failed data contract", "on": ["contract.checked"],
        "description": "When a dataset refresh fails a required quality check, investigate it and tell the owners.",
        "when": {"field": "data.outcome", "op": "in", "value": ["breaking", "failed", "fail"]},
        "action": {"kind": "investigate", "question": "Which rows of {subject.id} fail its data contract, and since when?"},
        "autonomy": "investigate", "priority": 7},
    "schema_change": {
        "name": "Review what a schema change affects", "on": ["dataset.schema_changed"],
        "description": "When an upstream schema changes, list the dependent artifacts and how each can be repaired.",
        "action": {"kind": "impact_review"}, "autonomy": "prepare", "priority": 6},
    "broken_asset": {
        "name": "Propose repairing a broken artifact", "on": ["asset.health_changed"],
        "description": "When a saved dashboard or report breaks or goes stale, prepare a repair and ask before running it.",
        "when": {"field": "data.to", "op": "in", "value": ["broken", "stale", "invalidated"]},
        "action": {"kind": "propose", "proposal": "asset_repair"}, "autonomy": "suggest", "priority": 6},
    "metric_deviation": {
        "name": "Investigate a material metric deviation", "on": ["metric.deviated"],
        "description": "When a watched metric moves beyond its expected range and no investigation is running yet, "
                       "investigate it and keep the findings as a report.",
        "when": {"all": [{"field": "data.kind", "op": "in", "value": ["metric_shift", "forecast_deviation"]},
                         {"field": "data.investigation_task", "op": "missing"}]},
        "action": {"kind": "investigate", "question": "Why did {subject.id} change in {data.period}?"},
        "autonomy": "investigate", "priority": 7, "suppress_seconds": 86400},
    "recoverable_failure": {
        "name": "Retry a task after a recoverable failure", "on": ["task.failed"],
        "description": "When a task fails with a transient or model error, retry it once; the task's retry cap still applies.",
        "action": {"kind": "retry_task"}, "autonomy": "automate", "suppress_seconds": 86400, "priority": 4},
    "definition_change": {
        "name": "Review what a metric definition change affects", "on": ["metric.defined"],
        "description": "When an approved metric definition changes, identify the analyses and artifacts that use it.",
        "action": {"kind": "notify", "summary": "The definition of {subject.id} changed; analyses and dashboards that use it compute it the new way."},
        "autonomy": "prepare", "priority": 5},
    "connector_failing": {
        "name": "Flag a failing connection", "on": ["connection.synced"],
        "description": "When a connection's sync fails three times within an hour, tell the owners with its diagnostics.",
        "when": {"field": "data.outcome", "op": "ne", "value": None},
        "correlate": {"type": "connection.synced", "within_seconds": 3600, "min_count": 3, "same_subject": True},
        "action": {"kind": "notify", "summary": "Connection {subject.id} keeps failing; see Settings > Connections for its diagnostics."},
        "autonomy": "prepare", "priority": 7, "suppress_seconds": 21600},
}
for _t in TEMPLATES.values():
    RuleSpec.model_validate(_t)  # every template is a valid rule

__all__ = ["ACTIONS", "LEVELS", "STAGES", "TEMPLATES", "AutonomyService", "RuleSpec", "evaluate", "needs_approval", "risk_of"]
