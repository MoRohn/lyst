# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Home: where you ask Lyst. Ask; see what Lyst is doing; read a verified answer at the depth you need; continue."""

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import answer, task_state

api, ws = client(), workspace()
ACTIVE = {"queued", "running", "paused", "awaiting_approval"}
PHASES = {"understand": "Reading the request", "contextualize": "Finding definitions and data",
          "plan": "Planning the analysis", "investigate": "Investigating", "execute": "Running governed queries",
          "hypothesize": "Forming explanations", "test": "Testing explanations", "validate": "Checking the result",
          "deliver": "Writing the answer", "learn": "Recording what was learned", "improve": "Improving a capability"}
EXPORTS = (("pdf", "PDF briefing"), ("pptx", "Deck"), ("xlsx", "Excel"), ("md", "Markdown"))
MIME = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "md": "text/markdown",
        "pdf": "application/pdf", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
TONE = {"green": "ok", "orange": "warn", "red": "bad", "blue": "run", "gray": "off"}


def open_task(tid: str | None) -> None:
    st.session_state.task_id = tid
    if tid:
        st.query_params["task"] = tid
    else:
        st.query_params.pop("task", None)


def submit(q: str) -> None:
    out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/tasks", {"question": q}))
    if out:
        open_task(out["task_id"])


# the open task lives in the URL too, so a reload, a bookmark or a shared link returns to the same work
if "task_id" not in st.session_state and (q_tid := st.query_params.get("task")):
    st.session_state.task_id = q_tid
tid = st.session_state.get("task_id")


def ask_box(compact: bool) -> None:
    with st.form("ask", clear_on_submit=True, border=False):
        q = st.text_area("Request", label_visibility="collapsed", height=68 if compact else 96,
                         placeholder=("Ask a follow-up or something new" if compact else
                                      "Ask a question or describe the work, for example: why did operating margin fall "
                                      "last quarter, and what should we do about it?"))
        c = st.columns([1.2, 6])
        if c[0].form_submit_button("Ask Lyst", type="primary", icon=":material/arrow_upward:", width="stretch") \
                and q.strip():
            submit(q.strip())
            st.rerun()


# ----------------------------------------------------------------------------------------- home

def suggestions(metrics: list[dict]) -> list[str]:
    """Questions this workspace can answer well, from its own approved metrics, not a hard-coded list."""
    out: list[str] = []
    for m in metrics[:4]:
        t = kit.lower(m["title"])
        if m.get("kind") == "ratio" or m.get("format") == "percent":
            out.append(f"Why did {t} change last quarter?")
        elif m.get("dimensions"):
            out.append(f"What was {t} last quarter by {m['dimensions'][0].replace('_', ' ')}?")
        else:
            out.append(f"Forecast {t}")
    if metrics:
        out.append(f"Forecast {kit.lower(metrics[0]['title'])}")
    return list(dict.fromkeys(out))[:5]


def home() -> None:
    kit.header("What should Lyst work on?",
               "Lyst plans the work, runs it on your governed data, checks the result and shows its evidence.")
    ask_box(compact=False)
    try:
        metrics = api.get(f"/v1/workspaces/{ws}/metrics")
        datasets = api.get(f"/v1/workspaces/{ws}/datasets")
    except APIError:
        metrics, datasets = [], []
    if not datasets:
        if kit.empty(":material/database:", "Connect data to begin",
                     "Lyst answers from data you load or connect. Add a file or a database, and approved metrics on it "
                     "become questions you can ask.", action="Add data", key="home_add"):
            st.switch_page("app_pages/data.py")
        return
    pick = st.pills("Try", suggestions(metrics), label_visibility="collapsed", key="suggest")
    if pick:
        submit(pick)
        st.rerun()
    st.caption(f"Lyst can use {len(datasets)} dataset{'s' * (len(datasets) != 1)} and "
               f"{len(metrics)} approved metric{'s' * (len(metrics) != 1)} in this workspace: "
               + ", ".join(m["title"] for m in metrics[:6]) + ("…" if len(metrics) > 6 else "") + ".")

    needs: list[tuple[str, str, str, str]] = []  # (title, meta, action label, id)
    try:
        for f in api.get(f"/v1/workspaces/{ws}/inbox"):
            if f["read_status"] == "unread" and f.get("task_id"):
                needs.append((f["summary"], f"Found by a monitor · {kit.when(f['created_at'])}", "See why",
                              f"f:{f['id']}:{f['task_id']}"))
        if st.session_state.get("role") in ("admin", "owner"):
            for ap in api.get(f"/v1/workspaces/{ws}/approvals"):
                needs.append((ap["summary"], f"Waiting for approval · {kit.when(ap['created_at'])}", "Review", "approvals"))
        recent = api.get(f"/v1/workspaces/{ws}/tasks")[:30]
    except APIError:
        recent = []
    for t in recent:
        if t.get("outcome") in ("needs_input", "unverified"):
            needs.append((t["title"], f"{task_state(t['status'], t['outcome'])[0]} · {kit.when(t['created_at'])}",
                          "Open", f"t:{t['id']}"))
    if needs:
        kit.label(f"Needs you · {len(needs)}")
        shown = needs[:6]
        for i, (title, meta, act, aid) in enumerate(shown):
            if kit.row(title, meta, actions=[(act, "go")], key=f"need{i}", divider=i < len(shown) - 1):
                if aid == "approvals":
                    st.switch_page("app_pages/work.py", query_params={"view": "needs you"})
                kind, *rest = aid.split(":")
                if kind == "f":
                    kit.guarded(lambda r=rest: api.post(f"/v1/workspaces/{ws}/findings/{r[0]}", {"status": "acknowledged"}))
                    open_task(rest[1])
                else:
                    open_task(rest[0])
                st.rerun()
    done = [t for t in recent if t.get("outcome") not in ("needs_input", "unverified")][:6]
    if done:
        kit.label("Continue where you left off")
        for i, t in enumerate(done):
            text, color, _ = task_state(t["status"], t.get("outcome"))
            if kit.row(t["title"], kit.when(t["created_at"]), status=(text, TONE.get(color, "info")),
                       actions=[("Open", "open")], key=f"rec{t['id']}", divider=i < len(done) - 1):
                open_task(t["id"])
                st.rerun()


