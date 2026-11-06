# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The Lyst agent bar: one place at the top of every page to talk to Lyst and watch it work.

Collapsed, it is a single line: Lyst's state (a mark that breathes while it works), one field to type or speak into,
and what Lyst is doing right now with a way to **watch** it. Opened (click the chevron, or press ``/`` then ``↓``),
it shows the conversation on the left and **Live** on the right: what is running with the engine's own step count,
what Lyst did in this workspace in the order it happened, each with **Go** to the page and section where it happened,
and **Follow Lyst**, which takes you there as it happens.

What the field understands (:func:`interpret`, pure and tested): "go to SQL", "open definitions", a place's name on
its own ("memory"), "follow Lyst" / "stop following", and anything else is a question for Lyst. Questions go through
the same governed analyst as everywhere else; places are the command palette's (``palette.PLACES``) so the bar and
the palette always agree on where things are.

Voice follows the voice rules (docs/VOICE.md): the microphone starts only when the person presses it and stops when
they stop or pause; recognition runs in the browser and no audio is kept; the transcript appears in the field as
it is heard; each utterance goes to the voice engine (``/voice/sessions/{id}/turns``), which decides what it was.
When the engine was not sure it heard right, nothing starts until the person confirms what it heard. Speech never
approves, confirms or runs an action: the engine refuses it, and the bar says to confirm on screen. Answers are read
aloud only after a spoken question, and only when the person's voice settings allow it.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import task_state

LIVE = 6  # seconds between refreshes while Lyst works or the person follows it
TURNS = 12  # conversation turns kept in this browser session
FEED = 14  # events shown in Live
PAGE_OF = {"": "home", "analyst": "home", "data": "data", "brain": "brain", "settings": "settings", "work": "brain",
           "artifact": "artifact"}
GO = re.compile(r"^(?:please\s+)?(?:go(?: back)? to|open|show(?: me)?|take me to|jump to|navigate to|switch to)\s+"
                r"(?:the\s+)?(.+?)(?:\s+(?:page|section|tab|view))?[.!]?$", re.I)
FOLLOW_ON = re.compile(r"^(?:follow|watch)(?: lyst| the agent| what lyst does| along)?[.!]?$", re.I)
FOLLOW_OFF = re.compile(r"^(?:stop|don'?t|do not)\s+(?:following|follow|watching)(?: lyst| the agent)?[.!]?$", re.I)

_COMPONENT = None


def _component():
    global _COMPONENT
    if _COMPONENT is None:
        from streamlit.components.v2 import component  # noqa: PLC0415

        here = Path(__file__).parent / "agent_bar_component"
        _COMPONENT = component("lyst_agent_bar", css=(here / "bar.css").read_text(encoding="utf-8"),
                               js=(here / "bar.js").read_text(encoding="utf-8"))
    return _COMPONENT


# ------------------------------------------------------------------------------------------- places and words

def places() -> list[dict[str, Any]]:
    """Every place the bar can take a person: the palette's places, by id, with their words."""
    from lyst.ui import palette  # noqa: PLC0415

    out = [{"id": "home", "label": "Home", "area": "Home", "words": "home ask questions answers start",
            "page": "app_pages/analyst.py", "params": {}}]
    for area, section, page, params in palette.PLACES:
        if not section:
            continue
        pid = f"{area}/{section}".lower().replace(" ", "-")
        out.append({"id": pid, "label": f"{area} › {section}", "area": area, "page": page, "params": params,
                    "words": f"{area} {section} {palette.ALIASES.get(section, '')}".lower()})
    for area, page in (("Data", "app_pages/data.py"), ("Brain", "app_pages/brain.py"),
                       ("Settings", "app_pages/settings.py")):
        out.append({"id": area.lower(), "label": area, "area": area, "page": page, "params": {},
                    "words": area.lower()})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def find_place(words: str, where: list[dict] | None = None) -> dict | None:
    """The place a person named: an exact section or area name first, then every word found in its words."""
    where = where or places()
    w = _norm(words)
    if not w:
        return None
    for p in where:  # "sql", "data › sql", "brain memory"
        label = _norm(p["label"].replace("›", " "))
        section = _norm(p["label"].split("›")[-1])
        if w in (label, section, _norm(p["area"]) if "›" not in p["label"] else "\0"):
            return p
    hits = [p for p in where if all(t in p["words"] for t in w.split())]
    return min(hits, key=lambda p: len(p["words"])) if hits else None


