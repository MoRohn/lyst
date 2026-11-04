# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import streamlit as st

from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import answer, status_badge

api, ws = client(), workspace()
ACTIVE = {"queued", "running", "paused", "awaiting_approval"}

SUGGESTED = [
    "Why did operating margin decline last quarter?",
    "What was revenue last quarter by region?",
    "Forecast revenue",
    "Profile pnl_ledger for data quality issues",
]


def submit(q: str) -> None:
    try:
        st.session_state.task_id = api.post(f"/v1/workspaces/{ws}/tasks", {"question": q})["task_id"]
    except APIError as e:
        st.error(str(e))


st.markdown("## What should Lyst work on?")
with st.form("ask", clear_on_submit=True, border=False):
    q = st.text_area("Request", placeholder="Describe the question or the work. Lyst plans it, runs it on "
                     "governed data and checks the result.", label_visibility="collapsed", height=90)
    c = st.columns([1, 5])
    if c[0].form_submit_button("Run", type="primary", icon=":material/play_arrow:") and q.strip():
        submit(q.strip())

pick = st.pills("Suggested", SUGGESTED, label_visibility="collapsed", key=f"pick_{st.session_state.get('task_id')}")
if pick:
    submit(pick)


@st.fragment(run_every=1.0)
def live() -> None:
    tid = st.session_state.get("task_id")
    if not tid:
        return
    t = api.get(f"/v1/tasks/{tid}")
    head = st.columns([6, 1], vertical_alignment="center")
    head[0].markdown(f"**{t['title']}**")
    with head[1]:
        status_badge(t["status"])
    if t["status"] in ACTIVE:
        with st.status("Working", expanded=True, state="running"):
            for s in t["steps"]:
                icon = "✓" if s["status"] == "done" else ("✕" if s["status"] == "failed" else "…")
                st.write(f"{icon} {s['agent'].replace('_', ' ')} · {s['capability'].replace('_', ' ')}")
        if t["status"] == "awaiting_approval":
            st.info("A step needs approval. An admin can approve it in Work.", icon=":material/approval:")
        return
    if t["status"] == "failed":
        st.error(t["error"])
        return
    if t["result"]:
        answer(t["result"])
        if t["result"]["status"] == "complete":
            d = st.columns([1, 1, 6])
            for i, (kind, label) in enumerate((("xlsx", "Excel workbook"), ("md", "Briefing"))):
                if d[i].button(label, key=f"mk_{kind}_{tid}", icon=":material/download:", type="tertiary"):
                    try:
                        a = api.post(f"/v1/tasks/{tid}/artifacts/{kind}")
                        st.session_state[f"art_{kind}_{tid}"] = a
                    except APIError as e:
                        st.error(str(e))
                a = st.session_state.get(f"art_{kind}_{tid}")
                if a:
                    data = api.http.get(f"/v1/artifacts/{a['id']}", headers=api._h()).content
                    d[i].download_button(f"Save {a['name']}", data, file_name=a["name"], mime=a["mime"],
                                         key=f"dl_{kind}_{tid}")
        metric = (t["result"].get("supporting", {}).get("metric") or {}).get("spec", {})
        if metric:
            st.caption("Follow up")
            dims = metric.get("dimensions", [])
            opts = [f"Show the {metric['title'].lower()} trend over time", f"Forecast {metric['title'].lower()}"]
            opts += [f"What was {metric['title'].lower()} last quarter by {d.replace('_', ' ')}?" for d in dims]
            nxt = st.pills("Follow up", opts, label_visibility="collapsed", key=f"fu_{tid}")
            if nxt:
                submit(nxt)
                st.rerun()


live()

st.divider()
st.markdown("##### Recent work")
try:
    recent = api.get(f"/v1/workspaces/{ws}/tasks")[:8]
except APIError:
    recent = []
for t in recent:
    row = st.columns([6, 1.2, 1], vertical_alignment="center")
    row[0].write(t["title"])
    with row[1]:
        status_badge(t["status"])
    if row[2].button("Open", key=f"open_{t['id']}", type="tertiary"):
        st.session_state.task_id = t["id"]
        st.rerun()
