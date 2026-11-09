# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Brain › Work: everything Lyst is doing or has done for this workspace, in three views (Needs you, Activity,
Recurring), with what is running now on top. It was its own page; it now lives inside Brain, beside what Lyst knows,
so what Lyst knows and what it does with it are one place. Old links to /work land here (app_pages/work.py)."""

from __future__ import annotations

import datetime

import streamlit as st

from lyst.ui import activity as auto
from lyst.ui import anticipate, collab, decisions, kit, needs, publish, visuals
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import task_state

PAGE = "app_pages/brain.py"
VIEWS = ("Needs you", "Activity", "Recurring")


def go(sub: str | None = None, **extra: str) -> None:
    """Open Brain › Work, at one of its views ('needs you', 'activity', 'recurring') when given."""
    st.switch_page(PAGE, query_params={"view": "work", **({"work": sub} if sub else {}), **extra})


def link(label: str, sub: str | None = None, *, icon: str = ":material/arrow_forward:") -> None:
    st.page_link(PAGE, label=label, icon=icon, query_params={"view": "work", **({"work": sub} if sub else {})})


TONE = {"green": "ok", "orange": "warn", "red": "bad", "blue": "run", "gray": "off"}
SENSITIVITY = {4.0: "Big moves only", 3.0: "Notable moves", 2.0: "Any unusual move"}
ACTIONS = {"schedule.activate": "Start a scheduled question", "monitor.activate": "Start watching a metric",
           "skill.register": "Add a new skill to Lyst", "decision.approve": "Approve a decision",
           "rule.activate": "Turn on an event rule", "activity.execute": "Run work Lyst prepared"}
WEEKDAYS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
EVERY = {3600: "Hourly", 21600: "Every 6 hours", 86400: "Daily", 604800: "Weekly", 2592000: "Monthly"}


def sensitivity(sigma: float) -> str:
    return SENSITIVITY.get(float(sigma)) or f"Moves beyond {sigma:g} standard deviations"


def priority(p: float) -> str:
    return "High priority" if p >= 6 else ("Medium priority" if p >= 4 else "Low priority")  # size of move + relative change


def every(seconds: int) -> str:
    return EVERY.get(seconds, f"Every {seconds // 3600} h" if seconds >= 3600 else f"Every {seconds // 60} min")


def _ctx() -> tuple:
    """The API, the workspace and the person's role, read on every call (a module outlives a page run)."""
    role = st.session_state.get("role")
    return client(), workspace(), role in ("admin", "owner"), role in ("editor", "admin", "owner")


def open_in_analyst(tid: str) -> None:
    st.session_state.task_id = tid
    st.switch_page("app_pages/analyst.py", query_params={"task": tid})


@st.dialog("Task", width="large")
def details(tid: str) -> None:
    api, ws, admin, editor = _ctx()
    t = api.get(f"/v1/tasks/{tid}")
    text, color, _ = task_state(t["status"], t.get("outcome"))
    st.markdown(f"**{t['title']}**")
    kit.chip(text, TONE.get(color, "info"))
    kind = {"scheduled": "Scheduled", "workflow": "Repeated analysis"}.get(t["kind"], "Asked")
    st.caption(f"{kind} · started {kit.when(t['created_at'])} · {sum(s['status'] == 'done' for s in t['steps'])} of "
               f"{len(t['steps'])} steps done")
    if t.get("error"):
        st.error(t["error"])
    n_done = sum(s["status"] == "done" for s in t["steps"])
    if t["steps"]:
        st.html(visuals.meter(n_done / len(t["steps"]), label="Steps done", tone="good" if t["status"] == "done" else
                              "info", text=f"{n_done} of {len(t['steps'])} steps done · autonomy: {t['autonomy']}"))
    from lyst.ui import actions  # noqa: PLC0415

    if (t.get("lifecycle") or {}).get("detail"):
        st.caption(f"{t['lifecycle']['state']}: {t['lifecycle']['detail']}")
    with st.expander("Steps", icon=":material/list:", expanded=t["status"] in ("running", "failed", "paused")):
        st.html(visuals.steps(t["steps"]))
    with st.expander("How it progressed", icon=":material/timeline:"):
        actions.timeline(tid)
    actions.menu("task", tid, page="brain", label="More", exclude=("task.open", "task.pause", "task.resume",
                                                                    "task.retry", "task.cancel"))
    c = st.columns(3)
    if t["status"] == "done" and c[0].button("Open the answer", type="primary", icon=":material/arrow_outward:"):
        open_in_analyst(tid)
    if t["status"] in ("queued", "running") and c[0].button("Pause", icon=":material/pause:"):
        kit.guarded(lambda: api.post(f"/v1/tasks/{tid}/pause"), "Paused")
        st.rerun()
    if t["status"] == "paused" and c[0].button("Resume", type="primary", icon=":material/play_arrow:"):
        kit.guarded(lambda: api.post(f"/v1/tasks/{tid}/resume"), "Resumed")
        st.rerun()
    if t["status"] == "failed" and c[0].button("Try again", type="primary", icon=":material/refresh:"):
        kit.guarded(lambda: api.post(f"/v1/tasks/{tid}/retry"), "Running it again")
        st.rerun()
    if t["status"] not in ("done", "failed", "cancelled") and c[1].button("Cancel", icon=":material/block:"):
        kit.guarded(lambda: api.post(f"/v1/tasks/{tid}/cancel"), "Cancelled")
        st.rerun()


