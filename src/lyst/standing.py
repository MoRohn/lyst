# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Where things stand, and what each pending approval would do.

Two read-only views assembled from records other services already keep, so they cannot disagree with them:

* ``approvals`` explains every pending decision in a workspace, from the runtime's approvals (schedules,
  monitors, skills, gated steps, decisions) and the collaboration reviews (metric and contract definitions,
  publishing): the proposed action, why it was asked, what it affects, what could happen, what is expected, how to
  undo it, and a risk level from what the action actually does (read-only, recurring, changes an authoritative
  definition, adds code).
* ``standing`` is the summary for someone returning to a workspace: what they asked for, what finished, what is
  running, what needs their attention, and what happens next.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .monitoring import cadence
from .security import Guard, Principal

# action -> (risk, consequence, reversal). Risk follows what the action does, not who asks.
ACTIONS: dict[str, tuple[str, str, str]] = {
    "monitor.activate": ("low", "Lyst checks the metric on a schedule and tells people when it moves; it reads data only.",
                         "Turn the monitor off at any time; nothing it did needs undoing."),
    "schedule.activate": ("medium", "The question runs automatically on a schedule, spending compute and model budget each run, "
                                    "and its answers reach whoever follows it.", "Disable the schedule; past runs stay in history."),
    "skill.register": ("high", "New analysis code becomes callable by every analysis in the workspace.",
                       "Retire the skill version; analyses that used it keep their recorded results."),
    "decision.approve": ("high", "A recorded decision becomes the workspace's position and may drive follow-up work.",
                         "Record a new decision that supersedes it; the original stays in history."),
    "metric.define": ("high", "Every analysis, dashboard and monitor that uses this metric will compute it the new way.",
                      "Restore the previous version from the metric's history; earlier answers keep the version they used."),
    "contract.define": ("medium", "New versions of the dataset are checked against these rules; a breaking version is flagged "
                                  "and the reports built on it are marked.", "Define the previous contract version again."),
    "rule.activate": ("medium", "An event rule starts acting on new events in this workspace within its autonomy level, "
                                "for example retrying failed tasks or preparing repairs.",
                      "Deactivate the rule; activities it already opened stay in Work with their history."),
    "activity.execute": ("medium", "Lyst carries out the activity a rule prepared, such as repairing an artifact or starting "
                                   "an investigation, as the rule's owner.",
                         "A repaired artifact keeps its previous version; an investigation can be cancelled in Work."),
    "asset.publish": ("medium", "More people can see this report or dashboard, and the data in it.",
                      "Share it back to its owner only."),
    "glossary.map": ("medium", "Every request that uses this term is read as the mapped metric, for everyone in the workspace.",
                     "Map the term to another metric in Brain › Definitions; earlier answers keep what they used."),
    "look": ("low", "Someone is asked to look at it; nothing changes.", "Nothing to undo."),
}
_STEP_RISK = ("medium", "Lyst runs this step of the task, which changes something rather than only reading data.",
              "Depends on the step: a dashboard can be retired, a sync re-run; the task history records what ran.")


def _ago(ts) -> str | None:
    if ts is None:
        return None
    ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    s = int((datetime.now(UTC) - ts).total_seconds())
    if s < 3600:
        return f"{max(s // 60, 1)} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


