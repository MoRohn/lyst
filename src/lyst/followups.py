# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Conversational drill-down: a follow-up ("break that down by region", "compare it to last year", "make this a
dashboard", "schedule this every Monday") applied to an earlier answer.

The follow-up is resolved against the parent task's verified understanding (metric, period, filters, split), not
re-guessed from the words alone, and becomes an explicit, complete question. The new task records what it was
derived from and what changed, so the analytical state can be revisited and reproduced: nothing depends on hidden
session state. A follow-up Lyst cannot carry out says so and offers what it can do.
"""

from __future__ import annotations

import re
from typing import Any

from .security import Principal

_BY = re.compile(r"\b(?:break (?:that|it|this) down|split (?:that|it|this)?|by|per|for each|across)\s+(?:by\s+)?(?P<dim>[a-z][a-z _]+?)"
                 r"(?:\?|\.|$|\s+(?:instead|please|for|in)\b)", re.I)
_YEAR = re.compile(r"\b(compare (?:it|that|this) (?:to|with) last year|year[- ]over[- ]year|vs\.? last year|a year (?:ago|earlier))\b", re.I)
_PRIOR = re.compile(r"\b(compare (?:it|that|this) (?:to|with) (?:the )?(?:previous|prior|last) (?:quarter|month|period))\b", re.I)
_WHY = re.compile(r"\b(why|what drove|top contribut\w*|biggest contribut\w*|main drivers?|explain (?:it|that|this))\b", re.I)
_DASH = re.compile(r"\b(make (?:this|that|it) (?:into )?a dashboard|dashboard (?:of|for) (?:this|that|it)|build a dashboard)\b", re.I)
_SCHED = re.compile(r"\b(?:schedule|repeat|run) (?:this|that|it)\s+(?:every|each)\s+(?P<when>.+?)(?:\?|\.|$)", re.I)
_TREND = re.compile(r"\b(over time|trend|by month|monthly)\b", re.I)
_FORECAST = re.compile(r"\b(forecast|project|predict)\b", re.I)
_PERIOD = re.compile(r"\b(?:what about|and|for|in)\s+(?P<p>(?:q[1-4] ?\d{2,4})|(?:(?:last|previous|this) (?:quarter|month|year))|"
                     r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{4})|(?:\d{4}))\b", re.I)
_UNSUPPORTED = [
    (re.compile(r"\b(exclude|remove|without) (?:the )?(unusual|outlier|abnormal|one[- ]off)", re.I),
     "Lyst does not drop rows it judges unusual on its own, because that would change the number silently. Name the "
     "rows to exclude (a region, product or period), or ask for the anomaly scan to see which months stand out."),
    (re.compile(r"\b(significan\w*|statistically)\b", re.I),
     "A significance test between two groups is not built in yet. Lyst can show the change with its prior-period and "
     "year-ago comparisons, or an interrupted time series if you name the event and its start month."),
]
_DAYS = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat",
         "sunday": "sun"}


def _dimension(text: str, dims: list[str]) -> str | None:
    m = _BY.search(text)
    if not m:
        return None
    cand = m.group("dim").strip().lower().replace(" ", "_")
    for d in dims:
        if cand.startswith(d) or d.startswith(cand.rstrip("s")) or cand.rstrip("s") == d.rstrip("s"):
            return d
    return None


def _schedule(when: str) -> dict[str, Any] | None:
    w = when.lower()
    days = [v for k, v in _DAYS.items() if k in w or re.search(rf"\b{v}\b", w)]
    at = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", w)
    hour, minute = 8, 0
    if at and ("am" in w or "pm" in w or ":" in w or " at " in f" {w}"):
        hour, minute = int(at.group(1)) % 24, int(at.group(2) or 0)
        if at.group(3) == "pm" and hour < 12:
            hour += 12
    if days:
        return {"at": f"{hour:02d}:{minute:02d}", "weekdays": days}
    if "day" in w and "week" not in w:
        return {"at": f"{hour:02d}:{minute:02d}", "weekdays": None}
    if "week" in w:
        return {"at": f"{hour:02d}:{minute:02d}", "weekdays": ["mon"]}
    if "month" in w:
        return {"every_seconds": 30 * 86400}
    return None


def resolve(parent: dict[str, Any], text: str, dims: list[str], metric_title: str) -> dict[str, Any]:
    """The follow-up as an explicit action. ``parent`` holds the parent task's understanding and period label."""
    u = parent["understand"]
    period = parent.get("period_label")
    filters = u.get("filters") or []
    where = "".join(f" in {f['value']}" for f in filters)
    when = f" {period}" if period else ""
    changed: dict[str, Any] = {}
    for pat, msg in _UNSUPPORTED:
        if pat.search(text):
            return {"action": "unsupported", "message": msg}
    if m := _SCHED.search(text):
        sched = _schedule(m.group("when"))
        if not sched:
            return {"action": "unsupported", "message": "Say when it should run, for example \"every Monday at 8\"."}
        return {"action": "schedule", "question": u["question"], "schedule": sched, "changed": {"schedule": m.group("when")}}
    if m := _PERIOD.search(text):
        period, changed["period"] = m.group("p"), m.group("p")
        when = f" {period}"
    if _DASH.search(text):
        dim = _dimension(text, dims) or u.get("by")
        q = f"Build a dashboard of {metric_title}" + (f" by {dim.replace('_', ' ')}" if dim else "") + where
        return {"action": "task", "question": q, "changed": {**changed, "output": "dashboard"}}
    dim = _dimension(text, dims)
    if dim:
        changed["by"] = dim
        q = f"What was {metric_title}{when}{where} by {dim.replace('_', ' ')}?"
    elif _WHY.search(text):
        changed["intent"] = "explain"
        q = f"Why did {metric_title} change{when}{where}?"
    elif _YEAR.search(text) or _PRIOR.search(text):
        changed["comparison"] = "year ago" if _YEAR.search(text) else "prior period"
        q = f"What was {metric_title}{when}{where}, compared with the {'same period a year earlier' if _YEAR.search(text) else 'previous period'}?"
    elif _FORECAST.search(text):
        changed["intent"] = "forecast"
        q = f"Forecast {metric_title}{where}"
    elif _TREND.search(text):
        changed["intent"] = "trend"
        q = f"Show the {metric_title} trend over time{where}"
    elif changed.get("period"):
        q = f"What was {metric_title}{when}{where}?"
    else:
        return {"action": "unsupported", "message": "Lyst could not tell how to change the earlier answer. Try \"break that "
                "down by region\", \"compare it to last year\", \"why?\", \"make this a dashboard\" or \"schedule this "
                "every Monday\"."}
    return {"action": "task", "question": q, "changed": changed}