@st.fragment
def needs_you() -> None:
    api, ws, admin, editor = _ctx()
    approvals = api.get(f"/v1/workspaces/{ws}/approvals") if admin else []
    tasks = [t for t in api.get(f"/v1/workspaces/{ws}/tasks") if t.get("outcome") in ("needs_input", "unverified")]
    mine, revs = collab.waiting()
    ready = anticipate.prepared_for_you()
    if ready:
        kit.label(f"Prepared for you · {len(ready)}")
        anticipate.rows(ready, key="pfy")
    if not approvals and not tasks and not mine and not revs and not ready:
        kit.empty(":material/task_alt:", "Nothing needs you", "Approvals, work assigned to you, reviews you can decide, "
                  "questions Lyst needs answered and results that did not pass verification appear here.")
        return
    collab.needs_you_lists()
    if approvals:
        kit.label(f"Approvals · {len(approvals)}")
        for i, ap in enumerate(approvals):
            hit = kit.row(ap["summary"], f"{ACTIONS.get(ap['action'], ap['action'].replace('.', ' ').replace('_', ' ').capitalize())}"
                          f" · {kit.when(ap['created_at'])}",
                          actions=[("Approve", "yes"), ("Reject", "no")], key=f"ap{ap['id']}", divider=i < len(approvals) - 1)
            if hit:
                if kit.guarded(lambda a=ap, h=hit: api.post(f"/v1/approvals/{a['id']}", {"approve": h == "yes"}),
                               "Approved" if hit == "yes" else "Rejected"):
                    st.rerun(scope="fragment")
    if tasks:
        kit.label(f"Waiting on you · {len(tasks)}")
        for i, t in enumerate(tasks):
            text, color, _ = task_state(t["status"], t["outcome"])
            # each one opens as Lyst's question with choices; the answer itself is a click further
            hit = kit.row(t["title"], kit.when(t["created_at"]), status=(text, TONE.get(color, "info")),
                          actions=[("Resolve", "resolve"), ("Open", "open")], key=f"nt{t['id']}",
                          divider=i < len(tasks) - 1)
            if hit == "resolve":
                needs.sheet(t["id"], t["title"])
            elif hit == "open":
                open_in_analyst(t["id"])


@st.fragment
def activity() -> None:
    api, ws, admin, editor = _ctx()
    tasks = api.get(f"/v1/workspaces/{ws}/tasks")
    if not tasks:
        if kit.empty(":material/view_kanban:", "No work yet", "Everything you ask Lyst, and everything it runs on a "
                     "schedule, is listed here.", action="Ask Lyst", key="act_ask"):
            st.switch_page("app_pages/analyst.py")
        return
    groups = {"All": None, "Running": {"queued", "running", "paused", "awaiting_approval"}, "Done": {"done"},
              "Stopped": {"failed", "cancelled"}, "Automatic": "automatic", "Decisions": "decisions",
              "Published": "published"}
    # the board view of this work is the activity zone above Brain's sections; this is the full history as a list
    pick = st.pills("Show", list(groups), default="All", label_visibility="collapsed", key="act_filter") or "All"
    if pick == "Decisions":
        decisions.activity_rows()
        return
    if pick == "Published":
        publish.listed()
        return
    if pick == "Automatic":  # what event rules started, with why and where each one is
        auto.workspace_list(admin=admin)
        return
    shown = [t for t in tasks if groups[pick] is None or t["status"] in groups[pick]][:60]
    last = None
    for i, t in enumerate(shown):
        day = _day(t["created_at"])
        if day != last:
            kit.label(day)
            last = day
        text, color, _ = task_state(t["status"], t.get("outcome"))
        if (t.get("lifecycle") or {}).get("state") and t["status"] not in ("done", "cancelled"):
            text, color = t["lifecycle"]["state"], "blue" if t["status"] in ("queued", "running") else color
        kind = {"scheduled": "Scheduled", "workflow": "Workflow"}.get(t["kind"], "")
        nxt = shown[i + 1] if i + 1 < len(shown) else None
        if kit.row(t["title"], " · ".join(x for x in (kind, kit.when(t["created_at"])) if x),
                   status=(text, TONE.get(color, "info")), actions=[("Details", "d")], key=f"t{t['id']}",
                   divider=bool(nxt) and _day(nxt["created_at"]) == day):
            details(t["id"])


