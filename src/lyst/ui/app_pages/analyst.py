# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import streamlit as st

from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import answer, md, status_badge

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


try:
    unread = [i for i in api.get(f"/v1/workspaces/{ws}/inbox") if i["read_status"] == "unread"]
except APIError:
    unread = []
if unread:
    with st.container(border=True):
        st.markdown(f"**Lyst found {len(unread)} thing{'s' if len(unread) > 1 else ''} worth your attention**")
        for f in unread[:3]:
            c = st.columns([7, 1.5], vertical_alignment="center")
            c[0].markdown(md(f["summary"]))
            if f.get("task_id") and c[1].button("See why", key=f"why_{f['id']}", type="tertiary"):
                st.session_state.task_id = f["task_id"]
                api.post(f"/v1/workspaces/{ws}/findings/{f['id']}", {"status": "acknowledged"})
                st.rerun()

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


PHASES = {"understand": "Reading the request", "contextualize": "Finding definitions and data",
          "plan": "Planning the analysis", "investigate": "Investigating", "execute": "Running governed queries",
          "hypothesize": "Forming explanations", "test": "Testing explanations", "validate": "Checking the result",
          "deliver": "Writing the answer", "learn": "Recording what was learned", "improve": "Improving a capability"}
EXPORTS = (("xlsx", "Excel"), ("pdf", "PDF briefing"), ("pptx", "Deck"), ("md", "Markdown"))
MIME = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "md": "text/markdown",
        "pdf": "application/pdf", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


def _export(tid: str, kind: str):
    """Build the deliverable only when the person clicks (one click, not 'make' then 'save')."""
    def build() -> bytes:
        a = api.post(f"/v1/tasks/{tid}/artifacts/{kind}")
        return api.http.get(f"/v1/artifacts/{a['id']}", headers=api._h()).content
    return build


@st.fragment(run_every=1.0)
def progress(tid: str) -> None:
    """Polls only while the task is active. When it finishes, one full rerun renders the result once,
    so a finished answer (and its charts) is not redrawn every second and Recent work agrees with it."""
    t = api.get(f"/v1/tasks/{tid}")
    if t["status"] not in ACTIVE:
        st.rerun(scope="app")
    head = st.columns([6, 1], vertical_alignment="center")
    head[0].markdown(f"**{md(t['title'])}**")
    with head[1]:
        status_badge(t["status"], t.get("outcome"))
    steps = t["steps"]
    done = sum(s["status"] == "done" for s in steps)
    current = next((s for s in steps if s["status"] == "running"), steps[-1] if steps else None)
    label = PHASES.get(current["phase"], current["phase"]) if current else "Starting"
    st.progress(done / max(len(steps), 1), text=f"{label} · {done} of {len(steps)} steps")
    if t["status"] == "awaiting_approval":
        st.info("A step needs an admin's approval before Lyst continues.", icon=":material/approval:")
        st.page_link("app_pages/work.py", label="Open approvals in Work", icon=":material/arrow_forward:")


def result(t: dict) -> None:
    tid = t["id"]
    head = st.columns([6, 1], vertical_alignment="center")
    head[0].markdown(f"**{md(t['title'])}**")
    with head[1]:
        status_badge(t["status"], t.get("outcome"))
    if t["status"] in ("failed", "cancelled"):
        st.error(t.get("error") or "This task was cancelled.", icon=":material/error:")
        if t["status"] == "failed" and st.button("Retry", icon=":material/refresh:", key=f"retry_{tid}"):
            api.post(f"/v1/tasks/{tid}/retry")
            st.rerun()
        return
    if not t["result"]:
        return
    answer(t["result"])
    if t["result"]["status"] == "complete":
        d = st.columns([1, 1, 1, 1, 3])
        for i, (kind, label) in enumerate(EXPORTS):
            d[i].download_button(label, _export(tid, kind), file_name=f"lyst-{tid}.{kind}", mime=MIME[kind],
                                 key=f"dl_{kind}_{tid}", icon=":material/download:", type="tertiary",
                                 on_click="ignore")
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


# the open task lives in the URL too, so a reload, a bookmark or a shared link returns to the same work
if not st.session_state.get("task_id") and (q_tid := st.query_params.get("task")):
    st.session_state.task_id = q_tid
tid = st.session_state.get("task_id")
if tid and st.query_params.get("task") != tid:
    st.query_params["task"] = tid
if tid:
    try:
        task = api.get(f"/v1/tasks/{tid}")
    except APIError as e:
        task = None
        st.error(str(e))
    if task and task["status"] in ACTIVE:
        progress(tid)
    elif task:
        result(task)

st.divider()
st.markdown("##### Recent work")
try:
    recent = api.get(f"/v1/workspaces/{ws}/tasks")[:8]
except APIError:
    recent = []
for t in recent:
    row = st.columns([6, 1.2, 1], vertical_alignment="center")
    row[0].markdown(md(t["title"]))
    with row[1]:
        status_badge(t["status"], t.get("outcome"))
    if row[2].button("Open", key=f"open_{t['id']}", type="tertiary"):
        st.session_state.task_id = t["id"]
        st.rerun()
