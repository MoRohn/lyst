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

_BY = re.compile(r"\b(?:break (?:that|it|this|them)? ?down|split (?:that|it|this)?|by|per|for each|across)\s+(?:by\s+)?"
                 r"(?P<dim>[a-z][a-z _]+?)(?:[?.!,;]|$|\s+(?:and|instead|please|for|in|too|also|now)\b)", re.I)
_YEAR = re.compile(r"\b(?:(?:compare|compared|comparison|vs\.?|versus|against|relative to)\b.{0,30}?\b(?:last|previous|prior) year|"
                   r"year[- ]over[- ]year|yoy|a year (?:ago|earlier|before)|same (?:period|quarter|month) last year)\b", re.I)
_PRIOR = re.compile(r"\b(?:compare|compared|comparison|vs\.?|versus|against|relative to)\b.{0,30}?\b(?:the )?(?:previous|prior|last) "
                    r"(?:quarter|month|period)\b|\b(?:quarter|month)[- ]over[- ](?:quarter|month)\b|\b(?:qoq|mom)\b", re.I)
_WHY = re.compile(r"\b(why|what drove|what(?:'s| is) (?:driving|behind)|driv(?:ing|ers?)|top contribut\w*|biggest contribut\w*|"
                  r"explain\w*|caus(?:e|ed|es|ing))\b", re.I)
_DASH = re.compile(r"\bdashboards?\b", re.I)
_EVERY = re.compile(r"\b(?:every|each)\s+(?P<when>[^?.!,;]+)", re.I)
_FREQ = re.compile(r"\b(?P<f>weekly|daily|monthly)\b", re.I)
_SCHED_VERB = re.compile(r"\b(schedul\w*|repeat\w*|recur\w*|run|send|email|refresh|remind)\b", re.I)
_TREND = re.compile(r"\b(over time|trend|by month|monthly)\b", re.I)
_FORECAST = re.compile(r"\b(forecast|project|predict)\b", re.I)
_LEAD = re.compile(r"^\s*(?:change it|refine(?: it)?|lyst)\s*[:,-]\s*", re.I)
_QUOTES = str.maketrans({"“": '"', "”": '"', "«": '"', "»": '"', "‘": "'", "’": "'", "`": "'"})
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


def normalize(text: str) -> str:
    """The follow-up as the person meant it: typographic quotes, a pasted "Change it:" lead-in and stray quoting
    (people copy the examples, quotes and all) are dropped, so they do not hide the words that say what to change."""
    t = _LEAD.sub("", text.translate(_QUOTES).strip())
    t = t.replace('"', " ").strip(" '")
    return re.sub(r"\s+", " ", t).strip()


def _match_dim(cand: str, dims: list[str]) -> str | None:
    cand = cand.strip().lower().replace(" ", "_")
    for d in dims:
        if cand.startswith(d) or d.startswith(cand.rstrip("s")) or cand.rstrip("s") == d.rstrip("s"):
            return d
    return None


def _dimension(text: str, dims: list[str]) -> str | None:
    """The dimension asked for with "by", "per", "split", "break down": ``by region`` or ``break it down by product line``."""
    m = _BY.search(text)
    return _match_dim(m.group("dim"), dims) if m else None


def _named_dimension(text: str, dims: list[str]) -> str | None:
    """A dimension named on its own ("region", "regions", "product lines?"), when nothing else says what to change."""
    low = text.lower()
    for d in dims:
        if re.search(rf"\b{re.escape(d.replace('_', ' '))}(?:e?s)?\b", low) or re.search(rf"\b{re.escape(d)}(?:e?s)?\b", low):
            return d
    return None


