# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The activity zone: one Kanban board, the same on every page, of everything Lyst is doing and wants to do.

Four columns, always in the same order: **Suggested** (what Lyst would do next, if you agree), **Needs you**
(approvals, questions, things that did not finish), **In progress** and **Done** (the last seven days). Each page
opens on its own cards (Home: your questions; Data: datasets and sources; Brain: metrics, monitors and schedules;
Settings: rules and skills), and one switch shows everything.

How a person acts on a card, all leading to the same small set of operations:

* **click** opens it where it lives (the answer, the activity, the approval);
* **drag** it to another column: each column says what dropping there does ("Drop to approve"), and columns a card
  cannot move to stand back;
* **press and hold** (or the ⋯ button, or M on the keyboard) opens its actions;
* **keyboard**: arrows move between cards, Space picks a card up, arrows choose a column, Space drops it.

Nothing runs from a gesture alone. A move or an action that changes anything opens a confirmation that says what will
happen and how it is governed; only Confirm calls the API, and the API keeps its own rules (an approval still needs an
admin, a proposer still cannot approve their own proposal, a rule that needs approval still waits for it). Opening a
card changes nothing, so it needs no confirmation.

The board is a ``st.components.v2`` component (``zone_component``): no iframe, no library and no network. It sits in
the page itself, so it is drawn with the page's own design tokens (``--lyst-*``) in light and dark. Every card is
built here from engine records (:func:`build`, pure and tested); the component only draws them and reports gestures.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import task_state

TTL = 15  # seconds the board's reads are reused; kit.guarded and live updates drop them at once
LIVE = 8  # seconds between refreshes while something is in progress
DONE_DAYS = 7
PAGES = {"home": "Home", "data": "Data", "brain": "Brain", "settings": "Settings"}
PAGE_FILES = {"home": "app_pages/analyst.py", "data": "app_pages/data.py", "brain": "app_pages/brain.py",
              "settings": "app_pages/settings.py"}
# (id, title, what the column holds, tone): the order never changes, so a card's place means the same everywhere
COLUMNS = (
    ("suggested", "Suggested", "What Lyst would do next, if you agree", "accent"),
    ("needs", "Needs you", "Approvals, questions and anything that did not finish", "warn"),
    ("working", "In progress", "What Lyst is working on now", "info"),
    ("done", "Done", f"Finished in the last {DONE_DAYS} days", "good"),
)
EMPTY = {"suggested": "No suggestions right now", "needs": "Nothing waits on you", "working": "Nothing running",
         "done": "Nothing finished this week"}

