# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The embedded autonomy layer's backend: what Lyst can do with the object in front of a person, the one lifecycle every
task reports, and work started from anywhere that lands where people expect it.

* **Context** (:class:`Context`): the page (home, work, data, brain, settings, artifact), the object (its kind and id) and
  an optional objective. Pages share this one typed model instead of each holding its own agent logic.
* **Actions** (:func:`actions`): for a dataset, metric, task, asset, workflow, scenario model or decision record, the
  actions this person may take now, each ``{id, label, kind, needs_approval, reason}``. ``kind`` is navigate (a link),
  ask (starts an analysis), run (does something now) or approve (a step that changes something and needs a person's
  explicit go-ahead). Actions come from what the existing services offer; anything the person's role, the workspace's
  governance or the object's state rules out is left out. Ids are stable (``dataset.profile``, ``task.retry``).
* **Execution** (:func:`execute`): the action list is derived again on the server and an action not in it is refused, so
  the menu is never the only check. Work runs through the services that already exist: the runtime for analyses, the
  asset registry for repairs, the follow-up resolver for drill-downs, the runtime's approval-gated schedules.
* **Lifecycle** (:func:`lifecycle`): Preparing, Investigating, Executing, Verifying, Awaiting Approval, Completed or Needs
  Attention, each derived from a recorded task state, step or event, never from what an agent says about itself.
* **Initiated anywhere**: a task an action starts records where it came from (``initiated_from``); it is an ordinary task
  in Work, its findings are its result, and when it finishes verified its briefing is saved as an artifact in the asset
  registry.

Anticipative maintenance is the existing chain made reachable from the object: a changed schema or definition moves
the affected assets to stale or broken (asset registry scan, with an inbox notice and a repair proposal), the dataset's
and asset's actions offer the repair as an ``approve`` action, and nothing is repaired until a person takes it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from .security import AccessDenied, Principal

log = logging.getLogger("lyst.ambient")

PAGES = ("home", "work", "data", "brain", "settings", "artifact")
KINDS = ("dataset", "metric", "task", "asset", "workflow", "scenario_model", "decision_record")
STATES = ("Preparing", "Investigating", "Executing", "Verifying", "Awaiting Approval", "Completed", "Needs Attention")
MUTATING_STEPS = {"build_dashboard", "refresh_dashboard", "sync_connection", "propose_capability", "run_skill"}


class ObjectRef(BaseModel):
    kind: Literal["dataset", "metric", "task", "asset", "workflow", "scenario_model", "decision_record", "none"] = "none"
    id: str | None = None


class Context(BaseModel):
    """What the person is looking at. Shared by every page; the server never trusts it for permissions."""
    page: Literal["home", "work", "data", "brain", "settings", "artifact"] = "home"
    object: ObjectRef = Field(default_factory=ObjectRef)
    objective: str | None = None


class ActionRefused(AccessDenied):
    pass


def _action(aid: str, label: str, kind: str, reason: str, *, needs_approval: bool = False, **extra) -> dict[str, Any]:
    return {"id": aid, "label": label, "kind": kind, "needs_approval": needs_approval, "reason": reason, **extra}


# ----------------------------------------------------------------------------- lifecycle