def _cadence(text: str) -> tuple[str | None, bool]:
    """(when it should repeat, whether the person asked for a repeat at all). "every Monday", "each week at 9am",
    "weekly"; a verb ("schedule this") with no time asks for one. "for each region" is a breakdown, not a schedule."""
    verb = _SCHED_VERB.search(text)
    if m := _EVERY.search(text):
        when = m.group("when").strip()
        if _schedule(when):
            return when, True
    if m := _FREQ.search(text):
        f = m.group("f").lower()
        if f != "monthly" or verb:  # "monthly" alone reads as a monthly trend
            return {"weekly": "week", "daily": "day", "monthly": "month"}[f], True
    asked = bool(verb and re.search(r"\b(schedul\w*|repeat\w*|recur\w*)\b", text, re.I))
    return None, asked


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
    text = normalize(text)
    for pat, msg in _UNSUPPORTED:
        if pat.search(text):
            return {"action": "unsupported", "message": msg}
    when_text, wants_schedule = _cadence(text)
    if when_text:
        sched = _schedule(when_text)
        return {"action": "schedule", "question": u["question"], "schedule": sched, "changed": {"schedule": when_text}}
    if wants_schedule:
        return {"action": "unsupported", "message": "Say when it should run, for example \"every Monday at 8\"."}
    if m := _PERIOD.search(text):
        period, changed["period"] = m.group("p"), m.group("p")
        when = f" {period}"
    if _DASH.search(text):
        dim = _dimension(text, dims) or _named_dimension(text, dims) or u.get("by")
        q = f"Build a dashboard of {metric_title}" + (f" by {dim.replace('_', ' ')}" if dim else "") + where
        return {"action": "task", "question": q, "changed": {**changed, "output": "dashboard"}}
    dim = _dimension(text, dims)
    year, prior = _YEAR.search(text), _PRIOR.search(text)
    if dim:
        changed["by"] = dim
        q = f"What was {metric_title}{when}{where} by {dim.replace('_', ' ')}?"
    elif _WHY.search(text):
        changed["intent"] = "explain"
        q = f"Why did {metric_title} change{when}{where}?"
    elif year or prior or not changed.get("period") and re.fullmatch(r"(?:(?:and|what about|how about) )?(?:compared? (?:it |that |this )?(?:to |with )?)?last year\??",
                                       text, re.I):
        year = year or not prior
        changed["comparison"] = "year ago" if year else "prior period"
        q = f"What was {metric_title}{when}{where}, compared with the {'same period a year earlier' if year else 'previous period'}?"
    elif _FORECAST.search(text):
        changed["intent"] = "forecast"
        q = f"Forecast {metric_title}{where}"
    elif _TREND.search(text):
        changed["intent"] = "trend"
        q = f"Show the {metric_title} trend over time{where}"
    elif changed.get("period"):
        q = f"What was {metric_title}{when}{where}?"
    elif dim := _named_dimension(text, dims):
        changed["by"] = dim
        q = f"What was {metric_title}{when}{where} by {dim.replace('_', ' ')}?"
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
        if u.get("intent") in ("brief", "close", "receivables"):
            return self._brief(actor, ws, task_id, parent, u, text, timezone)
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

    def _brief(self, actor: Principal, ws: str, task_id: str, parent: dict[str, Any], u: dict[str, Any], text: str,
               timezone: str | None) -> dict[str, Any]:
        """A brief covers every metric, so it can be repeated or moved to another period, not drilled into: a question
        about one metric is a new question."""
        base = {"parent_task": task_id, "follow_up": text, "carried": {"metric": None, "brief": True}}
        norm = normalize(text)
        when_text, wants_schedule = _cadence(norm)
        if when_text:
            sch = _schedule(when_text)
            res = self.runtime.create_schedule(actor, ws, f"Recurring: {parent['title'][:80]}", u["question"],
                                               sch.get("every_seconds"), at=sch.get("at"), weekdays=sch.get("weekdays"),
                                               timezone=timezone)
            return {**base, "action": "schedule", "question": u["question"], "schedule": sch,
                    "changed": {"schedule": when_text}, **res}
        if wants_schedule:
            return {**base, "action": "unsupported", "message": "Say when it should run, for example \"every Monday at 8\"."}
        if u.get("intent") == "receivables":
            return {**base, "action": "unsupported", "message": "This reads the invoices open today. Ask about one "
                    "client directly, for example \"how much does Acme owe?\", or say \"every Monday at 8\" to have it "
                    "repeated."}
        if m := _PERIOD.search(norm):
            q = f"Brief me on {m.group('p')}" if u.get("intent") == "brief" else f"Run the close checks for {m.group('p')}"
            tid = self.runtime.submit(actor, ws, q, extra={"derived_from": {
                "task_id": task_id, "follow_up": text, "changed": {"period": m.group("p")}, "carried": base["carried"]}})
            return {**base, "action": "task", "question": q, "changed": {"period": m.group("p")}, "task_id": tid}
        what = "A brief covers" if u.get("intent") == "brief" else "The close checks cover"
        return {**base, "action": "unsupported", "message": f"{what} every metric. Ask about one directly, for "
                "example \"why did operating margin fall?\", or say \"every Monday at 8\" to have it repeated."}


__all__ = ["FollowUps", "resolve"]