def interpret(text: str, where: list[dict] | None = None) -> tuple[str, Any]:
    """What a line typed (or said) into the bar asks for: ('go', place), ('follow', on) or ('ask', question)."""
    t = " ".join((text or "").split())
    if not t:
        return "none", None
    if FOLLOW_OFF.match(t):
        return "follow", False
    if FOLLOW_ON.match(t):
        return "follow", True
    m = GO.match(t)
    if m and (p := find_place(m.group(1), where)):
        return "go", p
    if len(t.split()) <= 3 and (p := find_place(t, where)) and _norm(t) in _norm(p["label"]).split(" ") + [
            _norm(p["label"].replace("›", " ")), _norm(p["label"].split("›")[-1])]:
        return "go", p
    return "ask", t


# ------------------------------------------------------------------------------------------------- live

EVENT_WORDS = {
    "task.created": "Started", "task.completed": "Finished", "task.failed": "Stopped",
    "dataset.version_created": "New data in", "dataset.schema_changed": "Shape changed in",
    "metric.defined": "Defined", "metric.deviated": "Noticed a move in", "workflow.compiled": "Learned to repeat",
    "anticipation.found": "Prepared", "asset.versioned": "Updated", "asset.health_changed": "Checked",
    "activity.opened": "Automatic", "activity.changed": "Automatic", "scenario.defined": "Saved a scenario",
}
ACT_TO = {"awaiting_approval": "needs approval", "running": "working", "completed": "done",
          "failed": "did not finish", "needs_attention": "needs a look", "blocked": "not allowed",
          "suggested": "suggested", "cancelled": "dismissed", "planned": "starting"}


def where(kind: str, oid: str, *, acts: dict[str, dict] | None = None) -> dict | None:
    """The page and section where a thing lives, so Live can take a person there."""
    if kind == "task":
        return {"page": "app_pages/analyst.py", "params": {"task": oid}, "label": "Home"}
    if kind == "dataset":
        return {"page": "app_pages/data.py", "params": {"dataset": oid}, "label": "Data"}
    if kind == "connection":
        return {"page": "app_pages/data.py", "params": {"connection": oid}, "label": "Data › Sources"}
    if kind == "metric":
        return {"page": "app_pages/brain.py", "params": {"view": "definitions", "metric": oid}, "label": "Brain"}
    if kind in ("asset", "workflow", "scenario"):
        return {"page": "app_pages/data.py", "params": {"view": "saved analyses"}, "label": "Data › Saved analyses"}
    if kind == "activity" and acts and oid in acts:
        a = acts[oid]
        return where(a.get("subject_kind") or "", a.get("subject_id") or "", acts=None) or {
            "page": "app_pages/brain.py", "params": {"view": "work", "work": "activity"}, "label": "Brain › Work"}
    if kind in ("monitor", "finding"):
        return {"page": "app_pages/brain.py", "params": {"view": "work", "work": "recurring"}, "label": "Brain › Work"}
    if kind == "rule":
        return {"page": "app_pages/settings.py", "params": {"view": "workspace"}, "label": "Settings"}
    return None


def feed(events: list[dict], *, titles: dict[str, str], acts: dict[str, dict]) -> list[dict[str, Any]]:
    """Recent events as lines a person reads: what Lyst did, to what, when, and where to see it. Newest first."""
    out = []
    for e in sorted(events, key=lambda e: -int(e.get("seq", 0))):
        t = e.get("type", "")
        if t not in EVENT_WORDS:
            continue
        subj = e.get("subject") or {}
        kind, oid = subj.get("kind", ""), str(subj.get("id", ""))
        d = e.get("data") or {}
        verb = EVENT_WORDS[t]
        tone = "info"
        if t == "task.completed":
            verb, tone = {"needs_input": ("Needs your input", "warn"), "unverified": ("Not verified", "bad")}.get(
                d.get("outcome"), ("Finished", "ok"))
        elif t == "task.failed":
            tone = "bad"
        elif t.startswith("activity."):
            to = d.get("to") or "suggested"
            verb = f"Automatic · {ACT_TO.get(to, to.replace('_', ' '))}"
            tone = {"awaiting_approval": "warn", "completed": "ok", "failed": "bad", "blocked": "bad",
                    "running": "run"}.get(to, "info")
        elif t == "task.created":
            tone = "run"
        a = acts.get(oid) if kind == "activity" else None
        what = titles.get(oid) or (f"{a.get('subject_title') or a.get('subject_id')}" if a else
                                   oid.replace("_", " ") if kind in ("dataset", "metric") else kind.replace("_", " "))
        out.append({"id": str(e.get("seq")), "verb": verb, "what": what, "tone": tone,
                    "when": kit.when(e.get("occurred_at")), "go": where(kind, oid, acts=acts), "kind": kind})
        if len(out) >= FEED:
            break
    return out


