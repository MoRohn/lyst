# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Work: everything Lyst is doing or has done for this workspace, in three views. What needs you comes first."""

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import task_state

api, ws = client(), workspace()
admin = st.session_state.get("role") in ("admin", "owner")
editor = st.session_state.get("role") in ("editor", "admin", "owner")
TONE = {"green": "ok", "orange": "warn", "red": "bad", "blue": "run", "gray": "off"}
EVERY = {3600: "Hourly", 21600: "Every 6 hours", 86400: "Daily", 604800: "Weekly", 2592000: "Monthly"}


def every(seconds: int) -> str:
    return EVERY.get(seconds, f"Every {seconds // 3600} h" if seconds >= 3600 else f"Every {seconds // 60} min")


def open_in_analyst(tid: str) -> None:
    st.session_state.task_id = tid
    st.switch_page("app_pages/analyst.py", query_params={"task": tid})


@st.dialog("Task", width="large")
def details(tid: str) -> None:
    t = api.get(f"/v1/tasks/{tid}")
    text, color, _ = task_state(t["status"], t.get("outcome"))
    st.markdown(f"**{t['title']}**")
    kit.chip(text, TONE.get(color, "info"))
    st.caption(f"{t['kind'].capitalize()} · started {kit.when(t['created_at'])} · autonomy {t['autonomy']}")
    if t.get("error"):
        st.error(t["error"])
    kit.label("Steps")
    for s in t["steps"]:
        mark = {"done": "✓", "failed": "✕", "skipped": "–", "running": "…"}.get(s["status"], "·")
        st.markdown(f"{mark} {s['capability'].replace('_', ' ')} · {s['agent'].replace('_', ' ')}"
                    + (f" · {s['error']}" if s.get("error") else ""))
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
    approvals = api.get(f"/v1/workspaces/{ws}/approvals") if admin else []
    tasks = [t for t in api.get(f"/v1/workspaces/{ws}/tasks") if t.get("outcome") in ("needs_input", "unverified")]
    if not approvals and not tasks:
        kit.empty(":material/task_alt:", "Nothing needs you", "Approvals, questions Lyst needs answered and results that "
                  "did not pass verification appear here.")
        return
    if approvals:
        kit.label(f"Approvals · {len(approvals)}")
        for i, ap in enumerate(approvals):
            hit = kit.row(ap["summary"], f"{ap['action']} · {kit.when(ap['created_at'])}",
                          actions=[("Approve", "yes"), ("Reject", "no")], key=f"ap{ap['id']}", divider=i < len(approvals) - 1)
            if hit:
                if kit.guarded(lambda a=ap, h=hit: api.post(f"/v1/approvals/{a['id']}", {"approve": h == "yes"}),
                               "Approved" if hit == "yes" else "Rejected"):
                    st.rerun(scope="fragment")
    if tasks:
        kit.label(f"Waiting on you · {len(tasks)}")
        for i, t in enumerate(tasks):
            text, color, _ = task_state(t["status"], t["outcome"])
            if kit.row(t["title"], kit.when(t["created_at"]), status=(text, TONE.get(color, "info")),
                       actions=[("Open", "open")], key=f"nt{t['id']}", divider=i < len(tasks) - 1):
                open_in_analyst(t["id"])


@st.fragment
def activity() -> None:
    tasks = api.get(f"/v1/workspaces/{ws}/tasks")
    if not tasks:
        if kit.empty(":material/view_kanban:", "No work yet", "Everything you ask Lyst, and everything it runs on a "
                     "schedule, is listed here.", action="Ask Lyst", key="act_ask"):
            st.switch_page("app_pages/analyst.py")
        return
    groups = {"All": None, "Running": {"queued", "running", "paused", "awaiting_approval"}, "Done": {"done"},
              "Stopped": {"failed", "cancelled"}}
    pick = st.pills("Show", list(groups), default="All", label_visibility="collapsed", key="act_filter") or "All"
    shown = [t for t in tasks if groups[pick] is None or t["status"] in groups[pick]][:40]
    for i, t in enumerate(shown):
        text, color, _ = task_state(t["status"], t.get("outcome"))
        kind = {"scheduled": "Scheduled", "workflow": "Workflow"}.get(t["kind"], "")
        if kit.row(t["title"], " · ".join(x for x in (kind, kit.when(t["created_at"])) if x),
                   status=(text, TONE.get(color, "info")), actions=[("Details", "d")], key=f"t{t['id']}",
                   divider=i < len(shown) - 1):
            details(t["id"])


