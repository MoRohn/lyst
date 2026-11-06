# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Conversation: the questions an analyst answers by talking, not by running numbers.

"Do we have an executive dashboard?", "What can you do?", "How do I add data?", "Thanks" are not requests for a figure.
Forcing them through the metric pipeline produced a confident, verified answer to a question nobody asked. This module
decides when a request is conversational (``route``), reads what really exists in the workspace (``inventory``) and
writes a short, grounded reply (``compose``), with a concrete next step the person can take, such as building the
dashboard that does not exist yet.

Rules the reply keeps:

* **No figures.** A conversational reply computes nothing, so it states no metric values. The only numbers it may
  carry are counts of things listed in the inventory, and a model that words it may not add any (engine.converse).
* **Only what exists.** Every dashboard, report, dataset, metric, rule or workflow named comes from the workspace's own
  catalog, read with the person's own permissions.
* **Never "Verified".** Nothing was computed, so there is nothing to verify: the result says it is a reply.
* **Offers, never acts.** Building something is a suggestion the person sends, which then runs through the governed
  pipeline (and its role checks and approvals) like any other request.
"""

from __future__ import annotations

import re
from typing import Any

# What a person can ask about, by the words they use for it.
TOPICS: dict[str, str] = {
    "outputs": r"dashboards?|scorecards?|reports?|reporting|briefings?|presentations?|decks?|workbooks?|charts?|views?|assets?",
    "data": r"datasets?|data ?sets?|data|tables?|sources?|connections?|connectors?|files?|spreadsheets?",
    "metrics": r"metrics?|kpis?|measures?|definitions?|glossary|terms?",
    "automation": r"rules?|alerts?|automations?|monitors?|notifications?|workflows?|schedules?|recurring",
    "scenarios": r"scenarios?|what[- ]ifs?|models?|simulations?",
}
_TOPIC = {k: re.compile(rf"\b({v})\b", re.I) for k, v in TOPICS.items()}
_ANY_THING = re.compile(r"\b(" + "|".join(TOPICS.values()) + r")\b", re.I)

# "Do we have ...", "Is there ...", "Which dashboards do we have", "Show me our reports", "Where is ..."
_DISCOVERY = [
    re.compile(r"^(do|does|did|have|has)\s+(we|i|you|lyst|it|they|anyone|someone)\s+(already\s+|ever\s+|currently\s+)?"
               r"(have|has|got|own|keep|track|maintain|monitor|use|built|build|made|set up|setup)\b", re.I),
    re.compile(r"^(is|are)\s+there\b", re.I),
    re.compile(r"^(what|which)\b.{0,40}\b(do|does|did)\s+(we|i|you|lyst)\s+(have|track|keep|use|monitor|own)\b", re.I),
    re.compile(r"^(what|which)\s+(\w+\s+){0,2}(" + "|".join(TOPICS.values()) + r")\s+(are|is|exist|have|do|does)\b", re.I),
    re.compile(r"^(list|show)\s+(me\s+)?(all\s+|my\s+|our\s+|the\s+|every\s+|available\s+)*(" + "|".join(TOPICS.values())
               + r")\b", re.I),
    re.compile(r"^(where\s+(is|are|can|do|does|would|should|did)|where'?s|how\s+do\s+i\s+find)\b", re.I),
]
_DEFINITION = re.compile(r"\b(what\s+does\s+.+\s+mean|meaning\s+of|definition\s+of|define\b|how\s+(is|are|do\s+we|do\s+you)\s+"
                         r".+\s+(calculated|defined|measured|computed|worked\s+out))", re.I)
_HELP = re.compile(r"^(how\s+(do|can|should|would)\s+(i|we|you)|how\s+does\b|how\s+to\b|help\b|i\s+need\s+help|"
                   r"what\s+(can|should)\s+(i|we)\s+(ask|do\s+here|use)|walk\s+me\s+through|explain\s+how)", re.I)
_CAPABILITY = re.compile(r"^(what\s+can\s+(you|lyst)|what\s+do\s+you\s+do|what\s+are\s+you|who\s+are\s+you|what\s+is\s+lyst|"
                         r"are\s+you\s+able|is\s+lyst\s+able|what\s+are\s+your)\b", re.I)
_CAN_YOU = re.compile(r"^(can|could|would|will)\s+(you|lyst)\b", re.I)
_ACTION = re.compile(r"\b(show|tell|give|calculate|compute|forecast|predict|break\s*down|split|explain|compare|analy[sz]e|"
                     r"investigate|chart|plot|trend|profile|run|refresh|sync|what\s+(was|is|were|are))\b", re.I)
# "How do I ..." is about using Lyst only when it is about doing something in it; "How do we cut shipping delays?" is
# a business question for the analysis (or a specialist), not a how-to.
_USAGE = re.compile(r"\b(add|upload|import|connect|create|build|define|set\s+up|setup|share|export|download|find|use|invite|"
                    r"schedule|change|edit|rename|delete|remove|approve|ask|start|get\s+started|navigate|open|save|publish|"
                    r"lyst|you)\b", re.I)
_GREETING = re.compile(r"^(hi|hello|hey|hiya|yo|good\s+(morning|afternoon|evening)|thanks|thank\s+you|thx|cheers|ok(ay)?|"
                       r"great|cool|nice|perfect|got\s+it|sounds\s+good)\b[\s!.,]*", re.I)
_DISCUSS = re.compile(r"^(what\s+do\s+you\s+think|do\s+you\s+think|should\s+(we|i)\b|what\s+should\s+(we|i)\b|any\s+(thoughts|ideas|"
                      r"suggestions|recommendations)|what\s+would\s+you|let'?s\s+(talk|discuss|chat)|i'?m\s+(thinking|wondering|"
                      r"curious)|i\s+(wonder|was\s+wondering)|tell\s+me\s+about\s+(yourself|lyst|this\s+workspace|"
                      r"the\s+workspace|our\s+data|my\s+data))", re.I)
_BUILD = re.compile(r"\b(create|build|set\s+up|make|generate)\b.*\b(dashboard|report(ing)?|scorecard)\b", re.I)


def topic_of(text: str) -> str:
    """The kind of thing a conversational question is about; 'general' when it names none."""
    for name, pat in _TOPIC.items():
        if pat.search(text):
            return name
    return "general"


def route(text: str, *, metrics_named: bool = False) -> dict[str, str] | None:
    """Whether a request is conversation, and which kind: None means it asks for a figure or analysis.

    ``metrics_named`` says the request names an approved metric. A question about whether something exists, how to
    use Lyst, or what a term means is conversation even then ("Do we have a revenue dashboard?"); a request that names
    a metric and asks for a figure ("Can you show revenue by region?") is analysis."""
    t = " ".join((text or "").split())
    if not t or re.match(r"^\s*(select|with)\b", t, re.I):
        return None
    if re.match(r"^\s*(refresh dashboard|sync connection)\b", t, re.I):
        return None
    words = len(t.split())
    if _GREETING.match(t) and words <= 8 and not (metrics_named and _ACTION.search(t)):
        return {"kind": "greeting", "topic": "general"}
    if _CAPABILITY.match(t):
        return {"kind": "capability", "topic": topic_of(t)}
    if _DEFINITION.search(t) and metrics_named:
        return {"kind": "definition", "topic": "metrics"}
    for pat in _DISCOVERY:
        if pat.match(t) and _ANY_THING.search(t):
            return {"kind": "discovery", "topic": topic_of(t)}
    if _BUILD.search(t) and not metrics_named:  # "build me an executive dashboard": which metrics is the open question
        return {"kind": "build", "topic": "outputs"}
    if _HELP.match(t) and (_ANY_THING.search(t) or _USAGE.search(t) or re.match(r"^\s*help\b", t, re.I)) \
            and not (metrics_named and _ACTION.search(t)):
        return {"kind": "help", "topic": topic_of(t)}
    if _CAN_YOU.match(t) and not (metrics_named and _ACTION.search(t)):
        return {"kind": "capability", "topic": topic_of(t)}
    if _DISCUSS.match(t):
        return {"kind": "discussion", "topic": topic_of(t)}
    return None


# -------------------------------------------------------------------------------------------------- inventory


def _safe(fn, default):
    try:
        out = fn()
        return default if out is None else out
    except Exception:  # noqa: BLE001 - a section the person cannot read, or a service not wired, is simply left out
        return default


def inventory(services: Any, actor: Any, ws: str) -> dict[str, Any]:
    """What exists in the workspace, read with the asking person's permissions. Names and kinds only, no values."""
    s = services
    metrics = _safe(lambda: [{"name": m.name, "title": m.title, "description": m.description, "dataset": m.dataset,
                              "dimensions": list(m.dimensions), "kind": m.kind, "format": m.format}
                             for m in s.semantic.metrics(actor, ws)], [])
    datasets = _safe(lambda: [{"name": d["name"], "title": d.get("title") or d["name"], "version": d.get("version"),
                               "description": d.get("description")} for d in s.data.catalog(actor, ws)], [])
    dashboards = _safe(lambda: [{"id": d["id"], "title": d["title"], "status": d.get("status")}
                                for d in s.studio.list(actor, ws)], []) if s.studio else []
    assets_svc = getattr(s.studio, "assets", None) if s.studio else None
    assets = _safe(lambda: [{"id": a["id"], "kind": a["kind"], "title": a["title"], "health": a.get("health")}
                            for a in assets_svc.list(actor, ws) if a["kind"] not in ("workflow",)], []) if assets_svc else []
    # a dashboard is registered as an asset too: list it once
    dash_titles = {d["title"] for d in dashboards}
    assets = [a for a in assets if not (a["kind"] == "dashboard" and a["title"] in dash_titles)]
    workflows = _safe(lambda: [{"id": w["id"], "title": w["title"], "status": w.get("status")}
                               for w in s.workflows.list(actor, ws, limit=50)], []) if s.workflows else []
    scenarios = _safe(lambda: [{"name": m["name"], "title": m.get("title") or m["name"]}
                               for m in s.scenarios.models(actor, ws)], []) if s.scenarios else []
    monitors = _safe(lambda: [{"id": m["id"], "title": m["title"], "metric": m["metric"], "enabled": bool(m.get("enabled"))}
                              for m in s.monitors.list(actor, ws)], []) if s.monitors else []
    autonomy = getattr(s.runtime, "autonomy", None) if s.runtime else None
    rules = _safe(lambda: [{"id": r["id"], "title": r.get("name") or r["id"], "status": r.get("status")}
                           for r in autonomy.rules(actor, ws)], []) if autonomy else []
    can_build = _safe(lambda: bool(s.guard.can(actor, ws, "data.ingest")), False)
    return {"workspace": ws, "metrics": metrics, "datasets": datasets, "dashboards": dashboards, "assets": assets,
            "workflows": workflows, "scenarios": scenarios, "monitors": monitors, "rules": rules, "can_build": can_build}