def _events(ws: str) -> list[dict]:
    hit = st.session_state.get("_bar_events")
    if hit and hit[0] == ws and time.monotonic() - hit[1] < LIVE - 1:
        return hit[2]
    api = client()
    try:
        head = int(api.get(f"/v1/workspaces/{ws}/events", after=-1)["next"])
        evs = api.get(f"/v1/workspaces/{ws}/events", after=max(0, head - 60), limit=100).get("events") or []
    except (APIError, KeyError, TypeError, ValueError):
        evs = []
    st.session_state["_bar_events"] = (ws, time.monotonic(), evs)
    return evs


# ------------------------------------------------------------------------------------------------- state

def _turns() -> list[dict]:
    return st.session_state.setdefault("_bar_turns", [])


def _remember(q: str, *, via: str, task: str | None = None, note: str | None = None, reply: str | None = None) -> None:
    turns = _turns()
    turns.append({"id": f"{time.time():.3f}", "q": q, "via": via, "task": task, "note": note, "reply": reply,
                  "at": time.time()})
    del turns[:-TURNS]


def conversation(tasks: dict[str, dict]) -> list[dict[str, Any]]:
    """This session's turns, oldest first, each with what became of it (the engine's status, the answer's brief)."""
    from lyst.ui import artifacts  # noqa: PLC0415

    out = []
    for t in _turns():
        item = {"id": t["id"], "q": t["q"], "via": t["via"], "note": t.get("note"), "reply": t.get("reply"),
                "when": kit.when(time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t["at"])))}
        if t.get("task"):
            rec = tasks.get(t["task"]) or {}
            status = rec.get("status") or "queued"
            words, color, _ = task_state(status, rec.get("outcome"))
            item |= {"task": t["task"], "status": words,
                     "tone": {"green": "ok", "orange": "warn", "red": "bad", "blue": "run"}.get(color, "info"),
                     "done": status == "done"}
            if status == "done":
                r = artifacts.result(t["task"]) or {}
                item["answer"] = str(r.get("brief") or r.get("answer") or "")[:420]
                if r.get("mode") == "conversation":  # a reply, not an analysis: no Done badge, and its offers to act on
                    talk = r.get("conversation") or {}
                    item |= {"status": None, "talk": True, "answer": str(r.get("answer") or "")[:700],
                             "suggest": [str(x)[:200] for x in (talk.get("suggestions") or [])][:3]}
        out.append(item)
    return out


def payload(page: str) -> dict[str, Any]:
    from lyst.ui import activity, agent, zone  # noqa: PLC0415

    ws = workspace()
    s = agent.state(ws)
    need = agent.waiting(s)
    tone, head, parts = agent.summary(s, need)
    tasks = {t["id"]: t for t in s["tasks"]}
    acts = {a["id"]: a for a in (activity.listed() or [])}
    try:
        cards, _ = zone.collect(ws)
    except APIError:
        cards = []
    now = []
    for c in [c for c in cards if c["col"] == "working"][:4]:
        kind, oid = c["id"].split(":", 1)
        rec = tasks.get(oid) if kind == "t" else acts.get(oid)
        go = where("task", oid) if kind == "t" else where("activity", oid, acts=acts)
        now.append({"id": c["id"], "title": c["title"], "status": c["status"], "progress": c.get("progress"),
                    "progressText": c.get("progressText"), "go": go, "kind": c["kindLabel"],
                    "when": c["when"], "task": oid if kind == "t" else (rec or {}).get("task_id")})
    heard = st.session_state.get("voice_confirm")
    say = st.session_state.pop("voice_say", None)
    return {
        "page": page, "tone": tone, "head": head, "parts": parts, "need": need,
        "now": now, "feed": feed(_events(ws), titles={k: v.get("title") or "" for k, v in tasks.items()}, acts=acts),
        "turns": conversation(tasks), "places": [{k: p[k] for k in ("id", "label", "area", "words")} for p in places()],
        "follow": bool(st.session_state.get("_bar_follow")), "heard": heard.get("text") if heard else None,
        "voice": _voice_ok(), "say": say, "open": bool(st.session_state.pop("_bar_open", False)),
        "role": st.session_state.get("role"),
    }


