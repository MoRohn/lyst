# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import pandas as pd
import streamlit as st

from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import STATUS

api, ws = client(), workspace()
st.markdown("## Work")
tabs = st.tabs(["Board", "Approvals", "Schedules"])

with tabs[0]:
    tasks = api.get(f"/v1/workspaces/{ws}/tasks")
    lanes = [("Queued", ["queued"]), ("Running", ["running", "paused"]), ("Needs approval", ["awaiting_approval"]),
             ("Done", ["done"]), ("Stopped", ["failed", "cancelled"])]
    cols = st.columns(len(lanes))
    for col, (title, states) in zip(cols, lanes, strict=True):
        items = [t for t in tasks if t["status"] in states]
        col.markdown(f"**{title}** · {len(items)}")
        for t in items[:20]:
            with col.container(border=True):
                st.write(t["title"])
                st.caption(f"{STATUS[t['status']][0]} · {t['kind']} · {str(t['created_at'])[:16]}")
                b = st.columns(2)
                if t["status"] in ("queued", "running") and b[0].button("Pause", key=f"p{t['id']}", type="tertiary"):
                    api.post(f"/v1/tasks/{t['id']}/pause")
                    st.rerun()
                if t["status"] == "paused" and b[0].button("Resume", key=f"r{t['id']}", type="tertiary"):
                    api.post(f"/v1/tasks/{t['id']}/resume")
                    st.rerun()
                if t["status"] == "failed" and b[0].button("Retry", key=f"y{t['id']}", type="tertiary"):
                    api.post(f"/v1/tasks/{t['id']}/retry")
                    st.rerun()
                if t["status"] not in ("done", "failed", "cancelled") and b[1].button(
                        "Cancel", key=f"c{t['id']}", type="tertiary"):
                    api.post(f"/v1/tasks/{t['id']}/cancel")
                    st.rerun()
    sel = st.selectbox("Inspect a task", [t["id"] for t in tasks], index=None,
                       format_func=lambda i: next(t["title"] for t in tasks if t["id"] == i))
    if sel:
        t = api.get(f"/v1/tasks/{sel}")
        st.dataframe(pd.DataFrame(t["steps"])[["seq", "phase", "agent", "capability", "status", "started_at",
                                               "finished_at", "error"]], hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame(t["events"]), hide_index=True, width="stretch")

with tabs[1]:
    pending = api.get(f"/v1/workspaces/{ws}/approvals")
    if not pending:
        st.caption("Nothing is waiting for approval.")
    for ap in pending:
        with st.container(border=True):
            st.write(ap["summary"])
            st.caption(f"{ap['action']} · requested by {ap['requested_by']} · {str(ap['created_at'])[:16]}")
            c = st.columns([1, 1, 6])
            try:
                if c[0].button("Approve", key=f"a{ap['id']}", type="primary"):
                    api.post(f"/v1/approvals/{ap['id']}", {"approve": True})
                    st.rerun()
                if c[1].button("Reject", key=f"x{ap['id']}"):
                    api.post(f"/v1/approvals/{ap['id']}", {"approve": False})
                    st.rerun()
            except APIError as e:
                st.error(str(e))

with tabs[2]:
    for s in api.get(f"/v1/workspaces/{ws}/schedules"):
        st.write(f"**{s['title']}** · every {s['every_seconds'] // 3600 or s['every_seconds'] // 60} "
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