# ----------------------------------------------------------------------------------------- a task

def _export(task: str, kind: str):
    """Built only when clicked: one click, not 'make' then 'save'."""
    def build() -> bytes:
        a = api.post(f"/v1/tasks/{task}/artifacts/{kind}")
        return api.http.get(f"/v1/artifacts/{a['id']}", headers=api._h()).content
    return build


@st.fragment(run_every=1.0)
def progress(task: str) -> None:
    """Polls only while the task runs; when it ends, one full rerun shows the result once."""
    t = api.get(f"/v1/tasks/{task}")
    if t["status"] not in ACTIVE:
        st.rerun(scope="app")
    steps = t["steps"]
    done = sum(s["status"] == "done" for s in steps)
    current = next((s for s in steps if s["status"] == "running"), steps[-1] if steps else None)
    label = PHASES.get(current["phase"], current["phase"]) if current else "Starting"
    st.progress(done / max(len(steps), 1), text=f"{label} · {done} of {len(steps)} steps")
    if t["status"] == "awaiting_approval":
        st.info("A step needs an admin's approval before Lyst continues.", icon=":material/approval:")
        st.page_link("app_pages/work.py", label="Review it in Work", icon=":material/arrow_forward:",
                     query_params={"view": "needs you"})
    elif st.button("Stop", type="tertiary", icon=":material/stop_circle:", key=f"stop_{task}"):
        kit.guarded(lambda: api.post(f"/v1/tasks/{task}/cancel"), "Stopped")


def task_view(task: str) -> None:
    top = st.columns([6, 1.4], vertical_alignment="center")
    if top[1].button("New question", icon=":material/add:", type="tertiary", width="stretch"):
        open_task(None)
        st.rerun()
    try:
        t = api.get(f"/v1/tasks/{task}")
    except APIError as e:
        top[0].error(str(e))
        return
    # the heading is the whole request (list titles are shortened); long requests use a smaller size
    full = ((t.get("request") or {}).get("question") or t["title"]).strip()
    size = "" if len(full) <= 90 else ' style="font-size:1.2rem;line-height:1.35"'
    top[0].html(f'<h1 class="lyst-title"{size}>{kit._esc(full)}</h1>')
    if t["status"] in ACTIVE:
        progress(task)
        return
    if t["status"] in ("failed", "cancelled"):
        st.error(t.get("error") or "This task was stopped before it finished.", icon=":material/error:")
        if t["status"] == "failed" and st.button("Try again", type="primary", icon=":material/refresh:"):
            kit.guarded(lambda: api.post(f"/v1/tasks/{task}/retry"), "Running it again")
            st.rerun()
    elif t["result"]:
        r = t["result"]
        answer(r, key=task)
        if r["status"] == "complete":
            cols = st.columns([1.2, 1, 1, 1.2, 3])
            for col, (kind, text) in zip(cols, EXPORTS, strict=False):
                col.download_button(text, _export(task, kind), file_name=f"lyst-{task}.{kind}", mime=MIME[kind],
                                    key=f"dl_{kind}_{task}", icon=":material/download:", type="tertiary",
                                    on_click="ignore", width="stretch")
        metric = ((r.get("supporting") or {}).get("metric") or {}).get("spec", {})
        if metric:
            m = kit.lower(metric["title"])
            opts = [f"Show the {m} trend over time", f"Forecast {m}"]
            opts += [f"What was {m} last quarter by {d.replace('_', ' ')}?" for d in metric.get("dimensions", [])]
            kit.label("Follow up")
            nxt = st.pills("Follow up", opts, label_visibility="collapsed", key=f"fu_{task}")
            if nxt:
                submit(nxt)
                st.rerun()
    ask_box(compact=True)


if tid:
    task_view(tid)
else:
    home()