# -------------------------------------------------------------------------------------------------- the reply


def _join(items: list[str], limit: int = 6) -> str:
    items = [i for i in items if i]
    if len(items) > limit:
        items = items[:limit] + [f"{len(items) - limit} more"]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _lower(title: str) -> str:
    """Sentence-case a title for use mid-sentence, keeping acronyms (COGS, EBIT) as they are."""
    return " ".join(w if (w.isupper() and len(w) > 1) else w.lower() for w in (title or "").split())


def headline_metrics(inv: dict[str, Any], limit: int = 4) -> tuple[list[dict[str, Any]], str | None]:
    """The approved metrics an overview would lead with, and a dimension they all share (a dashboard needs one).

    The dataset with the most approved metrics is the business's main ledger; its metrics come first, in definition
    order, kept only while they still share a dimension with the ones already chosen."""
    metrics = inv.get("metrics") or []
    if not metrics:
        return [], None
    by_ds: dict[str, list[dict[str, Any]]] = {}
    for m in metrics:
        by_ds.setdefault(m["dataset"], []).append(m)
    main = max(by_ds.values(), key=len)
    chosen: list[dict[str, Any]] = []
    shared: set[str] | None = None
    for m in main:
        dims = set(m.get("dimensions") or [])
        nxt = dims if shared is None else shared & dims
        if not nxt:
            continue
        # a filtered variant of a metric already chosen ("hardware revenue" beside "revenue") adds little to an overview
        if any(c["title"].lower() in m["title"].lower() for c in chosen):
            continue
        chosen.append(m)
        shared = nxt
        if len(chosen) == limit:
            break
    dim = None
    if shared:
        dim = "region" if "region" in shared else sorted(shared)[0]
    return chosen, dim