def lifecycle(task: dict[str, Any], steps: list[dict[str, Any]] | None = None, events: list[dict[str, Any]] | None = None,
              registry=None) -> dict[str, Any]:
    """The canonical state of a task and the record it comes from."""
    status = task.get("status")
    result = task.get("result") if isinstance(task.get("result"), dict) else None
    outcome = task.get("outcome") or (result or {}).get("status")
    if status == "done":
        if outcome in (None, "complete"):
            return {"state": "Completed", "detail": "Finished and verified" if outcome == "complete" else "Finished",
                    "source": "task.status=done"}
        return {"state": "Needs Attention", "detail": {"needs_input": "Needs your input", "unverified": "Finished but not verified"}
                .get(outcome, f"Finished: {outcome}"), "source": f"result.status={outcome}"}
    if status == "failed":
        return {"state": "Needs Attention", "detail": (task.get("error") or "Failed")[:200], "source": "task.status=failed"}
    if status == "cancelled":
        return {"state": "Completed", "detail": "Cancelled by a person", "source": "task.status=cancelled"}
    if status == "awaiting_approval":
        return {"state": "Awaiting Approval", "detail": "A step needs approval before it runs", "source": "task.status=awaiting_approval"}
    if status == "paused":
        return {"state": "Needs Attention", "detail": "Paused; resume to continue", "source": "task.status=paused"}
    if any(e.get("kind") == "stalled" for e in (events or [])[-5:]):
        return {"state": "Needs Attention", "detail": "A step has run far longer than expected", "source": "event=stalled"}
    running = [s for s in (steps or []) if s.get("status") == "running"]
    if status == "queued" or not running:
        done_caps = {s.get("capability") for s in (steps or []) if s.get("status") == "done"}
        if status == "running" and "plan" in done_caps:
            return {"state": "Investigating", "detail": "Between steps", "source": "task.status=running"}
        return {"state": "Preparing", "detail": "Queued" if status == "queued" else "Starting", "source": f"task.status={status}"}
    phases = {s.get("phase") for s in running}
    caps = {s.get("capability") for s in running}
    label = ", ".join(sorted(c for c in caps if c))
    if caps & MUTATING_STEPS or any(registry and _mutating(registry, c) for c in caps):
        return {"state": "Executing", "detail": label, "source": "step.running"}
    if phases & {"validate"}:
        return {"state": "Verifying", "detail": label, "source": "step.running"}
    if phases & {"deliver", "learn"}:
        return {"state": "Verifying", "detail": f"Writing the answer ({label})", "source": "step.running"}
    if phases & {"understand", "contextualize", "plan"}:
        return {"state": "Preparing", "detail": label, "source": "step.running"}
    return {"state": "Investigating", "detail": label, "source": "step.running"}


def _mutating(registry, cap: str) -> bool:
    try:
        return bool(registry.get(cap).mutating)
    except KeyError:
        return False


