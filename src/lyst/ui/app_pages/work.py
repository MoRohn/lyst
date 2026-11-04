# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import pandas as pd
import streamlit as st

from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md, task_state

api, ws = client(), workspace()
st.markdown("## Work")
SECTIONS = ("Board", "Approvals", "Schedules", "Monitors")

@st.fragment
def board() -> None:
    tasks = api.get(f"/v1/workspaces/{ws}/tasks")
    def lane(t) -> str:
        if t["status"] == "done":
            return "Needs you" if t.get("outcome") in ("needs_input", "unverified") else "Done"
        return {"queued": "Queued", "running": "Running", "paused": "Running", "awaiting_approval": "Needs you"}.get(
            t["status"], "Stopped")

    lanes = ["Queued", "Running", "Needs you", "Done", "Stopped"]
    cols = st.columns(len(lanes))
    for col, title in zip(cols, lanes, strict=True):
        items = [t for t in tasks if lane(t) == title]
        col.markdown(f"**{title}** · {len(items)}")
        for t in items[:20]:
            with col.container(border=True):
                st.markdown(md(t["title"]))
                st.caption(f"{task_state(t['status'], t.get('outcome'))[0]} · {t['kind']} · {str(t['created_at'])[:16]}")
                b = st.columns(2)
                if t["status"] in ("queued", "running") and b[0].button("Pause", key=f"p{t['id']}", type="tertiary"):
                    api.post(f"/v1/tasks/{t['id']}/pause")
                    st.rerun(scope="fragment")
                if t["status"] == "paused" and b[0].button("Resume", key=f"r{t['id']}", type="tertiary"):
                    api.post(f"/v1/tasks/{t['id']}/resume")
                    st.rerun(scope="fragment")
                if t["status"] == "failed" and b[0].button("Retry", key=f"y{t['id']}", type="tertiary"):
                    api.post(f"/v1/tasks/{t['id']}/retry")
                    st.rerun(scope="fragment")
                if t["status"] == "done" and b[0].button("Open", key=f"o{t['id']}", type="tertiary",
                                                         icon=":material/arrow_outward:"):
                    st.session_state.task_id = t["id"]
                    st.switch_page("app_pages/analyst.py")
                if t["status"] not in ("done", "failed", "cancelled") and b[1].button(
                        "Cancel", key=f"c{t['id']}", type="tertiary"):
                    api.post(f"/v1/tasks/{t['id']}/cancel")
                    st.rerun(scope="fragment")
    sel = st.selectbox("Inspect a task", [t["id"] for t in tasks], index=None,
                       format_func=lambda i: next(t["title"] for t in tasks if t["id"] == i))
    if sel:
        t = api.get(f"/v1/tasks/{sel}")
        st.dataframe(pd.DataFrame(t["steps"])[["seq", "phase", "agent", "capability", "status", "started_at",
                                               "finished_at", "error"]], hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame(t["events"]), hide_index=True, width="stretch")

@st.fragment
def approvals() -> None:
    pending = api.get(f"/v1/workspaces/{ws}/approvals")
    if not pending:
        st.caption("Nothing is waiting for approval.")
    for ap in pending:
        with st.container(border=True):
            st.markdown(md(ap["summary"]))
            st.caption(f"{ap['action']} · requested by {ap['requested_by']} · {str(ap['created_at'])[:16]}")
            c = st.columns([1, 1, 6])
            try:
                if c[0].button("Approve", key=f"a{ap['id']}", type="primary"):
                    api.post(f"/v1/approvals/{ap['id']}", {"approve": True})
                    st.rerun(scope="fragment")
                if c[1].button("Reject", key=f"x{ap['id']}"):
                    api.post(f"/v1/approvals/{ap['id']}", {"approve": False})
                    st.rerun(scope="fragment")
            except APIError as e:
                st.error(str(e))