def build_suggestion(inv: dict[str, Any], question: str) -> str | None:
    """A request that builds the overview a person asked about, from approved metrics only, or None if none fit."""
    chosen, dim = headline_metrics(inv)
    if not chosen or not dim:
        return None
    exec_ = bool(re.search(r"\b(exec\w*|leadership|board|ceo|cfo|top|key|headline|overview)\b", question, re.I))
    title = "Executive overview" if exec_ else "Business overview"
    names = _join([_lower(m["title"]) for m in chosen], limit=len(chosen))
    return f'Build a dashboard called "{title}" with {names} by {dim.replace("_", " ")}'


def _relevant(items: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    """Items whose title shares a meaningful word with the question ("executive" in "Executive overview")."""
    stop = {"the", "a", "an", "our", "we", "do", "have", "to", "in", "of", "for", "one", "place", "and", "is", "there",
            "any", "dashboard", "dashboards", "report", "reports", "my", "me", "show", "list", "all", "what", "which",
            "monitor", "metrics", "top", "on", "that", "this", "with", "can", "you", "it"}
    words = {w for w in re.findall(r"[a-z]{3,}", question.lower()) if w not in stop}
    words |= {w[:-1] for w in words if w.endswith("s")}
    return [i for i in items if words & set(re.findall(r"[a-z]{3,}", (i.get("title") or "").lower()))]


def _examples(inv: dict[str, Any]) -> list[str]:
    """Questions that will run as written in this workspace, built from its own approved metrics."""
    chosen, dim = headline_metrics(inv, limit=2)
    out = []
    if chosen:
        m0 = _lower(chosen[0]["title"])
        out.append(f"What was {m0} last quarter" + (f" by {dim.replace('_', ' ')}?" if dim else "?"))
        out.append(f"Why did {_lower(chosen[-1]['title'])} change last quarter?")
        out.append(f"Forecast {m0}")
    return out


HOW_TO = {
    "data": "Open Data and choose Add data. You can upload a file, connect a database or warehouse, or describe an API, and "
            "I'll profile what arrives and check its quality before anyone relies on it.",
    "outputs": "Ask for it in a sentence, naming the approved metrics you want on it, like \"Build a dashboard of revenue by "
               "region\". I build it from the governed definitions, check every page, and only then publish it.",
    "metrics": "Metrics are defined once, under Brain, by an admin: the dataset, the calculation and the splits it allows. "
               "After that every question about it uses the same definition.",
    "automation": "Ask about a metric, then choose Watch this metric under the answer, or Repeat it to have the analysis run "
                  "on its own. Anything that runs on its own waits for an admin's approval first.",
    "scenarios": "Ask a what-if question in plain words, like \"What if revenue grows 5%?\". I apply it to a saved scenario "
                 "model and show the observed basis beside the result.",
}


def compose(question: str, route_: dict[str, str], inv: dict[str, Any], *, named: list[str] | None = None) -> dict[str, Any]:
    """The template reply: answer first, in plain sentences, grounded in ``inv``. Returns answer, items (what was
    found, each a real record), suggestions (requests the person can send as written) and notes."""
    kind, topic = route_["kind"], route_["topic"]
    named = named or []
    metrics = inv.get("metrics") or []
    mt = {m["name"]: m for m in metrics}
    items: list[dict[str, Any]] = []
    suggestions: list[str] = []
    notes: list[str] = []
    parts: list[str] = []

    outputs = [{"kind": "dashboard", "id": d["id"], "title": d["title"], "note": d.get("status")} for d in inv["dashboards"]] + \
              [{"kind": a["kind"], "id": a["id"], "title": a["title"], "note": a.get("health")} for a in inv["assets"]]
    build = build_suggestion(inv, question)

    if kind == "greeting":
        thanks = re.match(r"^\s*(thanks|thank\s+you|thx|cheers)", question, re.I)
        parts.append("You're welcome." if thanks else "Hi.")
        parts.append("Ask me about any approved metric, or what's in this workspace, and I'll take it from there.")
        suggestions += _examples(inv)[:2]
    elif kind == "definition":
        for name in named:
            m = mt.get(name)
            if m:
                parts.append(f"{m['title']} is an approved metric on the {m['dataset']} dataset: "
                             f"{(m.get('description') or 'no description recorded').rstrip('.')}." )
                if m.get("dimensions"):
                    parts.append(f"It can be split by {_join([d.replace('_', ' ') for d in m['dimensions']])}.")
                items.append({"kind": "metric", "id": name, "title": m["title"], "note": m["dataset"]})
                suggestions.append(f"What was {_lower(m['title'])} last quarter?")
        if not parts:
            parts.append("I couldn't find an approved definition for that term. An admin can add one, or map the term "
                         "to an existing metric in the glossary.")
    elif kind == "build":
        if build:
            chosen, dim = headline_metrics(inv)
            parts.append(f"Yes. For an overview of the business I'd lead with {_join([_lower(m['title']) for m in chosen])}, "
                         f"split by {dim.replace('_', ' ')}: they're approved metrics on the same data, so the dashboard "
                         "stays consistent with every other answer.")
            parts.append("Send the request below as it is, or change the metrics first.")
            suggestions.append(build)
            if not inv.get("can_build"):
                notes.append("Building a dashboard needs the editor role, so an editor would have to send it.")
        else:
            parts.append("I can build one once there are approved metrics to put on it. None are approved in this workspace "
                         "yet: an admin can define them under Brain.")
    elif kind == "help" and topic in HOW_TO:
        parts.append(HOW_TO[topic])
        suggestions += _examples(inv)[:2]
    elif kind == "discovery" and topic == "general" and re.match(r"^\s*(where|how\s+do\s+i\s+find)", question, re.I):
        parts.append("That isn't something in this workspace's catalog. To get to a page, type its name in the bar, "
                     "like \"Data\", \"Brain\" or \"Settings\", and I'll take you there.")
    elif topic == "outputs":
        want_dash = bool(re.search(r"\bdashboards?\b", question, re.I))
        pool = [o for o in outputs if o["kind"] == "dashboard"] if want_dash else outputs
        hits = _relevant(pool, question)
        thing = "dashboard" if want_dash else "dashboard or report"
        qualifier = re.search(r"\b(exec\w*|leadership|board|ceo|cfo)\b", question, re.I)
        if hits:
            parts.append(f"Yes: {_join([h['title'] for h in hits])}.")
            items += hits
        elif pool and not qualifier:
            parts.append(f"Yes, there {'is' if len(pool) == 1 else 'are'} {len(pool)}: {_join([p['title'] for p in pool])}.")
            items += pool
        elif pool:
            parts.append(f"Not quite. There's no {qualifier.group(1).lower()} {thing} yet; what this workspace has is "
                         f"{_join([p['title'] for p in pool])}.")
            items += pool
        else:
            parts.append(f"Not yet. Nobody has built a {thing} in this workspace so far.")
        if build and not hits:
            chosen, dim = headline_metrics(inv)
            parts.append(f"I can build one that keeps your headline numbers in one place: "
                         f"{_join([_lower(m['title']) for m in chosen])}, by {dim.replace('_', ' ')}, from the approved "
                         "definitions, checked before it's published. Want me to?")
            suggestions.append(build)
            if not inv.get("can_build"):
                notes.append("Building a dashboard needs the editor role, so an editor would have to send it.")
        elif hits or pool:
            suggestions += [f"Refresh dashboard {h['id']}" for h in (hits or pool) if h["kind"] == "dashboard"][:1]
    elif topic == "data":
        ds = inv["datasets"]
        if ds:
            parts.append(f"This workspace has {len(ds)} dataset{'s' if len(ds) != 1 else ''}: "
                         f"{_join([d['title'] for d in ds])}.")
            items += [{"kind": "dataset", "id": d["name"], "title": d["title"], "note": f"v{d['version']}"} for d in ds]
            if metrics:
                parts.append(f"{len(metrics)} approved metric{'s are' if len(metrics) != 1 else ' is'} defined on them, "
                             "so you can ask about those directly.")
            suggestions += [f"Profile {ds[0]['name']} for data quality issues"] + _examples(inv)[:1]
        else:
            parts.append("There's no data in this workspace yet. Add a file or connect a source under Data, and I'll "
                         "profile it and suggest the first metrics to define.")
    elif topic == "metrics":
        if metrics:
            parts.append(f"There {'are' if len(metrics) != 1 else 'is'} {len(metrics)} approved metric"
                         f"{'s' if len(metrics) != 1 else ''}: {_join([m['title'] for m in metrics], limit=8)}.")
            items += [{"kind": "metric", "id": m["name"], "title": m["title"], "note": m["dataset"]} for m in metrics]
            parts.append("Each has one governed definition, so every answer about it uses the same numbers.")
            suggestions += _examples(inv)[:2]
        else:
            parts.append("No metrics are approved yet. An admin defines them under Brain; until then I can profile the data "
                         "and run SQL you write.")
    elif topic == "automation":
        rules, mons, wfs = inv["rules"], inv["monitors"], inv["workflows"]
        if rules or mons or wfs:
            if rules:
                parts.append(f"Rules: {_join([r['title'] + ('' if r.get('status') == 'active' else ' (' + str(r.get('status')) + ')') for r in rules])}.")
                items += [{"kind": "rule", "id": r["id"], "title": r["title"], "note": r.get("status")} for r in rules]
            if mons:
                parts.append(f"Watching: {_join([m['title'] + ('' if m['enabled'] else ' (waiting for approval)') for m in mons])}.")
                items += [{"kind": "monitor", "id": m["id"], "title": m["title"], "note": "on" if m["enabled"] else "pending"} for m in mons]
            if wfs:
                parts.append(f"Repeatable analyses: {_join([w['title'] for w in wfs])}.")
                items += [{"kind": "workflow", "id": w["id"], "title": w["title"], "note": w.get("status")} for w in wfs]
        else:
            parts.append("Nothing runs on its own here yet: no rules, monitors or repeatable analyses.")
            chosen, _ = headline_metrics(inv, limit=1)
            if chosen:
                parts.append(f"A good first one is a watch on {_lower(chosen[0]['title'])}, so a real move gets investigated "
                             "without anyone asking: ask about it, then choose Watch this metric under the answer.")
                suggestions.append(f"What was {_lower(chosen[0]['title'])} last quarter?")
    elif topic == "scenarios":
        sc = inv["scenarios"]
        if sc:
            parts.append(f"Yes, {_join([x['title'] for x in sc])} {'is' if len(sc) == 1 else 'are'} set up for what-if questions.")
            items += [{"kind": "scenario", "id": x["name"], "title": x["title"], "note": None} for x in sc]
            if any(m["name"] == "revenue" for m in metrics):
                suggestions.append("What if revenue grows 5%?")
        else:
            parts.append("There are no scenario models yet. An admin can define one under Data › Saved analyses, from "
                         "approved metrics and stated assumptions.")
    elif kind in ("capability", "help", "discussion", "discovery"):
        if kind == "capability" or kind == "help":
            parts.append("I'm Lyst, the analyst for this workspace. I answer questions about your approved metrics with "
                         "figures I compute and check, explain what moved them, forecast them, build dashboards and "
                         "reports, and watch for changes.")
            if metrics:
                parts.append(f"Here that means {_join([_lower(m['title']) for m in metrics], limit=5)}"
                             f"{', on ' + _join([d['title'] for d in inv['datasets']], limit=3) if inv['datasets'] else ''}.")
            else:
                parts.append("No metrics are approved here yet, so start by adding data under Data.")
        else:
            parts.append("Happy to think it through with you. I won't put numbers on it from memory; when a figure "
                         "matters I'll compute it from the approved data.")
            for name in named:
                if (m := mt.get(name)):
                    suggestions.append(f"Why did {_lower(m['title'])} change last quarter?")
        suggestions += [x for x in _examples(inv) if x not in suggestions][:3 - len(suggestions)] if len(suggestions) < 3 else []
    if not parts:
        parts.append("I can't answer that from this workspace on its own.")
        suggestions += _examples(inv)[:2]
    seen: set[str] = set()
    suggestions = [x for x in suggestions if x and not (x in seen or seen.add(x))][:3]
    return {"answer": " ".join(parts), "items": items[:12], "suggestions": suggestions, "notes": notes,
            "kind": kind, "topic": topic}


def counts(inv: dict[str, Any]) -> list[float]:
    """The only numbers a reply may carry: how many of each thing exists."""
    return [float(len(v)) for v in inv.values() if isinstance(v, list)]


def result(question: str, reply: dict[str, Any], *, narrative_by: str) -> dict[str, Any]:
    """The task result for a conversational reply, in the same shape as an analysis so every viewer can show it, with
    nothing claimed that was not done: no evidence, no checks, and a confidence level of 'reply', not 'verified'."""
    return {
        "status": "complete",
        "mode": "conversation",
        "depth": "conversation",
        "audience": "analyst",
        "brief": reply["answer"],
        "question": question,
        "answer": reply["answer"],
        "hypotheses": [],
        "evidence": [],
        "implications": [],
        "actions": list(reply.get("notes") or []),
        "charts": [],
        "tables": [],
        "conversation": {"kind": reply["kind"], "topic": reply["topic"], "items": reply["items"],
                         "suggestions": reply["suggestions"]},
        "confidence": {"level": "reply", "basis": "a conversational reply from the workspace's own catalog; no figures "
                                                  "were computed, so there was nothing to verify", "checks": []},
        "supporting": {"queries": [], "sources": [], "citations": [], "consistency": None, "metric": None,
                       "assumptions": ["Read from the workspace's catalog with your permissions. No figures were computed."],
                       "plan": [{"capability": "converse", "agent": "business_analyst"}], "narrative_by": narrative_by,
                       "expertise": None, "workflow": {"used": None, "strategy": "conversation", "compiled": None}},
    }


__all__ = ["build_suggestion", "compose", "counts", "headline_metrics", "inventory", "result", "route", "topic_of"]
