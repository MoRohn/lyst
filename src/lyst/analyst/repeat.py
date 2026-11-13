# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A first question that says when to repeat it: "Send me the KPI brief every Monday", "revenue by region every day at 9".

The follow-up "schedule this every Monday" already turned an answer into a schedule, but the same words in the first
question were ignored: Lyst answered once and nothing repeated. Now the answer is given as usual and, when it completed,
Lyst asks to repeat the question without its cadence words on that cadence. Like every schedule it starts only once an
admin approves it, and the answer says so. Lyst never schedules from a run that is itself scheduled, and asking again
reuses the schedule already made (a paused one asks for approval again).
"""

from __future__ import annotations

import re
from typing import Any

from .. import followups
from .registry import Context

_TAIL = re.compile(r"\s*\b(?:and|please|from now on|going forward)\s*$", re.I)


def requested(question: str) -> tuple[str, dict[str, Any], str] | None:
    """(the question without its cadence, the schedule, the cadence words) when the question asks to repeat."""
    m = followups._EVERY.search(question or "")
    if not m:
        return None
    when = m.group("when").strip()
    sched = followups._schedule(when)
    if not sched:  # "for each region" is a breakdown, not a schedule
        return None
    rest = (question[:m.start()] + " " + question[m.end():]).strip()
    rest = _TAIL.sub("", re.sub(r"\s+", " ", rest)).strip(" ,;")
    return (rest or question), sched, f"{m.group(0).strip()}"


def apply(ctx: Context, out: dict[str, Any]) -> dict[str, Any]:
    asked = requested((ctx.outputs.get("understand") or {}).get("question") or "")
    if not asked or out.get("status") != "complete" or ctx.request.get("workflow"):
        return out
    question, sched, when = asked
    s = ctx.services
    runtime = getattr(s, "runtime", None)
    if runtime is None or ctx.task_id is None:
        return out
    task = runtime.db.query("SELECT kind FROM tasks WHERE id = ?", [ctx.task_id])
    if task and task[0]["kind"] == "scheduled":
        return out
    actions = list(out.get("actions") or [])
    if not s.guard.can(ctx.actor, ctx.workspace_id, "schedule.create"):
        actions.insert(0, f"Ask an editor to repeat this {when}: your role cannot set up a schedule.")
        return {**out, "actions": actions}
    same = runtime.db.query("SELECT id FROM schedules WHERE workspace_id = ? "
                            "AND json_extract_string(request, '$.question') = ?",
                            [ctx.workspace_id, question])
    if same:  # asked again: a running schedule stays as it is; a paused one asks for approval again
        res = runtime.resume_schedule(ctx.actor, ctx.workspace_id, same[0]["id"])
    else:
        res = runtime.create_schedule(ctx.actor, ctx.workspace_id, f"Recurring: {question[:80]}", question,
                                      sched.get("every_seconds"), at=sched.get("at"), weekdays=sched.get("weekdays"))
    actions.insert(0, f"This repeats {when} once an admin approves it under Approvals." if not res.get("enabled")
                   else f"This already repeats {when}.")
    supporting = {**(out.get("supporting") or {}), "schedule": {**res, "question": question, "when": when}}
    return {**out, "actions": actions, "supporting": supporting}


__all__ = ["apply", "requested"]