# Every operation a card can offer. confirm: it changes something, so a person confirms it first. effect: what
# happens, in words. rule: how it is governed, shown beside Confirm. danger: it stops or discards something.
OPS: dict[str, dict[str, Any]] = {
    "open": {"label": "Open", "icon": "open", "confirm": False},
    "details": {"label": "Details", "icon": "info", "confirm": False},
    "answer": {"label": "Answer", "icon": "reply", "confirm": False},
    "why": {"label": "Why Lyst suggests it", "icon": "info", "confirm": False},
    "go": {"label": "Go to", "icon": "arrow", "confirm": False},
    "task.pause": {"label": "Pause", "icon": "pause", "confirm": True,
                   "effect": "Lyst stops after its current step and keeps everything it has done so far.",
                   "rule": "You can resume it at any time."},
    "task.resume": {"label": "Resume", "icon": "play", "confirm": True,
                    "effect": "Lyst carries on from the step where it stopped.",
                    "rule": "It runs with the same permissions and budget as before."},
    "task.retry": {"label": "Try again", "icon": "refresh", "confirm": True,
                   "effect": "Lyst runs this work again from the start.",
                   "rule": "It runs with your permissions and counts toward the workspace's budget."},
    "task.cancel": {"label": "Cancel", "icon": "close", "confirm": True, "danger": True,
                    "effect": "Lyst stops this work. What it finished so far is kept in its history.",
                    "rule": "Cancelled work cannot be resumed; you can ask the question again."},
    "task.again": {"label": "Ask again", "icon": "refresh", "confirm": True,
                   "effect": "Lyst asks the same question again on today's data. The earlier answer is kept.",
                   "rule": "It runs with your permissions and counts toward the workspace's budget."},
    "act.run": {"label": "Do it", "icon": "play", "confirm": True,
                "effect": "Lyst starts this activity now.",
                "rule": "If its rule needs an approval, it waits for an admin first; nothing runs without one."},
    "act.retry": {"label": "Try again", "icon": "refresh", "confirm": True,
                  "effect": "Lyst runs this activity again.",
                  "rule": "If its rule needs an approval, it waits for an admin first; nothing runs without one."},
    "act.dismiss": {"label": "Dismiss", "icon": "close", "confirm": True, "danger": True,
                    "effect": "Lyst drops this activity. If it was running, its work is cancelled.",
                    "rule": "Dismissing is recorded in the audit trail."},
    "approve": {"label": "Approve", "icon": "check", "confirm": True,
                "effect": "What was proposed is allowed to run.",
                "rule": "You approve as yourself, and the approval is recorded in the audit trail. Lyst never "
                        "approves anything on its own."},
    "reject": {"label": "Reject", "icon": "close", "confirm": True, "danger": True,
               "effect": "What was proposed does not run.",
               "rule": "The rejection is recorded in the audit trail."},
    "finding.seen": {"label": "Mark as seen", "icon": "check", "confirm": True,
                     "effect": "Lyst marks this finding as seen and keeps it in Brain › Work.",
                     "rule": "Only your view of it changes."},
    "finding.dismiss": {"label": "Dismiss", "icon": "close", "confirm": True, "danger": True,
                        "effect": "Lyst hides this finding.",
                        "rule": "The monitor keeps watching; a new move raises a new finding."},
    "sugg.run": {"label": "Do it", "icon": "play", "confirm": True,
                 "effect": "Lyst does what it suggested now.",
                 "rule": "It runs with your permissions. Anything that needs an approval waits for one."},
    "sugg.later": {"label": "Later", "icon": "clock", "confirm": True,
                   "effect": "Lyst hides this suggestion until tomorrow.", "rule": "Nothing runs."},
    "sugg.dismiss": {"label": "Dismiss", "icon": "close", "confirm": True, "danger": True,
                     "effect": "Lyst drops this suggestion and suggests less like it.", "rule": "Nothing runs."},
}
ACTIVITY_WORDS = {"notify": "Tell the owners", "impact_review": "Review what depends on it", "investigate": "Investigate",
                  "retry_task": "Retry once", "propose": "Propose a change"}
APPROVAL_WORDS = {"schedule.activate": "Start a scheduled question", "monitor.activate": "Start watching a metric",
                  "skill.register": "Add a new skill", "skill.approve": "Approve a skill",
                  "decision.approve": "Approve a decision", "rule.activate": "Turn on an event rule",
                  "activity.execute": "Run work Lyst prepared", "harness.update": "Change a harness rule"}
# which page an object belongs to, by the kind of thing it is about
SUBJECT_PAGE = {"dataset": "data", "connection": "data", "source": "data", "asset": "data", "dashboard": "data",
                "transform": "data", "credential": "settings", "rule": "settings", "skill": "settings",
                "workspace": "settings", "metric": "brain", "term": "brain", "schedule": "brain", "monitor": "brain",
                "document": "brain", "memory": "brain", "task": "home"}
APPROVAL_PAGE = {"schedule.activate": "brain", "monitor.activate": "brain", "decision.approve": "brain",
                 "harness.update": "brain", "rule.activate": "settings", "skill.register": "settings",
                 "skill.approve": "settings", "activity.execute": "data"}

_COMPONENT = None


def _component():
    """Register the board once per process (the component's code is read from zone_component/)."""
    global _COMPONENT
    if _COMPONENT is None:
        here = Path(__file__).parent / "zone_component"
        from streamlit.components.v2 import component  # noqa: PLC0415

        _COMPONENT = component(
            "lyst_activity_zone", css=(here / "zone.css").read_text(encoding="utf-8"),
            js=(here / "zone.js").read_text(encoding="utf-8"))
    return _COMPONENT


# ----------------------------------------------------------------------------------------------- cards (pure)

def _ts(v: Any) -> float:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T", 1))
    except (TypeError, ValueError):
        return 0.0
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def _op(op: str, to: str | None = None, *, primary: bool = False, label: str | None = None) -> dict[str, Any]:
    spec = OPS[op]
    out = {"id": op, "label": label or spec["label"], "icon": spec["icon"], "confirm": bool(spec["confirm"])}
    if to:
        out["to"] = to
    if primary:
        out["primary"] = True
    if spec.get("danger"):
        out["danger"] = True
    return out