@st.fragment
def schedules() -> None:
    for s in api.get(f"/v1/workspaces/{ws}/schedules"):
        st.write(f"**{md(s['title'])}** · every {s['every_seconds'] // 3600 or s['every_seconds'] // 60} "
                 f"{'h' if s['every_seconds'] >= 3600 else 'min'} · {'active' if s['enabled'] else 'awaiting approval'}"
                 f" · next {str(s['next_run_at'])[:16]}")
    with st.form("schedule"):
        title = st.text_input("Title", placeholder="Weekly margin review")
        question = st.text_input("Request", placeholder="Why did operating margin decline last month?")
        every = st.selectbox("Every", [3600, 86400, 7 * 86400], format_func=lambda s: {3600: "hour", 86400: "day",
                                                                                     604800: "week"}[s])
        if st.form_submit_button("Propose schedule") and title and question:
            try:
                api.post(f"/v1/workspaces/{ws}/schedules", {"title": title, "question": question, "every_seconds": every})
                st.success("Proposed. It starts once an admin approves it.")
            except APIError as e:
                st.error(str(e))

@st.fragment
def monitors() -> None:
    st.caption("Monitors watch an approved metric, investigate material changes on their own, rank the findings and "
               "notify owners and admins. A new monitor starts once an admin approves it.")
    mons = api.get(f"/v1/workspaces/{ws}/monitors")
    if mons:
        st.dataframe(pd.DataFrame(mons)[["title", "metric", "grain", "threshold_sigma", "every_seconds", "enabled", "last_run_at"]],
                     hide_index=True, width="stretch")
    with st.form("monitor"):
        mc = st.columns([2, 1, 1, 1])
        metric = mc[0].selectbox("Metric", [m["name"] for m in api.get(f"/v1/workspaces/{ws}/metrics")])
        grain = mc[1].selectbox("Grain", ["month", "quarter"])
        sigma = mc[2].number_input("Alert beyond (sigma)", 1.0, 10.0, 3.0, 0.5)
        every = mc[3].selectbox("Check every", [3600, 21600, 86400], format_func=lambda s: {3600: "hour", 21600: "6 hours", 86400: "day"}[s])
        if st.form_submit_button("Propose monitor", type="primary"):
            try:
                api.post(f"/v1/workspaces/{ws}/monitors", {"metric": metric, "grain": grain, "threshold_sigma": sigma, "every_seconds": every})
                st.success("Proposed. It starts once an admin approves it in Approvals.")
            except APIError as e:
                st.error(str(e))
    st.markdown("**Findings**")
    for f in api.get(f"/v1/workspaces/{ws}/findings"):
        with st.container(border=True):
            head = st.columns([6, 1, 1], vertical_alignment="center")
            head[0].markdown(md(f["summary"]))
            head[1].badge(f["direction"], color="red" if f["direction"] == "risk" else "green")
            head[2].caption(f"priority {f['priority']:.1f}")
            row = st.columns([1, 1, 1, 5])
            if f.get("task_id") and row[0].button("Open investigation", key=f"inv_{f['id']}", type="tertiary"):
                st.session_state.task_id = f["task_id"]
                st.switch_page("app_pages/analyst.py")
            if f["status"] == "new" and row[1].button("Acknowledge", key=f"ack_{f['id']}", type="tertiary"):
                api.post(f"/v1/workspaces/{ws}/findings/{f['id']}", {"status": "acknowledged"})
                st.rerun(scope="fragment")
            if f["status"] != "dismissed" and row[2].button("Dismiss", key=f"dis_{f['id']}", type="tertiary"):
                api.post(f"/v1/workspaces/{ws}/findings/{f['id']}", {"status": "dismissed"})
                st.rerun(scope="fragment")


# Only the section in view is fetched and drawn, and each one reruns on its own when you act in it, so approving
# a step does not reload the board, schedules, monitors and findings. The section is in the URL (?section=).
try:
    waiting = len(api.get(f"/v1/workspaces/{ws}/approvals"))
except APIError:
    waiting = 0
labels = {s_: (f"{s_} · {waiting}" if s_ == "Approvals" and waiting else s_) for s_ in SECTIONS}
asked = st.query_params.get("section", "Board").capitalize()
section = st.segmented_control("Section", SECTIONS, default=asked if asked in SECTIONS else "Board",
                               format_func=labels.get, label_visibility="collapsed", key="work_section") or "Board"
if st.query_params.get("section") != section.lower():
    st.query_params["section"] = section.lower()
{"Board": board, "Approvals": approvals, "Schedules": schedules, "Monitors": monitors}[section]()