def timeline(events: list[dict[str, Any]], steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordered lifecycle transitions, each tied to the task event that caused it."""
    phase_of = {s["seq"]: (s.get("phase"), s.get("capability")) for s in steps}
    out: list[dict[str, Any]] = []

    def add(state: str, e: dict[str, Any], detail: str) -> None:
        if out and out[-1]["state"] == state:
            return
        out.append({"state": state, "at": str(e.get("occurred_at")), "event": e.get("kind"), "detail": detail})

    for e in events:
        k = e.get("kind")
        data = e.get("data") if isinstance(e.get("data"), dict) else json.loads(e.get("data") or "{}") if e.get("data") else {}
        if k in ("queued", "claimed", "recovered", "resumed", "retry"):
            add("Preparing", e, e.get("message", ""))
        elif k == "step_started":
            phase, cap = phase_of.get(data.get("seq"), (None, None))
            state = ("Executing" if cap in MUTATING_STEPS else "Verifying" if phase in ("validate", "deliver", "learn")
                     else "Preparing" if phase in ("understand", "contextualize", "plan") else "Investigating")
            add(state, e, e.get("message", ""))
        elif k == "awaiting_approval":
            add("Awaiting Approval", e, e.get("message", ""))
        elif k in ("failed", "stalled", "paused"):
            add("Needs Attention", e, e.get("message", ""))
        elif k in ("done", "cancelled"):
            add("Completed", e, e.get("message", ""))
    return out


# ----------------------------------------------------------------------------- actions


class AmbientService:
    def __init__(self, lyst) -> None:
        self.lyst = lyst

    @property
    def guard(self):
        return self.lyst.guard

    def _can(self, actor: Principal, ws: str, action: str) -> bool:
        return self.guard.can(actor, ws, action)

    def actions(self, actor: Principal, ws: str, kind: str, oid: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        if kind not in KINDS:
            raise KeyError(f"unknown object kind {kind}")
        return getattr(self, f"_{kind}")(actor, ws, oid)

    def _affected(self, actor, ws, dataset):
        rows = self.lyst.assets.affected_by(actor, ws, dataset=dataset)
        return [{**r, "proposal": self.lyst.assets._proposal(ws, r)} for r in rows]

    @staticmethod
    def _repairable(rows):
        return [a for a in rows if a["health"] in ("stale", "invalidated", "broken") and a["proposal"]["automatic"]]

    # -- per kind -------------------------------------------------------------------------------

    def _dataset(self, actor, ws, name):
        ds = {d["name"]: d for d in self.lyst.data.catalog(actor, ws)}
        if name not in ds:
            raise KeyError(f"dataset {name}")
        out = [_action("dataset.open", "Open", "navigate", "See columns, health and contract", href=f"/data?dataset={name}")]
        if self._can(actor, ws, "analyst.ask"):
            out.append(_action("dataset.profile", "Check data quality", "ask", "Profile it for duplicates, gaps, outliers and nulls",
                               question=f"Profile {name} for data quality issues"))
        affected = self._affected(actor, ws, name)
        if affected:
            out.append(_action("dataset.impact", f"What depends on it ({len(affected)})", "run",
                               "Reports, dashboards and workflows built on this dataset"))
        repairable = self._repairable(affected)
        if repairable and self._can(actor, ws, "task.control"):
            out.append(_action("dataset.repair_affected", f"Repair {len(repairable)} affected", "approve",
                               "The data changed since they were built; each is rebuilt on current data and verified",
                               needs_approval=True))
        if self._can(actor, ws, "contract.define"):
            out.append(_action("dataset.contract", "Draft a data contract", "run",
                               "Propose expectations from the data; nothing is enforced until you save it"))
        return out

    def _metric(self, actor, ws, name):
        spec = self.lyst.semantic.get(actor, ws, name)
        t = spec.title
        out = [_action("metric.lineage", "Where it comes from", "navigate", "Definition, source and what uses it",
                       href=f"/brain?metric={name}")]
        if self._can(actor, ws, "analyst.ask"):
            out += [
                _action("metric.value", f"{t} last quarter", "ask", "Value with prior period and year-ago comparison",
                        question=f"What was {t.lower()} last quarter?"),
                _action("metric.explain", f"Why did {t.lower()} change?", "ask", "Drivers of the latest change",
                        question=f"Why did {t.lower()} change last quarter?"),
                _action("metric.trend", "Trend and anomalies", "ask", "Monthly series with unusual months marked",
                        question=f"Show the {t.lower()} trend over time"),
                _action("metric.forecast", "Forecast", "ask", "Next periods with an interval and its backtest",
                        question=f"Forecast {t.lower()}"),
            ]
        sc = getattr(self.lyst.services, "scenarios", None)
        if sc is not None:
            models = sc.for_metric(actor, ws, name)
            if models:
                out.append(_action("metric.what_if", "Explore scenarios", "navigate", "Change assumptions in a driver model",
                                   href=f"/data?scenario={models[0]}"))
        return out

    def _task(self, actor, ws, tid):
        t = self.lyst.runtime.get(actor, tid)
        if t["workspace_id"] != ws:
            raise KeyError(tid)
        out = [_action("task.open", "Open", "navigate", "The answer, its evidence and how it ran", href=f"/?task={tid}")]
        mine = t["created_by"] == actor.user_id
        control = mine or self._can(actor, ws, "task.control")
        st = t["status"]
        if control:
            if st in ("queued", "running"):
                out.append(_action("task.pause", "Pause", "run", "Stops after the current step; resume any time"))
                out.append(_action("task.cancel", "Cancel", "run", "Stops it; finished steps are kept"))
            if st == "paused":
                out.append(_action("task.resume", "Resume", "run", "Continues from the last finished step"))
            if st == "failed":
                out.append(_action("task.retry", "Try again", "run", "Re-runs from the failed step with a fresh time budget"))
        und = next((s for s in t["steps"] if s["capability"] == "understand" and s["status"] == "done"), None)
        if st == "done" and und and self._can(actor, ws, "analyst.ask"):
            u = json.loads(self.lyst.stores.control.scalar(
                "SELECT output FROM task_steps WHERE task_id = ? AND capability = 'understand'", [tid]) or "{}")
            if u.get("metrics"):
                spec = self.lyst.semantic.get(actor, ws, u["metrics"][0])
                for d in spec.dimensions[:3]:
                    if d != u.get("by"):
                        out.append(_action(f"task.breakdown.{d}", f"By {d.replace('_', ' ')}", "ask",
                                           "Same metric and period, split", follow_up=f"break that down by {d.replace('_', ' ')}"))
                out.append(_action("task.compare_year", "Compare to last year", "ask", "Same period a year earlier",
                                   follow_up="compare it to last year"))
                out.append(_action("task.why", "What drove it", "ask", "Contribution by dimension", follow_up="why?"))
                if self._can(actor, ws, "data.ingest"):
                    out.append(_action("task.dashboard", "Make a dashboard", "ask", "A verified dashboard of this metric",
                                       follow_up="make this a dashboard"))
                if self._can(actor, ws, "schedule.create"):
                    out.append(_action("task.schedule", "Repeat every Monday", "approve",
                                       "A recurring run starts once an admin approves it", needs_approval=True,
                                       follow_up="schedule this every Monday"))
        if st == "done" and (t.get("result") or {}).get("status") == "complete":
            out.append(_action("task.briefing", "Save a briefing", "run", "A Markdown briefing kept in your reports"))
        return out

    def _asset(self, actor, ws, aid):
        a = self.lyst.assets.get(actor, ws, aid)
        out = [_action("asset.open", "Open", "navigate", "View it full screen", href=f"/artifact?asset={aid}"),
               _action("asset.versions", "Versions", "navigate", f"{a['version']} version(s) kept", href=f"/artifact?asset={aid}&view=versions")]
        if a["health"] != "current" and a["status"] == "active":
            p = a["proposal"]
            if p["automatic"] and self._can(actor, ws, "task.control"):
                out.append(_action("asset.repair", "Repair", "approve", p["text"], needs_approval=True))
            else:
                out.append(_action("asset.explain_health", "Why it is " + a["health"], "navigate", p["text"],
                                   href=f"/artifact?asset={aid}&view=health"))
        return out

    def _workflow(self, actor, ws, wid):
        wf = self.lyst.services.workflows.get(actor, ws, wid)
        out = [_action("workflow.open", "Open", "navigate", "Steps, checks and runs", href=f"/data?workflow={wid}")]
        if wf.get("status", "active") == "active" and self._can(actor, ws, "task.create"):
            out.append(_action("workflow.run", "Run for the latest period", "run", "Reuses the verified plan"))
        if self._can(actor, ws, "schedule.create"):
            out.append(_action("workflow.schedule", "Repeat every Monday", "approve", "Starts once an admin approves it",
                               needs_approval=True))
        return out

    def _scenario_model(self, actor, ws, name):
        self.lyst.services.scenarios.get(actor, ws, name)
        return [_action("scenario.explore", "Explore", "navigate", "Change assumptions and compare", href=f"/data?scenario={name}")]

    def _decision_record(self, actor, ws, did):
        d = self.lyst.services.decisions.get(actor, ws, did)
        out = [_action("decision.open", "Open", "navigate", "Options, uncertainty and the recommendation", href=f"/work?decision={did}")]
        if d["status"] in ("analyzed", "rejected") and self._can(actor, ws, "decision.create") and d["recommendation"].get("choice"):
            out.append(_action("decision.submit", f"Ask approval for {d['recommendation']['choice']}", "approve",
                               "An approver decides; nothing is carried out by Lyst", needs_approval=True))
        return out

    # -- execution ------------------------------------------------------------------------------

    def execute(self, actor: Principal, ws: str, kind: str, oid: str, action_id: str, *,
                context: Context | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run an action only if it is in the list derived now for this person and object."""
        offered = {a["id"]: a for a in self.actions(actor, ws, kind, oid)}
        a = offered.get(action_id)
        if a is None:
            self.guard.audit(actor, ws, "action.refused", f"{kind}:{oid}:{action_id}", "denied")
            raise ActionRefused(f"'{action_id}' is not available for this {kind.replace('_', ' ')} now")
        ctx = (context or Context()).model_dump()
        ctx["object"] = {"kind": kind, "id": oid}
        origin = {"page": ctx["page"], "object": ctx["object"], "action": action_id, "objective": ctx.get("objective")}
        rt = self.lyst.runtime
        out: dict[str, Any] = {"action": action_id, "kind": a["kind"]}
        if a["kind"] == "navigate":
            return out | {"navigate": a["href"]}
        if "question" in a:
            tid = rt.submit(actor, ws, a["question"], extra={"initiated_from": origin})
            out |= {"task_id": tid}
        elif "follow_up" in a:
            from .followups import FollowUps  # noqa: PLC0415

            r = FollowUps(rt, self.lyst.semantic).apply(actor, oid, a["follow_up"], timezone=(params or {}).get("timezone"))
            out |= {k: r[k] for k in ("task_id", "schedule_id", "approval_id", "cadence", "question") if k in r}
            if r.get("task_id"):
                rt.db.execute("UPDATE tasks SET request = json_merge_patch(request, ?) WHERE id = ?",
                              [json.dumps({"initiated_from": origin}), r["task_id"]])
        elif action_id == "dataset.impact":
            out |= {"affected": [{k: x.get(k) for k in ("id", "kind", "title", "health", "health_reason", "proposal")}
                                 for x in self._affected(actor, ws, oid)]}
        elif action_id == "dataset.repair_affected":
            out |= {"repairs": [self.lyst.assets.repair(actor, ws, x["id"])
                                for x in self._repairable(self._affected(actor, ws, oid))]}
        elif action_id == "dataset.contract":
            out |= {"draft": self.lyst.contracts.draft(actor, ws, oid)}
        elif action_id in ("task.pause", "task.cancel", "task.resume", "task.retry"):
            getattr(rt, action_id.split(".")[1])(actor, oid)
            out |= {"task_id": oid}
        elif action_id == "task.briefing":
            art = self.lyst.artifacts.build(actor, rt.get(actor, oid), "md")
            out |= {"artifact": {k: art.get(k) for k in ("id", "kind", "bytes", "asset_id") if k in art}}
        elif action_id == "asset.repair":
            out |= self.lyst.assets.repair(actor, ws, oid)
        elif action_id == "workflow.run":
            out |= {"task_id": self.lyst.services.workflows.run(actor, ws, oid, rt)}
        elif action_id == "workflow.schedule":
            out |= self.lyst.services.workflows.schedule(actor, ws, oid, rt, 7 * 86400)
        elif action_id == "decision.submit":
            d = self.lyst.services.decisions.get(actor, ws, oid)
            out |= {"decision": self.lyst.services.decisions.submit(actor, ws, oid, d["recommendation"]["choice"],
                                                                     (params or {}).get("rationale", ""))["status"]}
        else:  # pragma: no cover - every offered id has a branch above
            raise ActionRefused(f"'{action_id}' has no handler")
        if out.get("task_id"):
            out["lifecycle"] = rt.get(actor, out["task_id"])["lifecycle"]
        self.guard.audit(actor, ws, "action.run", f"{kind}:{oid}:{action_id}", "ok",
                         {k: v for k, v in out.items() if k in ("task_id", "approval_id", "schedule_id")})
        return out

    # -- initiated anywhere: keep a verified result as an artifact ------------------------------

    def on_finish(self, task_id: str, state: str) -> None:
        if state != "done":
            return
        try:
            row = self.lyst.stores.control.query("SELECT request, created_by FROM tasks WHERE id = ?", [task_id])
            if not row or "initiated_from" not in (row[0]["request"] or ""):
                return
            owner = self.lyst.stores.control.query("SELECT id, email FROM users WHERE id = ?", [row[0]["created_by"]])[0]
            p = Principal(owner["id"], owner["email"])
            task = self.lyst.runtime.get(p, task_id)
            if (task.get("result") or {}).get("status") == "complete" and not self.lyst.stores.control.scalar(
                    "SELECT count(*) FROM artifacts WHERE task_id = ? AND kind = 'md'", [task_id]):
                self.lyst.artifacts.build(p, task, "md")
        except Exception:  # noqa: BLE001 - keeping a briefing never changes the task's outcome
            log.exception("saving the briefing for %s failed", task_id)


__all__ = ["STATES", "ActionRefused", "AmbientService", "Context", "lifecycle", "timeline"]