def _voice_ok() -> bool:
    """Whether the mic is offered. The voice session itself starts on the first utterance, not on every page."""
    return st.session_state.get(f"voice_sid_{workspace()}") != "denied"


# ------------------------------------------------------------------------------------------------- acting

def _go(target: dict | None) -> None:
    if target and target.get("page"):
        st.switch_page(target["page"], query_params=target.get("params") or {})


def _ask(q: str, page: str, *, via: str = "typed") -> None:
    api, ws = client(), workspace()
    out = kit.guarded(lambda: api.ask(ws, q))
    if not out:
        return
    _remember(q, via=via, task=out.get("task_id"))
    st.toast("Lyst is on it", icon=":material/bolt:")
    if page == "home":  # Home is where answers are read: open it there, as the ask box did
        st.session_state.task_id = out["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})
    st.session_state["_bar_open"] = True


def _voice(text: str, confidence: float | None, page: str) -> None:
    """One utterance through the voice engine, which decides what it was. Nothing it refuses is done here either."""
    from lyst.ui import voice  # noqa: PLC0415

    sid = voice._session()
    if not sid:
        st.toast("Voice is not available in this workspace; type instead.", icon=":material/mic_off:")
        return
    kind, arg = interpret(text)
    if kind in ("go", "follow"):  # moving around the app is not an action on data: voice may do it
        _remember(text, via="voice", note=f"Going to {arg['label']}" if kind == "go" else
                  ("Following Lyst" if arg else "Stopped following"))
        if kind == "go":
            _go(arg)
        st.session_state["_bar_follow"] = bool(arg) if kind == "follow" else st.session_state.get("_bar_follow")
        return
    body = {"text": text} | ({"confidence": confidence} if confidence else {})
    r = voice._turn(sid, body)
    if r is None:
        return
    reply = (r.get("reply") or {}).get("text")
    heard = r.get("confirm_transcript") or {}
    intent = r.get("intent")
    if (r.get("analyst") or {}).get("status") == "needs_confirmation" and heard.get("text"):
        st.session_state["voice_confirm"] = {"sid": st.session_state.get(f"voice_sid_{workspace()}") or sid,
                                             "text": heard["text"]}
        _remember(text, via="voice", note="Not sure I heard that right")
    elif intent == "question" and r.get("task_id"):
        _remember(r.get("understood") or text, via="voice", task=r["task_id"])
        st.session_state["voice_wait"] = {"sid": st.session_state.get(f"voice_sid_{workspace()}") or sid,
                                          "task": r["task_id"]}
        if page == "home":
            st.session_state.task_id = r["task_id"]
            st.switch_page("app_pages/analyst.py", query_params={"task": r["task_id"]})
    elif intent in ("action", "assent"):
        _remember(text, via="voice", note="Voice can't approve or run this. Confirm it on screen.")
    else:
        _remember(text, via="voice", reply=reply)
    if reply and (r.get("reply") or {}).get("speak"):
        st.session_state["voice_say"] = {"text": reply, "id": f"{sid}:{time.time():.0f}"}
    st.session_state["_bar_open"] = True


def handle(ev: dict, page: str) -> None:
    kind = ev.get("type")
    if kind == "submit":
        what, arg = interpret(str(ev.get("text") or ""))
        if what == "go":
            _remember(str(ev.get("text")), via="typed", note=f"Going to {arg['label']}")
            _go(arg)
        elif what == "follow":
            st.session_state["_bar_follow"] = arg
            _remember(str(ev.get("text")), via="typed", note="Following Lyst" if arg else "Stopped following")
            st.session_state["_bar_open"] = True
        elif what == "ask":
            _ask(arg, page)
    elif kind == "place":
        p = next((p for p in places() if p["id"] == ev.get("id")), None)
        _go(p)
    elif kind == "voice":
        text = " ".join(str(ev.get("text") or "").split())[:500]
        if text:
            conf = ev.get("confidence")
            _voice(text, float(conf) if isinstance(conf, int | float) else None, page)
    elif kind == "heard":
        c = st.session_state.pop("voice_confirm", None)
        if c and ev.get("yes"):
            from lyst.ui import voice  # noqa: PLC0415

            r = voice._turn(c["sid"], {"text": c["text"], "confidence": 1})
            if r and r.get("task_id"):
                _remember(c["text"], via="voice", task=r["task_id"])
                st.session_state["voice_wait"] = {"sid": c["sid"], "task": r["task_id"]}
        st.session_state["_bar_open"] = True
    elif kind == "follow":
        st.session_state["_bar_follow"] = bool(ev.get("on"))
        st.session_state["_bar_open"] = True
    elif kind == "go":
        _go(ev.get("go") if isinstance(ev.get("go"), dict) else None)
    elif kind == "open" and ev.get("task"):
        st.session_state.task_id = str(ev["task"])
        st.switch_page("app_pages/analyst.py", query_params={"task": str(ev["task"])})
    elif kind == "clear":
        st.session_state["_bar_turns"] = []


def _follow(page: str, data: dict) -> None:
    """Follow Lyst: when something new happens somewhere else, go there. At most once every 12 seconds."""
    if not data["follow"] or not data["feed"]:
        return
    top = data["feed"][0]
    seen = st.session_state.get("_bar_seen")
    st.session_state["_bar_seen"] = top["id"]
    if seen is None or seen == top["id"] or not top.get("go"):
        return
    if time.monotonic() - st.session_state.get("_bar_moved", 0) < 12:
        return
    target = top["go"]
    if PAGE_OF.get(Path(target["page"]).stem.replace("analyst", "")) == page and not target.get("params"):
        return
    st.session_state["_bar_moved"] = time.monotonic()
    st.toast(f"Following Lyst: {top['verb']} {top['what']}", icon=":material/visibility:")
    _go(target)


def _draw(page: str) -> None:
    data = payload(page)
    res = _component()(data=data, key="lyst_agent_bar", on_action_change=lambda: None)
    ev = getattr(res, "action", None)
    if isinstance(ev, dict) and ev.get("nonce") != st.session_state.get("_bar_nonce"):
        st.session_state["_bar_nonce"] = ev.get("nonce")
        st.session_state["_bar_do"] = ev
        st.rerun()
    _follow(page, data)


@st.fragment(run_every=LIVE)
def _live(page: str) -> None:
    _draw(page)


@st.fragment
def _still(page: str) -> None:
    _draw(page)


def bar(page: str) -> None:
    """The agent bar, at the top of every page. Gestures are handled here, before the page draws, so a question
    asked from any page is started and followed without leaving it."""
    from lyst.ui import agent, voice  # noqa: PLC0415

    ev = st.session_state.pop("_bar_do", None)
    if ev:
        handle(ev, page)
    if w := st.session_state.get("voice_wait"):  # a spoken question: say its answer once it is verified
        voice.read_back(w["task"])
    try:
        s = agent.state(workspace())
    except APIError:
        return
    busy = any(t["status"] in agent.ACTIVE for t in s["tasks"]) or bool(st.session_state.get("_bar_follow")) or \
        bool(st.session_state.get("voice_wait"))
    (_live if busy else _still)(page)


CSS = """
/* the agent bar stays at the top of the page while it scrolls */
div.st-key-lyst_agent_bar {position: sticky; top: .65rem; z-index: 60;}
@media (max-width: 640px) {div.st-key-lyst_agent_bar {top: 3.4rem;}}
"""

__all__ = ["bar", "conversation", "feed", "find_place", "handle", "interpret", "places", "where"]