def _day(ts: str) -> str:
    """Today, Yesterday, or the date: the timeline's day headings."""
    try:
        d = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().date()
    except ValueError:
        return "Earlier"
    today = datetime.datetime.now().astimezone().date()
    return "Today" if d == today else ("Yesterday" if (today - d).days == 1 else d.strftime("%-d %B"))


@st.fragment
def recurring() -> None:
    api, ws, admin, editor = _ctx()
    anticipate.coming_up()
    scheds = api.get(f"/v1/workspaces/{ws}/schedules")
    mons = api.get(f"/v1/workspaces/{ws}/monitors")
    kit.label("Scheduled questions")
    if not scheds:
        st.caption("Nothing repeats on its own yet. Open any answer and choose Repeat it, or set one up here.")
    for i, s in enumerate(scheds):
        when = (s.get("cadence") or every(s["every_seconds"])).replace("every", "Every", 1)
        meta = " · ".join(x for x in (
            when, f"next {kit.when(s['next_run_at'])}" if s.get("next_run_at") else None,
            f"last ran {kit.when(s['last_run_at'])}" if s.get("last_run_at") else None,
            f"missed {s['missed']}" if s.get("missed") else None,
            f"skipped {s['skipped']} (the last run was still going)" if s.get("skipped") else None) if x)
        status = ("Awaiting approval", "wait") if not s["enabled"] else (("Missed runs", "warn") if s.get("missed")
                                                                         else ("Active", "ok"))
        hit = kit.row(s["title"], meta, status=status, key=f"s{s['id']}", divider=i < len(scheds) - 1,
                      actions=[("Last result", "last")] if s.get("last_task_id") else [])
        if hit == "last":
            open_in_analyst(s["last_task_id"])
    if editor:
        with st.popover("Schedule a question", icon=":material/event_repeat:"):
            title = st.text_input("Name", placeholder="Weekly margin review", key="sch_title")
            question = st.text_input("What to ask", placeholder="Why did operating margin change last week?", key="sch_q")
            how = st.segmented_control("When", ["At a set time", "Every so often"], default="At a set time",
                                       key="sch_how", label_visibility="collapsed") or "At a set time"
            if how == "At a set time":
                c = st.columns(2)
                at = c[0].time_input("At", value=datetime.time(8, 0), step=900, key="sch_at")
                tz = c[1].text_input("Time zone", value=st.context.timezone or "UTC", key="sch_tz",
                                     help="An IANA name, such as Europe/London or America/New_York.")
                days = st.pills("On", list(WEEKDAYS), selection_mode="multi", default=["mon"], format_func=WEEKDAYS.get,
                                key="sch_days")
                body = {"at": at.strftime("%H:%M"), "weekdays": days or list(WEEKDAYS), "timezone": tz.strip() or "UTC",
                        "overlap": "skip"}
            else:
                body = {"every_seconds": st.selectbox("How often", [86400, 604800, 2592000], format_func=EVERY.get,
                                                      key="sch_ev")}
            if st.button("Propose", type="primary", key="sch_go") and title and question:
                if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/schedules",
                                                {"title": title, "question": question, **body}),
                               "Proposed. It starts once an admin approves it."):
                    st.rerun(scope="fragment")
    kit.label("Watched metrics")
    if not mons:
        st.caption("No metric is watched yet. Lyst checks a watched metric on its own, investigates unusual moves "
                   "and tells you here and on Home.")
    for i, m in enumerate(mons):
        kit.row(m["title"], f"Each {m['grain']} · {sensitivity(m['threshold_sigma']).lower()} · checked "
                f"{every(m['every_seconds']).lower()} · last checked {kit.when(m['last_run_at']) or 'not yet'}",
                status=("Active", "ok") if m["enabled"] else ("Awaiting approval", "wait"), key=f"m{m['id']}",
                divider=i < len(mons) - 1)
    if editor:
        with st.popover("Watch a metric", icon=":material/monitor_heart:"):
            with st.form("monitor", border=False):
                metric = st.selectbox("Metric", [m["name"] for m in api.get(f"/v1/workspaces/{ws}/metrics")],
                                      format_func=lambda n: n.replace("_", " ").capitalize())
                grain = st.selectbox("Looking at", ["month", "quarter"], format_func=lambda g: f"Each {g}")
                sigma = st.radio("Tell me about", list(SENSITIVITY), format_func=SENSITIVITY.get, index=1,
                                 help="Big moves only alerts beyond 4 standard deviations of the usual change, "
                                      "notable beyond 3, any unusual move beyond 2.")
                ev = st.selectbox("Check it", [3600, 21600, 86400], format_func=EVERY.get, index=2)
                if st.form_submit_button("Propose", type="primary"):
                    if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/monitors", {
                            "metric": metric, "grain": grain, "threshold_sigma": sigma, "every_seconds": ev}),
                            "Proposed. It starts once an admin approves it."):
                        st.rerun(scope="fragment")
    found = [f for f in api.get(f"/v1/workspaces/{ws}/findings") if f["status"] != "dismissed"]
    if found:
        kit.label(f"What Lyst noticed · {len(found)}")
        for i, f in enumerate(found[:20]):
            hit = kit.row(f["summary"], f"{priority(f['priority'])} · {kit.when(f['created_at'])}",
                          status=("Risk", "bad") if f["direction"] == "risk" else ("Opportunity", "ok"),
                          actions=[("See why", "why"), ("Dismiss", "x")], key=f"f{f['id']}", divider=i < len(found) - 1)
            if hit == "why" and f.get("task_id"):
                kit.guarded(lambda x=f: api.post(f"/v1/workspaces/{ws}/findings/{x['id']}", {"status": "acknowledged"}))
                open_in_analyst(f["task_id"])
            elif hit == "x":
                kit.guarded(lambda x=f: api.post(f"/v1/workspaces/{ws}/findings/{x['id']}", {"status": "dismissed"}), "Dismissed")
                st.rerun(scope="fragment")