class StandingService:
    def __init__(self, stores, guard: Guard, *, runtime, registry=None, collab=None, assets=None, connections=None,
                 onboarding=None, semantic=None) -> None:
        self.stores = stores
        self.guard = guard
        self.runtime = runtime
        self.registry = registry
        self.collab = collab
        self.assets = assets
        self.connections = connections
        self.onboarding = onboarding
        self.semantic = semantic

    # -- approvals ------------------------------------------------------------------------------

    def approvals(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        role = self.guard.require(actor, ws, "workspace.read")
        out = []
        for a in self.runtime.approvals(actor, ws, "pending"):
            payload = json.loads(a["payload"]) if isinstance(a["payload"], str) else (a["payload"] or {})
            out.append(self._runtime_item(actor, ws, a, payload, role))
        if self.collab is not None:
            for r in self.collab.reviews(actor, ws, status="pending"):
                out.append(self._review_item(actor, ws, r))
        order = {"high": 0, "medium": 1, "low": 2}
        out.sort(key=lambda x: (not x["can_decide"], order[x["risk"]], str(x["requested_at"])))
        return out

    def _runtime_item(self, actor, ws, a, payload, role) -> dict[str, Any]:
        action = a["action"]
        affected: list[str] = []
        expected = "The change takes effect once approved."
        if action.startswith("step:"):
            risk, consequence, reversal = _STEP_RISK
            t = self.runtime.db.query("SELECT title, plan FROM tasks WHERE id = ?", [a["task_id"]])
            if t:
                affected.append(f"task \"{t[0]['title']}\"")
            cap = None
            try:
                cap = self.registry.get(a["summary"].removeprefix("Run ").split(":")[0]) if self.registry else None
            except Exception:  # noqa: BLE001 - a capability retired since the request
                cap = None
            if cap is not None and not cap.mutating:
                risk, consequence = "low", "Lyst runs this step of the task; it reads data and changes nothing."
            expected = "The task continues from this step; if rejected it is cancelled and nothing else runs."
        else:
            risk, consequence, reversal = ACTIONS.get(action, ("medium", "The change takes effect once approved.", "Ask an administrator."))
            if action == "schedule.activate":
                s = self.runtime.db.query("SELECT title, request, every_seconds FROM schedules WHERE id = ?", [payload.get("schedule_id")])
                if s:
                    affected.append(f"schedule \"{s[0]['title']}\" ({cadence(s[0]['every_seconds'])})")
                    expected = f"\"{json.loads(s[0]['request']).get('question', s[0]['title'])}\" runs {cadence(s[0]['every_seconds'])}."
            elif action == "monitor.activate":
                m = self.stores.workspace(ws).query("SELECT metric, title, every_seconds FROM monitors WHERE id = ?", [payload.get("monitor_id")])
                if m:
                    affected.append(f"metric {m[0]['metric']}")
                    expected = f"{m[0]['title']} is checked {cadence(m[0]['every_seconds'])}."
            elif action == "skill.register":
                affected.append(f"skill {payload.get('name')} v{payload.get('version')}")
        can = self.guard.can(actor, ws, "skill.approve" if action == "skill.register" else "approval.decide") and not (
            action == "skill.register" and a["requested_by"] == actor.user_id)
        return {"id": a["id"], "source": "task" if a.get("task_id") else "runtime", "action": action, "title": a["summary"],
                "risk": risk, "reason": a["summary"], "affected": affected, "consequences": consequence, "expected": expected,
                "reversal": reversal, "requested_by": a["requested_by"], "requested_at": a["created_at"], "waiting": _ago(a["created_at"]),
                "task_id": a.get("task_id"), "can_decide": bool(can)}

    def _review_item(self, actor, ws, r) -> dict[str, Any]:
        action = r["action"]
        risk, consequence, reversal = ACTIONS.get(action, ("medium", "The change takes effect once approved.", "Ask an administrator."))
        affected: list[str] = [f"{r['object_kind']} {r['object_id']}"]
        expected = "The change takes effect once approved."
        if action == "metric.define":
            name = r["payload"].get("name") or r["object_id"]
            exists = bool(self.stores.workspace(ws).scalar("SELECT count(*) FROM metrics WHERE name = ? AND status = 'approved'", [name]))
            if not exists:
                risk, consequence = "medium", "A new governed metric becomes available; nothing that exists changes."
                expected = f"{name} can be asked about by name."
            else:
                expected = f"{name} gets a new version; answers from now on use it."
            if self.assets is not None:
                affected += [f"{a['kind']} \"{a['title']}\"" for a in self.assets.affected_by(actor, ws, metric=name)][:10]
        elif action == "contract.define" and self.assets is not None:
            affected += [f"{a['kind']} \"{a['title']}\"" for a in self.assets.affected_by(actor, ws, dataset=r["object_id"])][:10]
            expected = f"Every new version of {r['object_id']} is checked against the contract."
        return {"id": r["id"], "source": "review", "action": action, "title": r["summary"], "risk": risk, "reason": r["summary"],
                "affected": affected, "consequences": consequence, "expected": expected, "reversal": reversal,
                "requested_by": r["requested_by"], "requested_at": r["created_at"], "waiting": _ago(r["created_at"]),
                "task_id": None, "can_decide": bool(r.get("can_decide"))}

    # -- where things stand ---------------------------------------------------------------------

    def standing(self, actor: Principal, ws: str, *, days: int = 7) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        since = datetime.now(UTC) - timedelta(days=days)
        ctl = self.runtime.db
        mine = ctl.query("SELECT id, title, status, CASE WHEN status = 'done' THEN CASE WHEN id IN (SELECT task_id FROM task_resolutions) "
                         "THEN 'resolved' ELSE result->>'$.status' END END AS outcome, error, created_at, updated_at "
                         "FROM tasks WHERE workspace_id = ? AND created_by = ? AND created_at >= ? ORDER BY created_at DESC",
                         [ws, actor.user_id, since])
        item = lambda t, extra="": {"text": t["title"] + extra, "task_id": t["id"], "when": _ago(t["updated_at"] or t["created_at"])}  # noqa: E731
        requested = [item(t) for t in mine[:5]]
        completed = [item(t, {"complete": "", "unverified": " (not verified)", "needs_input": " (needs your input)"}.get(t["outcome"] or "", ""))
                     for t in mine if t["status"] == "done"]
        running = [item(t, " (paused)" if t["status"] == "paused" else "") for t in mine if t["status"] in ("queued", "running", "paused")]
        attention: list[dict[str, Any]] = []
        for t in mine:
            if t["status"] == "awaiting_approval":
                attention.append({**item(t), "text": f"{t['title']} is waiting for approval", "kind": "waiting"})
            elif t["status"] == "failed":
                attention.append({**item(t), "text": f"{t['title']} failed: {(t['error'] or '')[:120]}", "kind": "failed"})
            elif t["outcome"] == "needs_input":
                attention.append({**item(t), "text": f"{t['title']} needs your input", "kind": "needs_input"})
        decide = [a for a in self.approvals(actor, ws) if a["can_decide"]]
        if decide:
            top = decide[0]
            attention.append({"kind": "approvals", "text": f"{len(decide)} approval{'s' if len(decide) != 1 else ''} waiting for you"
                              + f"; the most important: {top['title']} ({top['risk']} risk)", "approval_id": top["id"]})
        if self.collab is not None:
            open_ = self.collab.assignments(actor, ws, mine=True, status="open")
            if open_:
                attention.append({"kind": "assignments", "text": f"{len(open_)} assignment{'s' if len(open_) != 1 else ''} for you",
                                  "assignment_id": open_[0]["id"]})
        if self.assets is not None:
            bad = [a for a in self.assets.list(actor, ws) if a["health"] in ("broken", "invalidated", "stale")]
            if bad:
                worst = sorted(bad, key=lambda a: ("broken", "invalidated", "stale").index(a["health"]))[0]
                attention.append({"kind": "assets", "text": f"{len(bad)} report{'s' if len(bad) != 1 else ''} or dashboard{'s' if len(bad) != 1 else ''} "
                                  f"no longer match the data; first: {worst['title']} ({worst['health']})", "asset_id": worst["id"]})
        if self.connections is not None:
            try:
                sick = [c for c in self.connections.health(actor, ws) if c.get("state") in ("failing", "needs_action")]
            except Exception:  # noqa: BLE001 - members without data.read simply see no connection items
                sick = []
            if sick:
                attention.append({"kind": "connections", "text": f"{sick[0]['name']}: {sick[0].get('required_action') or '; '.join(sick[0].get('reasons') or []) or sick[0]['state']}",
                                  "connection_id": sick[0]["id"]})
        upcoming = []
        for s in ctl.query("SELECT id, title, next_run_at FROM schedules WHERE workspace_id = ? AND enabled ORDER BY next_run_at LIMIT 3", [ws]):
            upcoming.append({"text": f"{s['title']} runs next at {str(s['next_run_at'])[:16]}", "schedule_id": s["id"]})
        if self.onboarding is not None:
            ob = self.onboarding.status(actor, ws)
            if ob["next"]:
                step = next(s for s in ob["steps"] if s["key"] == ob["next"])
                upcoming.insert(0, {"text": f"Set-up: {step['title'].lower()}", "onboarding_step": step["key"]})
        parts = []
        if running:
            parts.append(f"{len(running)} running")
        if completed:
            parts.append(f"{len(completed)} finished in the last {days} days")
        if attention:
            parts.append(f"{len(attention)} need{'s' if len(attention) == 1 else ''} your attention")
        headline = (", ".join(parts) + ".").capitalize() if parts else "Nothing is running and nothing needs you."
        return {"headline": headline, "requested": requested, "completed": completed[:5], "running": running,
                "attention": attention, "next": upcoming, "since_days": days}