@st.fragment
def recurring() -> None:
    scheds = api.get(f"/v1/workspaces/{ws}/schedules")
    mons = api.get(f"/v1/workspaces/{ws}/monitors")
    kit.label("Schedules")
    if not scheds:
        st.caption("Nothing runs on a schedule yet. Schedule a verified workflow from Build › Workflows, or below.")
    for i, s in enumerate(scheds):
        kit.row(s["title"], f"{every(s['every_seconds'])} · next {kit.when(s['next_run_at']) or s['next_run_at']}",
                status=("Active", "ok") if s["enabled"] else ("Awaiting approval", "wait"), key=f"s{s['id']}",
                divider=i < len(scheds) - 1)
    if editor:
        with st.popover("New schedule", icon=":material/event_repeat:"):
            with st.form("schedule", border=False):
                title = st.text_input("Name", placeholder="Weekly margin review")
                question = st.text_input("What to ask", placeholder="Why did operating margin change last week?")
                ev = st.selectbox("How often", [86400, 604800, 2592000], format_func=EVERY.get)
                if st.form_submit_button("Propose", type="primary") and title and question:
                    if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/schedules",
                                                    {"title": title, "question": question, "every_seconds": ev}),
                                   "Proposed. It starts once an admin approves it."):
                        st.rerun(scope="fragment")
    kit.label("Monitors")
    if not mons:
        st.caption("No metric is watched yet. A monitor investigates material changes on its own and tells you.")
    for i, m in enumerate(mons):
        kit.row(m["title"], f"{m['grain'].capitalize()} · alerts beyond {m['threshold_sigma']:g}σ · checks "
                f"{every(m['every_seconds']).lower()} · last run {kit.when(m['last_run_at']) or 'not yet'}",
                status=("Active", "ok") if m["enabled"] else ("Awaiting approval", "wait"), key=f"m{m['id']}",
                divider=i < len(mons) - 1)
    if editor:
        with st.popover("Watch a metric", icon=":material/monitor_heart:"):
            with st.form("monitor", border=False):
                metric = st.selectbox("Metric", [m["name"] for m in api.get(f"/v1/workspaces/{ws}/metrics")],
                                      format_func=lambda n: n.replace("_", " ").capitalize())
                grain = st.selectbox("By", ["month", "quarter"], format_func=str.capitalize)
                sigma = st.slider("Alert when a change is beyond (σ)", 1.0, 6.0, 3.0, 0.5)
                ev = st.selectbox("Check", [3600, 21600, 86400], format_func=EVERY.get, index=2)
                if st.form_submit_button("Propose", type="primary"):
                    if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/monitors", {
                            "metric": metric, "grain": grain, "threshold_sigma": sigma, "every_seconds": ev}),
                            "Proposed. It starts once an admin approves it."):
                        st.rerun(scope="fragment")
    found = [f for f in api.get(f"/v1/workspaces/{ws}/findings") if f["status"] != "dismissed"]
    if found:
        kit.label(f"Findings · {len(found)}")
        for i, f in enumerate(found[:20]):
            hit = kit.row(f["summary"], f"Priority {f['priority']:.1f} · {kit.when(f['created_at'])}",
                          status=("Risk", "bad") if f["direction"] == "risk" else ("Opportunity", "ok"),
                          actions=[("See why", "why"), ("Dismiss", "x")], key=f"f{f['id']}", divider=i < len(found) - 1)
            if hit == "why" and f.get("task_id"):
                kit.guarded(lambda x=f: api.post(f"/v1/workspaces/{ws}/findings/{x['id']}", {"status": "acknowledged"}))
                open_in_analyst(f["task_id"])
            elif hit == "x":
                kit.guarded(lambda x=f: api.post(f"/v1/workspaces/{ws}/findings/{x['id']}", {"status": "dismissed"}), "Dismissed")
                st.rerun(scope="fragment")


try:
    waiting = st.session_state.get("waiting", 0)
    kit.header("Work", "What Lyst is doing, what it finished, and what runs on its own.")
    mine = [t for t in api.get(f"/v1/workspaces/{ws}/tasks") if t.get("outcome") in ("needs_input", "unverified")]
    waiting += len(mine)
    view = kit.sections(["Needs you", "Activity", "Recurring"], key="view", counts={"Needs you": waiting},
                        default="Needs you" if waiting else "Activity")
    {"Needs you": needs_you, "Activity": activity, "Recurring": recurring}[view]()
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