ACTIVE = {"queued", "running", "paused", "awaiting_approval"}


def now(tasks: list[dict]) -> None:
    """What Lyst is doing this minute, as live cards with the engine's own step progress. Nothing when idle."""
    running = [t for t in tasks if t["status"] in ACTIVE][:3]
    if not running:
        return
    api = client()
    kit.label(f"Running now · {sum(t['status'] in ACTIVE for t in tasks)}")
    cols = st.columns(3)
    for col, t in zip(cols, running, strict=False):
        with col, kit.card(f"now_{t['id']}"):
            full = kit.guarded(lambda x=t: api.get(f"/v1/tasks/{x['id']}")) or {}
            sts = full.get("steps") or []
            n = sum(x["status"] == "done" for x in sts)
            lc = t.get("lifecycle") or {}
            st.html(f'<div class="lyst-card-kicker"><span class="lyst-live-dot" aria-hidden="true"></span>'
                    f'{kit._esc(lc.get("state") or task_state(t["status"], t.get("outcome"))[0])}</div>'
                    f'<div class="lyst-card-title lyst-clamp">{kit._esc(t["title"])}</div>'
                    + (visuals.meter(n / len(sts), label="Steps done", tone="info", text=f"{n} of {len(sts)} steps")
                       if sts else ""))
            if st.button("Details", key=f"now_d_{t['id']}", type="tertiary", icon=":material/open_in_full:"):
                details(t["id"])


def section(tasks: list[dict], waiting: int) -> None:
    """Brain › Work: running work on top, then Needs you, Activity or Recurring, kept in the URL as ?work=."""
    if (did := st.query_params.get("decision")):  # a link to one decision (from the engine's actions) opens it
        st.query_params.pop("decision", None)
        decisions.detail(did)
    now(tasks)
    view = kit.sections(list(VIEWS), key="work", counts={"Needs you": waiting},
                        default="Needs you" if waiting else "Activity")
    try:
        {"Needs you": needs_you, "Activity": activity, "Recurring": recurring}[view]()
    except APIError as e:
        st.error(str(e), icon=":material/cloud_off:")