class FollowUps:
    def __init__(self, runtime, semantic) -> None:
        self.runtime, self.semantic = runtime, semantic

    def apply(self, actor: Principal, task_id: str, text: str, *, timezone: str | None = None) -> dict[str, Any]:
        parent = self.runtime.get(actor, task_id)
        ws = parent["workspace_id"]
        steps = {s["capability"]: s for s in parent["steps"]}
        und = self.runtime.db.query("SELECT output FROM task_steps WHERE task_id = ? AND capability = 'understand' "
                                    "AND status = 'done'", [task_id])
        ctxs = self.runtime.db.query("SELECT output FROM task_steps WHERE task_id = ? AND capability = 'contextualize' "
                                     "AND status = 'done'", [task_id])
        if not und or "understand" not in steps:
            raise ValueError("the earlier task has no finished understanding to build on")
        import json  # noqa: PLC0415

        u = json.loads(und[0]["output"])
        c = json.loads(ctxs[0]["output"]) if ctxs else {}
        if not u.get("metrics"):
            raise ValueError("the earlier answer has no metric to drill into; ask a full question instead")
        spec = self.semantic.get(actor, ws, u["metrics"][0])
        period_label = (c.get("period") or {}).get("label")
        plan = resolve({"understand": u, "period_label": period_label}, text, spec.dimensions, spec.title.lower())
        carried = {"metric": spec.name, "period": period_label, "filters": u.get("filters") or [], "by": u.get("by")}
        base = {"parent_task": task_id, "follow_up": text, "carried": carried, **plan}
        if plan["action"] == "unsupported":
            return base
        if plan["action"] == "schedule":
            sch = plan["schedule"]
            res = self.runtime.create_schedule(actor, ws, f"Recurring: {parent['title'][:80]}", plan["question"],
                                               sch.get("every_seconds"), at=sch.get("at"), weekdays=sch.get("weekdays"),
                                               timezone=timezone)
            return {**base, **res}
        tid = self.runtime.submit(actor, ws, plan["question"], extra={"derived_from": {
            "task_id": task_id, "follow_up": text, "changed": plan["changed"], "carried": carried}})
        return {**base, "task_id": tid}


__all__ = ["FollowUps", "resolve"]