def _card(cid: str, *, kind: str, kind_label: str, title: str, col: str, status: str, tone: str, page: str,
          when: Any, ops: list[dict], meta: str = "", pages: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    return {"id": cid, "kind": kind, "kindLabel": kind_label, "title": (title or "Untitled").strip(), "col": col,
            "status": status, "tone": tone, "page": page, "pages": sorted({page, *pages}), "when": kit.when(when),
            "at": _ts(when), "meta": meta, "ops": ops, **extra}


def task_card(t: dict, *, editor: bool, progress: tuple[int, int] | None = None) -> dict[str, Any] | None:
    status, outcome = t["status"], t.get("outcome")
    if status == "cancelled":
        return None
    text, color, _ = task_state(status, outcome)
    lc = t.get("lifecycle") or {}
    kind = t.get("kind") or "analysis"
    label = {"investigation": "Investigation", "scheduled": "Scheduled", "workflow": "Repeated analysis"}.get(kind,
                                                                                                          "Analysis")
    page = "brain" if kind in ("investigation", "scheduled", "workflow") else "home"
    ops: list[dict] = []
    if status in ("queued", "running"):
        col, tone = "working", "run"
        text = lc.get("detail") if lc.get("detail") and lc.get("state") not in (None, "Completed") else text
        if editor:
            ops += [_op("task.pause", "needs"), _op("task.cancel")]
        ops.insert(0, _op("details", primary=True, label="Follow it"))
    elif status in ("paused", "awaiting_approval") or outcome in ("needs_input", "unverified"):
        col = "needs"
        tone = "bad" if outcome == "unverified" else "wait"
        if status == "paused":
            ops += [_op("task.resume", "working", primary=True)] if editor else []
            ops += [_op("task.cancel")] if editor else []
        elif outcome == "needs_input":
            ops += [_op("answer", "working", primary=True)]
        else:
            ops += [_op("open", primary=True)]
            ops += [_op("task.again", "working")] if editor else []
    elif status == "failed":
        col, tone, text = "needs", "bad", "Did not finish"
        ops += [_op("task.retry", "working", primary=True)] if editor else []
        ops += [_op("details")]
    else:  # done and verified
        col, tone = "done", "ok"
        ops += [_op("open", primary=True, label="Open the answer")]
        ops += [_op("task.again", "working")] if editor and kind == "analysis" else []
    if not any(o["id"] in ("open", "details") for o in ops):
        ops.append(_op("details"))
    extra: dict[str, Any] = {"open": "open" if status == "done" else "details"}
    if progress and progress[1]:
        extra |= {"progress": round(progress[0] / progress[1], 3), "progressText": f"{progress[0]} of {progress[1]} steps"}
    if col == "working":
        extra["live"] = True
    return _card(f"t:{t['id']}", kind="task", kind_label=label, title=t.get("title") or "", col=col, status=text,
                 tone=tone, page=page, when=t.get("updated_at") if col == "done" else t.get("created_at"), ops=ops,
                 question=((t.get("request") or {}).get("question") if isinstance(t.get("request"), dict) else None)
                 or t.get("title"), **extra)


ACT_STATUS = {"observed": ("Noted", "info"), "suggested": ("Suggestion", "info"), "planned": ("Starting", "run"),
              "running": ("Working on it", "run"), "awaiting_approval": ("Needs approval", "wait"),
              "completed": ("Done", "ok"), "needs_attention": ("Needs a look", "warn"),
              "failed": ("Did not finish", "bad"), "blocked": ("Not allowed", "bad")}


def activity_card(a: dict, *, editor: bool, admin: bool) -> dict[str, Any] | None:
    s = a["status"]
    if s not in ACT_STATUS:
        return None
    words, tone = ACT_STATUS[s]
    col = {"observed": "suggested", "suggested": "suggested", "planned": "working", "running": "working",
           "completed": "done"}.get(s, "needs")
    ops: list[dict] = []
    if s == "suggested" and editor:
        ops += [_op("act.run", "working", primary=True), _op("act.dismiss", "done")]
    elif s == "awaiting_approval":
        if admin and a.get("approval_id"):
            ops += [_op("approve", "working", primary=True), _op("reject", "done")]
        else:
            words = "Waiting for an admin"
    elif s in ("failed", "blocked") and editor:
        ops += [_op("act.retry", "working", primary=True), _op("act.dismiss", "done")]
    elif s in ("needs_attention", "observed", "planned", "running") and editor:
        ops += [_op("act.dismiss", "done")]
    ops.append(_op("details", primary=not any(o.get("primary") for o in ops)))
    page = SUBJECT_PAGE.get(a.get("subject_kind") or "", "brain")
    title = f"{ACTIVITY_WORDS.get(a['action'], str(a['action']).replace('_', ' ').capitalize())} · " \
            f"{a.get('subject_title') or a.get('subject_id')}"
    return _card(f"a:{a['id']}", kind="activity", kind_label="Automatic", title=title, col=col, status=words,
                 tone=tone, page=page, when=a.get("updated_at") if col == "done" else a.get("created_at"), ops=ops,
                 meta=str(a.get("explanation") or "")[:220], open="details", live=col == "working",
                 approval=a.get("approval_id"))


def approval_card(ap: dict) -> dict[str, Any]:
    page = "home" if ap.get("task_id") else APPROVAL_PAGE.get(ap.get("action") or "", "settings")
    what = APPROVAL_WORDS.get(ap.get("action") or "", str(ap.get("action") or "").replace(".", " ").capitalize())
    return _card(f"ap:{ap['id']}", kind="approval", kind_label="Approval", title=ap.get("summary") or what,
                 col="needs", status="Needs your approval", tone="wait", page=page, when=ap.get("created_at"),
                 ops=[_op("approve", "working", primary=True), _op("reject", "done"), _op("details")],
                 meta=what, open="details")


def finding_card(f: dict, *, editor: bool) -> dict[str, Any] | None:
    if f.get("status") in ("dismissed", "acknowledged") or str(f.get("monitor_id") or "").startswith("rule:"):
        return None  # a rule's finding is the activity card that raised it
    risk = f.get("direction") == "risk"
    ops = [_op("open", primary=True, label="See why" if f.get("task_id") else "Details")]
    if editor:
        ops += [_op("finding.seen", "done"), _op("finding.dismiss")]
    return _card(f"f:{f['id']}", kind="finding", kind_label="Lyst noticed", title=f.get("summary") or "",
                 col="needs" if risk else "suggested", status="Risk" if risk else "Opportunity",
                 tone="bad" if risk else "ok", page="brain", pages=("home",), when=f.get("created_at"), ops=ops,
                 meta=" · ".join(x for x in (str(f.get("metric") or "").replace("_", " ").capitalize(), f.get("label"))
                                 if x), open="open")


def suggestion_card(a: dict, *, editor: bool, wants_person: bool) -> dict[str, Any] | None:
    acts = [x["id"] if isinstance(x, dict) else x for x in a.get("actions") or []]
    labels = {x["id"]: x.get("label") for x in a.get("actions") or [] if isinstance(x, dict)}
    sk = a.get("subject_kind") or ""
    page = "home" if sk == "task" else SUBJECT_PAGE.get(sk, "brain")
    if a.get("kind") == "capability_gap":
        page = "settings"
    verdict = {"ready": ("Ready", "ok"), "blocked": ("Blocked", "bad"), "warn": ("Check first", "warn"),
               "risk": ("At risk", "bad"), "watch": ("Keep an eye on it", "warn"), "warning": ("Check first", "warn"),
               "stale": ("Out of date", "warn")}.get(a.get("verdict") or "", ("", "info"))  # the kicker says it
    ops: list[dict] = []
    if "run_now" in acts and editor:
        ops.append(_op("sugg.run", "working", primary=True, label=labels.get("run_now") or "Do it"))
    ops.append(_op("why", primary=not ops))
    if editor:
        ops += [_op("sugg.later"), _op("sugg.dismiss", "done")]
    label = {"next_best_action": "Next step", "health_foresight": "Heads-up", "early_warning": "Early warning",
             "capability_gap": "Missing skill"}.get(a.get("kind") or "", "Prepared")
    return _card(f"n:{a['id']}", kind="suggestion", kind_label=label, title=a.get("objective") or "",
                 col="needs" if wants_person else "suggested", status=verdict[0], tone=verdict[1], page=page,
                 when=a.get("created_at"), ops=ops, meta=((a.get("reasons") or [""])[0] or "")[:220], open="why",
                 rank=float(a.get("score") or 0))


def _wants_person(a: dict) -> bool:
    from lyst.ui import anticipate  # noqa: PLC0415

    return anticipate._wants_person(a)


def build(*, tasks: list[dict], activities: list[dict], approvals: list[dict], findings: list[dict],
          suggestions: list[dict], role: str | None, progress: dict[str, tuple[int, int]] | None = None,
          now: float | None = None) -> list[dict[str, Any]]:
    """Every card for a workspace, from engine records: no estimate, no state the engine did not record.
    Approvals behind an activity are shown on the activity's own card, not twice."""
    editor = role in ("editor", "admin", "owner")
    admin = role in ("admin", "owner")
    now = now or time.time()
    cutoff = now - DONE_DAYS * 86400
    cards: list[dict] = []
    # work an automatic activity started is shown once, on the activity's card, with the work's progress
    owned = {a["task_id"]: a["id"] for a in activities or [] if a.get("task_id") and a["status"] in ("planned", "running")}
    for t in tasks[:60]:
        if t["id"] in owned:
            continue
        c = task_card(t, editor=editor, progress=(progress or {}).get(t["id"]))
        if c:
            cards.append(c)
    covered = set()
    for a in activities or []:
        c = activity_card(a, editor=editor, admin=admin)
        if c:
            done_of = (progress or {}).get(a.get("task_id") or "")
            if done_of and done_of[1] and c["col"] == "working":
                c |= {"progress": round(done_of[0] / done_of[1], 3), "progressText": f"{done_of[0]} of {done_of[1]} steps"}
            cards.append(c)
            if a.get("approval_id"):
                covered.add(a["approval_id"])
    for ap in approvals if admin else []:
        if ap.get("status", "pending") == "pending" and ap["id"] not in covered:
            cards.append(approval_card(ap))
    for f in findings:
        c = finding_card(f, editor=editor)
        if c:
            cards.append(c)
    for a in suggestions:
        c = suggestion_card(a, editor=editor, wants_person=_wants_person(a))
        if c:
            cards.append(c)
    cards = [c for c in cards if c["col"] != "done" or c["at"] >= cutoff]
    order = {"approval": 0, "activity": 1, "task": 2, "finding": 3, "suggestion": 4}

    def rank(c: dict) -> tuple:
        if c["col"] == "suggested":
            return (-(c.get("rank") or 0), -c["at"])
        if c["col"] == "needs":
            return (order.get(c["kind"], 9), -c["at"])
        return (-c["at"],)

    return sorted(cards, key=lambda c: ([x[0] for x in COLUMNS].index(c["col"]), rank(c)))


# ------------------------------------------------------------------------------------------------ reads

def _progress(api, tasks: list[dict]) -> dict[str, tuple[int, int]]:
    out = {}
    for t in [t for t in tasks if t["status"] in ("running", "paused")][:4]:
        try:
            sts = (api.get(f"/v1/tasks/{t['id']}") or {}).get("steps") or []
        except APIError:
            continue
        if sts:
            out[t["id"]] = (sum(s["status"] == "done" for s in sts), len(sts))
    return out


def collect(ws: str) -> tuple[list[dict], dict[str, dict]]:
    """(cards, the record behind each card by id), read at most every TTL seconds per workspace."""
    hit = st.session_state.get("_zone")
    if hit and hit[0] == ws and time.monotonic() - hit[1] < TTL:
        return hit[2], hit[3]
    from lyst.ui import activity, agent, anticipate  # noqa: PLC0415

    api = client()
    role = st.session_state.get("role")
    tasks = agent.state(ws)["tasks"]

    def get(path: str, **params: Any) -> list[dict]:
        try:
            return api.get(f"/v1/workspaces/{ws}/{path}", **params) or []
        except APIError:
            return []

    acts = activity.listed() or []
    aps = get("approvals") if role in ("admin", "owner") else []
    found = get("findings")
    sugg = anticipate.items()
    cards = build(tasks=tasks, activities=acts, approvals=aps, findings=found, suggestions=sugg, role=role,
                  progress=_progress(api, tasks))
    raw: dict[str, dict] = {}
    for prefix, rows in (("t", tasks), ("a", acts), ("ap", aps), ("f", found), ("n", sugg)):
        for r in rows:
            raw[f"{prefix}:{r['id']}"] = r
    st.session_state["_zone"] = (ws, time.monotonic(), cards, raw)
    return cards, raw


def drop() -> None:
    st.session_state.pop("_zone", None)


# ------------------------------------------------------------------------------------------------ acting

def _find(cid: str) -> tuple[dict | None, dict | None]:
    cards, raw = collect(workspace())
    return next((c for c in cards if c["id"] == cid), None), raw.get(cid)


def _col_title(col: str | None) -> str:
    return next((t for c, t, *_ in COLUMNS if c == col), "")


def _call(card: dict, rec: dict, op: str) -> bool:
    """Do one confirmed operation through the API. Returns True when it went through (kit.guarded shows why not)."""
    api, ws = client(), workspace()
    rid = rec["id"]
    kind = card["kind"]
    calls = {
        "task.pause": lambda: api.post(f"/v1/tasks/{rid}/pause"),
        "task.resume": lambda: api.post(f"/v1/tasks/{rid}/resume"),
        "task.retry": lambda: api.post(f"/v1/tasks/{rid}/retry"),
        "task.cancel": lambda: api.post(f"/v1/tasks/{rid}/cancel"),
        "task.again": lambda: api.ask(ws, card.get("question") or card["title"]),
        "act.run": lambda: api.post(f"/v1/workspaces/{ws}/activities/{rid}/run"),
        "act.retry": lambda: api.post(f"/v1/workspaces/{ws}/activities/{rid}/run"),
        "act.dismiss": lambda: api.post(f"/v1/workspaces/{ws}/activities/{rid}/dismiss"),
        "finding.seen": lambda: api.post(f"/v1/workspaces/{ws}/findings/{rid}", {"status": "acknowledged"}),
        "finding.dismiss": lambda: api.post(f"/v1/workspaces/{ws}/findings/{rid}", {"status": "dismissed"}),
    }
    if op in ("approve", "reject"):
        aid = rec.get("approval_id") if kind == "activity" else rid
        fn = lambda: api.post(f"/v1/approvals/{aid}", {"approve": op == "approve"})  # noqa: E731
    elif op in ("sugg.run", "sugg.later", "sugg.dismiss"):
        from lyst.ui import anticipate  # noqa: PLC0415

        anticipate._act(rec, {"sugg.run": "run_now", "sugg.later": "snooze", "sugg.dismiss": "dismiss"}[op])
        drop()
        return True
    else:
        fn = calls[op]
    out = kit.guarded(fn, {"approve": "Approved", "reject": "Rejected", "task.cancel": "Cancelled",
                           "act.dismiss": "Dismissed", "finding.dismiss": "Dismissed", "finding.seen": "Marked as seen",
                           "task.pause": "Paused", "task.resume": "Resumed"}.get(op, "Started"))
    if out is None:
        return False
    drop()
    from lyst.ui import activity  # noqa: PLC0415

    activity._drop()
    if op == "task.again" and out.get("task_id"):
        st.session_state.task_id = out["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})
    return True


def _open(card: dict, rec: dict, op: str) -> None:
    """Open a card where it lives. Nothing changes, so nothing is confirmed."""
    from lyst.ui import activity, anticipate, work  # noqa: PLC0415

    kind = card["kind"]
    if kind == "task":
        if op in ("open", "answer") and rec["status"] == "done":
            work.open_in_analyst(rec["id"])
        else:
            work.details(rec["id"])
    elif kind == "activity":
        activity.details(rec["id"])
    elif kind == "finding":
        if rec.get("task_id"):
            kit.guarded(lambda: client().post(f"/v1/workspaces/{workspace()}/findings/{rec['id']}",
                                              {"status": "acknowledged"}))
            drop()
            work.open_in_analyst(rec["task_id"])
        else:
            work.go(None)
    elif kind == "suggestion":
        anticipate.why(rec)


def _path(frm: str | None, to: str | None) -> str:
    if not (frm and to) or frm == to:
        return ""
    tone = {c: t for c, _, _, t in COLUMNS}
    return (f'<div class="lyst-zone-path" aria-label="From {escape(_col_title(frm))} to {escape(_col_title(to))}">'
            f'<span class="lyst-zone-pill {tone[frm]}">{escape(_col_title(frm))}</span>'
            f'<span class="lyst-zone-arrow" aria-hidden="true">→</span>'
            f'<span class="lyst-zone-pill {tone[to]}">{escape(_col_title(to))}</span></div>')


def preview(card: dict, op: str, to: str | None = None) -> str:
    """The confirmation's body: the card, where it moves, what will happen and how it is governed."""
    spec = OPS[op]
    return (f'<div class="lyst-zone-confirm">'
            f'<div class="lyst-zone-card"><div class="lyst-zone-kicker">{escape(card["kindLabel"])} · '
            f'{escape(card["when"])}</div><div class="lyst-zone-title">{escape(card["title"])}</div>'
            + (f'<div class="lyst-zone-meta">{escape(card["meta"])}</div>' if card.get("meta") else "")
            + "</div>" + _path(card["col"], to)
            + f'<p class="lyst-zone-effect">{escape(spec.get("effect", ""))}</p>'
            + (f'<p class="lyst-zone-rule"><span aria-hidden="true">◆</span> {escape(spec["rule"])}</p>'
               if spec.get("rule") else "") + "</div>")


def _close() -> None:
    """The confirmation is answered or dismissed: stop drawing it on the page's next run."""
    st.session_state.pop("_zone_open", None)


def confirm(card: dict, rec: dict, op: str, to: str | None = None) -> None:
    """Ask before anything changes: what, where it moves, what happens and how it is governed. Only Confirm acts."""
    spec = OPS[op]
    label = next((o["label"] for o in card["ops"] if o["id"] == op), spec["label"])

    def body() -> None:
        st.html(preview(card, op, to))
        c = st.columns([1.4, 1, 2.2], vertical_alignment="center")
        if c[0].button(label, type="primary", key=f"zone_ok_{op}", width="stretch",
                       icon=":material/delete:" if spec.get("danger") and op.endswith("dismiss") else None):
            _close()
            if _call(card, rec, op):
                st.rerun()
        if c[1].button("Cancel", key=f"zone_no_{op}", type="tertiary", width="stretch"):
            _close()
            st.rerun()

    st.dialog(f"{label}?", width="small", on_dismiss=_close)(body)()


def review(card: dict, rec: dict) -> None:
    """An approval opened from the board: what was asked, by whom, and both answers side by side."""
    def body() -> None:
        st.html(preview(card, "approve"))
        c = st.columns(2)
        if c[0].button("Approve", type="primary", key="zone_rev_ok", width="stretch", icon=":material/check:"):
            _close()
            if _call(card, rec, "approve"):
                st.rerun()
        if c[1].button("Reject", key="zone_rev_no", width="stretch", icon=":material/close:"):
            _close()
            if _call(card, rec, "reject"):
                st.rerun()

    st.dialog("Review this approval", width="small", on_dismiss=_close)(body)()


def resolve(card: dict, event: dict) -> tuple[str | None, str | None]:
    """What a gesture asks for: (operation, column it moves to). (None, None) when the card does not offer it, so a
    stale or forged event can never reach an operation the card was not built with."""
    kind = event.get("type")
    allowed = {o["id"]: o for o in card["ops"]}
    if kind == "move":
        hit = next((o for o in card["ops"] if o.get("to") == event.get("to") and o.get("to") != card["col"]), None)
        return (hit["id"], hit["to"]) if hit else (None, None)
    if kind == "open":
        op = card.get("open") or "details"
        return op, None
    op = str(event.get("op") or "")
    if op in allowed:
        return op, allowed[op].get("to")
    return None, None


def handle(event: dict) -> None:
    """One gesture from the board: open a card, follow a move or run one of its operations (confirmed first)."""
    if event.get("type") == "go":
        page = str(event.get("page") or "")
        if page in PAGE_FILES:
            st.switch_page(PAGE_FILES[page])
        return
    card, rec = _find(str(event.get("card") or ""))
    if not card or not rec:
        _close()
        st.toast("That card has changed; the board shows it as it is now.", icon=":material/refresh:")
        drop()
        return
    op, to = resolve(card, event)
    if not op:
        if event.get("type") == "move":
            st.toast(f"This card cannot move to {_col_title(str(event.get('to')))}.", icon=":material/block:")
        return
    if OPS[op]["confirm"]:
        st.session_state["_zone_open"] = event  # drawn again on every page run until it is answered or dismissed
        confirm(card, rec, op, to)
    elif op == "details" and card["kind"] == "approval":
        st.session_state["_zone_open"] = event
        review(card, rec)
    else:
        _open(card, rec, op)


# ------------------------------------------------------------------------------------------------ the board

def _payload(page: str, cards: list[dict]) -> dict[str, Any]:
    keep = ("id", "kind", "kindLabel", "title", "col", "status", "tone", "page", "pages", "when", "meta", "ops",
            "progress", "progressText", "live")
    return {"page": page, "pageNames": PAGES, "cards": [{k: c[k] for k in keep if k in c} for c in cards],
            "columns": [{"id": c, "title": t, "hint": h, "tone": tone, "empty": EMPTY[c]} for c, t, h, tone in COLUMNS],
            "reduced": False}


def _draw(page: str) -> None:
    cards, _ = collect(workspace())
    res = _component()(data=_payload(page, cards), key=f"lyst_zone_{page}", on_action_change=lambda: None)
    ev = getattr(res, "action", None)
    if isinstance(ev, dict) and ev.get("nonce") != st.session_state.get("_zone_nonce"):
        st.session_state["_zone_nonce"] = ev.get("nonce")
        st.session_state["_zone_do"] = ev
        st.rerun()  # the whole page: a dialog or a move is handled outside this fragment


@st.fragment(run_every=LIVE)
def _live(page: str) -> None:
    _draw(page)


@st.fragment
def _still(page: str) -> None:
    _draw(page)


def board(page: str) -> None:
    """The page's activity zone: its own cards by default, everything with one switch. Gestures are handled here,
    outside the board's fragment, so a confirmation stays open while the board keeps itself current."""
    if page not in PAGES:
        raise ValueError(f"unknown page {page!r}")
    ev = st.session_state.pop("_zone_do", None) or st.session_state.get("_zone_open")
    if ev:
        handle(ev)
    try:
        cards, _ = collect(workspace())
    except APIError:
        return
    (_live if any(c["col"] == "working" for c in cards) else _still)(page)


CSS = """
/* the activity zone's confirmation: the card, where it moves, what happens, how it is governed */
.lyst-zone-confirm {display: flex; flex-direction: column; gap: .75rem;}
.lyst-zone-card {border: 1px solid var(--lyst-border); border-radius: var(--lyst-radius-md); padding: .7rem .85rem;
  background: var(--lyst-surface);}
.lyst-zone-kicker {font-size: var(--lyst-size-xs); text-transform: uppercase; letter-spacing: var(--lyst-tracking-label);
  color: var(--lyst-text-muted); font-weight: 600;}
.lyst-zone-title {font-weight: var(--lyst-weight-medium); margin-top: .2rem; line-height: 1.35;}
.lyst-zone-meta {font-size: var(--lyst-size-sm); color: var(--lyst-text-muted); margin-top: .25rem;}
.lyst-zone-path {display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;}
.lyst-zone-arrow {color: var(--lyst-text-muted);}
.lyst-zone-pill {font-size: var(--lyst-size-sm); font-weight: var(--lyst-weight-medium); border-radius: var(--lyst-radius-pill);
  padding: .15rem .65rem; border: 1px solid color-mix(in srgb, currentColor 40%, transparent);
  background: color-mix(in srgb, currentColor 8%, transparent);}
.lyst-zone-pill.accent {color: var(--lyst-link);} .lyst-zone-pill.warn {color: var(--lyst-warn);}
.lyst-zone-pill.info {color: var(--lyst-info);} .lyst-zone-pill.good {color: var(--lyst-good);}
p.lyst-zone-effect {margin: 0; line-height: 1.5;}
p.lyst-zone-rule {margin: 0; font-size: var(--lyst-size-sm); color: var(--lyst-text-muted); line-height: 1.5;}
p.lyst-zone-rule span {color: var(--lyst-accent); font-size: .6rem; vertical-align: .1rem; margin-right: .2rem;}
"""

__all__ = ["COLUMNS", "OPS", "board", "build", "collect", "confirm", "drop", "handle", "preview", "resolve"]
