# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A question that asks Lyst to watch something: "Watch operating margin in EMEA", "Alert me when revenue moves".

Investigations end with "Set a monitor on operating margin for EMEA", but typing that back only answered the figure:
nothing was watched. Now the figure is given as usual and, when it completed on one approved metric, Lyst asks for a
monitor on it, narrowed to the one segment the question named (a dimension and a member that exist). Like every monitor
it runs only once an admin approves it, and the answer says so. Asking again reuses the monitor already requested, a
scheduled run never asks for one, and a reader whose role cannot set up monitors is told to ask an editor.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import Context

ASKS = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:watch|monitor|keep an eye on|keep watch on|"
    r"set (?:up )?(?:a |an )?(?:monitor|alert|watch) (?:on|for)|"
    r"(?:alert|tell|notify|warn) me (?:when|if)|let me know (?:when|if))\b", re.I)


def asks_to_watch(question: str) -> bool:
    return bool(ASKS.search(question or ""))


def _segment(spec: Any, filters: list[dict[str, Any]]) -> tuple[dict[str, str] | None, bool]:
    """(the one segment the question named, whether it can be watched): one equality filter on an approved dimension."""
    if not filters:
        return None, True
    if len(filters) == 1 and filters[0].get("op", "=") == "=" and filters[0].get("column") in spec.dimensions:
        return {"dimension": filters[0]["column"], "member": str(filters[0]["value"])}, True
    return None, False


def apply(ctx: Context, out: dict[str, Any]) -> dict[str, Any]:
    u = ctx.outputs.get("understand") or {}
    if not asks_to_watch(u.get("question") or "") or out.get("status") != "complete" or ctx.request.get("workflow"):
        return out
    s = ctx.services
    runtime, monitors = getattr(s, "runtime", None), getattr(s, "monitors", None)
    metrics = u.get("metrics") or []
    if runtime is None or monitors is None or ctx.task_id is None or len(metrics) != 1:
        return out
    task = runtime.db.query("SELECT kind FROM tasks WHERE id = ?", [ctx.task_id])
    if task and task[0]["kind"] == "scheduled":
        return out
    try:
        spec = s.semantic.get(ctx.actor, ctx.workspace_id, metrics[0])
    except KeyError:
        return out
    segment, ok = _segment(spec, u.get("filters") or [])
    what = f"{spec.title.lower()}{' in ' + segment['member'] if segment else ''}"
    actions = list(out.get("actions") or [])
    if not ok:
        actions.insert(0, f"Lyst can watch {spec.title.lower()} as a whole or in one segment, such as one region; "
                          "ask again naming one.")
        return {**out, "actions": actions}
    if not s.guard.can(ctx.actor, ctx.workspace_id, "schedule.create"):
        actions.insert(0, f"Ask an editor to watch {what}: your role cannot set up a monitor.")
        return {**out, "actions": actions}
    same = [m for m in monitors.list(ctx.actor, ctx.workspace_id)
            if m["metric"] == spec.name and (m.get("segment") or None) == segment
            and (m.get("enabled") or m.get("approval_id"))]
    if same:
        mon = {"monitor_id": same[0]["id"], "approval_id": same[0].get("approval_id"),
               "enabled": bool(same[0].get("enabled"))}
    else:
        try:
            mon = monitors.create(ctx.actor, ctx.workspace_id, metric=spec.name, grain="month", segment=segment)
        except ValueError as e:  # a member that is not in the data, or a dimension the metric is not split by
            actions.insert(0, f"Lyst could not watch {what}: {e}.")
            return {**out, "actions": actions}
    actions.insert(0, f"Lyst already watches {what} and investigates its next unusual move." if mon["enabled"]
                   else f"Lyst will watch {what} each month and investigate the next unusual move once an admin "
                        "approves it under Approvals.")
    supporting = {**(out.get("supporting") or {}), "monitor": {**mon, "metric": spec.name, "segment": segment, "what": what}}
    return {**out, "actions": actions, "supporting": supporting}


__all__ = ["ASKS", "apply", "asks_to_watch"]
