# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Home: where you ask Lyst. Ask; see what Lyst is doing; read a verified answer at the depth you need; continue."""

import pandas as pd
import streamlit as st

from lyst.ui import anticipate, collab, decisions, governance, kit, onboarding, scenarios, voice
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import answer, md, task_state

api, ws = client(), workspace()
ACTIVE = {"queued", "running", "paused", "awaiting_approval"}
PHASES = {"understand": "Reading the request", "contextualize": "Finding definitions and data",
          "plan": "Planning the analysis", "investigate": "Investigating", "execute": "Running governed queries",
          "hypothesize": "Forming explanations", "test": "Testing explanations", "validate": "Checking the result",
          "deliver": "Writing the answer", "learn": "Recording what was learned", "improve": "Improving a capability"}
EXPORTS = (("pdf", "PDF briefing"), ("pptx", "Slides"), ("xlsx", "Excel workbook"), ("md", "Markdown"))
SENSITIVITY = {4.0: "Big moves only", 3.0: "Notable moves", 2.0: "Any unusual move"}
REPEAT = {604800: "Every week", 2592000: "Every month", 86400: "Every day"}
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
    try:
        out = api.ask(ws, q)
    except APIError as e:
        if e.status == 429:  # the workspace's queue or monthly budget is full
            st.warning("This workspace is at its limit right now, so Lyst could not start this. "
                       f"{e} Try again shortly, or ask an administrator to raise the limit in Settings › Workspace.",
                       icon=":material/hourglass_top:")
        else:
            st.error(str(e), icon=":material/error:")
        return
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
    voice.mic(open_task)


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


def get_started(datasets: list[dict], metrics: list[dict]) -> None:
    """The first run teaches by doing: four steps, each linking to where it is done; gone once done or dismissed."""
    if st.session_state.get("hide_get_started"):
        return
    admin = st.session_state.get("role") in ("admin", "owner")
    if admin:
        try:
            model = bool(api.get("/health").get("model"))
        except APIError:
            model = True
        extra = [("Connect a model (optional)", "Richer explanations. Every number still comes from your data.", model,
                  "app_pages/settings.py")]
    else:
        extra = []
    if onboarding.card(extra=extra):
        return
    try:
        asked = bool(api.get(f"/v1/workspaces/{ws}/tasks"))
        watching = bool(api.get(f"/v1/workspaces/{ws}/monitors") or api.get(f"/v1/workspaces/{ws}/schedules"))
        model = bool(api.get("/health").get("model")) if admin else True
    except APIError:
        return
    steps = [("Add your data", "Upload a spreadsheet or connect a database.", bool(datasets), "app_pages/data.py", None),
             ("Ask a first question", "Pick a suggestion below, or type your own.", asked, None, None),
             ("Keep watch", "From an answer: Watch this metric, or Repeat it weekly.", watching, "app_pages/work.py",
              {"view": "recurring"})]
    if admin:
        steps.append(("Connect a model (optional)", "Richer explanations. Every number still comes from your data.",
                      model, "app_pages/settings.py", None))
    left = sum(not s[2] for s in steps)
    if not left:
        return
    with st.container(border=True):
        top = st.columns([6, 1], vertical_alignment="center")
        top[0].markdown(f"**Get started** · {len(steps) - left} of {len(steps)} done")
        if top[1].button("Hide", type="tertiary", key="gs_hide", width="stretch"):
            st.session_state.hide_get_started = True
            st.rerun()
        for i, (title, body, ok, page, qp) in enumerate(steps):
            c = st.columns([0.35, 6, 1.4], vertical_alignment="center")
            c[0].markdown(":material/check_circle:" if ok else ":material/radio_button_unchecked:")
            c[1].markdown(f"~~{title}~~" if ok else f"**{title}**  \n{body}")
            if not ok and page and c[2].button("Go", key=f"gs_{i}", type="tertiary", width="stretch"):
                st.switch_page(page, query_params=qp)


def _pulse_data() -> list[dict]:
    """Cached per person and workspace for five minutes: a query per metric is cheap once, not on every rerun."""
    import time  # noqa: PLC0415

    hit = st.session_state.get("_pulse")
    if hit and hit[0] == ws and time.time() - hit[1] < 300:
        return hit[2]
    try:
        data = api.get(f"/v1/workspaces/{ws}/pulse")
    except APIError:
        data = []
    st.session_state["_pulse"] = (ws, time.time(), data)
    return data


def pulse() -> None:
    """The business at a glance: each approved metric's last complete month against the one before. A tile opens
    'why did it change?', so the status page leads straight into an explanation."""
    tiles = _pulse_data()[:6]
    if not tiles:
        return
    kit.label(f"This month at a glance · {tiles[0]['period']}")
    cols = st.columns(3)
    for i, t in enumerate(tiles):
        with cols[i % 3].container(border=True):
            st.metric(t["title"], t["value_text"], t["change_text"] + " vs prior month",
                      delta_color="off" if t["good"] is None else ("normal" if t["good"] == (t["change"] > 0) else "inverse"),
                      chart_data=t["history"], chart_type="line", border=False)
            if st.button("Why?", key=f"pulse_{t['metric']}", type="tertiary", icon=":material/arrow_outward:",
                         width="stretch"):
                submit(f"Why did {kit.lower(t['title'])} change last month?")
                st.rerun()


def home() -> None:
    kit.header("What should Lyst work on?",
               "Ask in plain words. Lyst works it out from your approved numbers, checks every figure and shows why.")
    ask_box(compact=False)
    try:
        metrics = api.get(f"/v1/workspaces/{ws}/metrics")
        datasets = api.get(f"/v1/workspaces/{ws}/datasets")
    except APIError:
        metrics, datasets = [], []
    anticipate.home_line()
    get_started(datasets, metrics)
    if datasets:
        pulse()
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
    st.caption(f"Lyst can answer about {len(metrics)} approved measure{'s' * (len(metrics) != 1)} here ("
               + ", ".join(m["title"] for m in metrics[:6]) + ("…" if len(metrics) > 6 else "")
               + "): why they changed, what drove them, a breakdown, a trend or a forecast.")

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


def _secs(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


@st.dialog("How this ran", width="large")
def how_it_ran(task: str) -> None:
    """Where the time, model calls and queries went, step by step: the trail from this answer back to the engine."""
    try:
        tr = api.get(f"/v1/tasks/{task}/trace")
    except APIError as e:
        st.error(str(e))
        return
    s = tr["summary"]
    parts = [f"{_secs(s['wall_ms'])} of work" + (f" after {_secs(s['queued_ms'])} in the queue" if s["queued_ms"] else ""),
             f"{s['steps']} steps"]
    parts.append(f"{s['model_calls']} model call{'s' * (s['model_calls'] != 1)}, {_secs(s['model_ms'])}"
                 + (f", ${s['cost_usd']:.4f}" if s["cost_usd"] else "")
                 + (f", {s['model_failures']} failed over" if s["model_failures"] else "")
                 if s["model_calls"] else "no model calls")
    parts.append(f"{s['queries']} governed quer{'ies' if s['queries'] != 1 else 'y'}, {_secs(s['query_ms'])}"
                 + (f", {s['rejected_queries']} refused" if s["rejected_queries"] else ""))
    st.markdown(md(" · ".join(parts)))
    if s.get("slowest_step"):
        st.caption(f"Slowest step: {md(s['slowest_step'].replace('_', ' '))}.")
    st.dataframe(pd.DataFrame([{"Step": f"{x['seq'] + 1}. {PHASES.get(x['phase'], x['phase'])}", "Capability": x["capability"],
                                "Status": x["status"], "Time": _secs(x["ms"]), "Model calls": x["model_calls"],
                                "Queries": x["queries"], "Error": x["error"] or ""} for x in tr["steps"]]),
                 hide_index=True, width="stretch")
    if tr["model_calls"]:
        st.markdown("**Model calls**")
        st.dataframe(pd.DataFrame([{"Purpose": c["purpose"], "Model": c["model"], "Level": c["tier"] or "", "Outcome": c["outcome"],
                                    "Time": _secs(c["latency_ms"]), "Tokens in": c["input_tokens"], "Tokens out": c["output_tokens"],
                                    "Cost": c["cost_usd"]} for c in tr["model_calls"]]),
                     hide_index=True, width="stretch",
                     column_config={"Cost": st.column_config.NumberColumn("Cost", format="$%.5f")})
    st.caption(f"Task {task}. Model calls and queries are matched to a step when exactly one step was running at the time.")


def act_bar(task: str, t: dict, r: dict) -> None:
    """Every recommendation is a button: watch the metric, repeat the analysis, take the answer away, see how it ran."""
    editor = st.session_state.get("role") in ("editor", "admin", "owner")
    sup = r.get("supporting") or {}
    metric = (sup.get("metric") or {}).get("spec") or {}
    wf = sup.get("workflow") or {}
    wf_id = (wf.get("used") or {}).get("id") or (wf.get("compiled") or {}).get("id")
    done = r["status"] == "complete"
    cols = st.columns([1.5, 1.2, 1.2, 1.1, 1.1, 1.3], vertical_alignment="center")
    if done and editor and metric.get("name"):
        name = kit.lower(metric.get("title") or metric["name"])
        with cols[0].popover("Watch this metric", icon=":material/monitor_heart:", width="stretch"):
            st.caption(f"Lyst checks {name} on its own and investigates when it moves unusually. "
                       "An admin approves it once.")
            sens = st.radio("Tell me about", list(SENSITIVITY), format_func=SENSITIVITY.get, index=1,
                            key=f"w_sens_{task}", help="Big moves only alerts beyond 4 standard deviations of the "
                            "usual change, notable beyond 3, any unusual move beyond 2.")
            grain = st.radio("Looking at", ["month", "quarter"], format_func=lambda g: f"Each {g}", horizontal=True,
                             key=f"w_grain_{task}")
            if st.button("Start watching", type="primary", key=f"w_go_{task}", width="stretch"):
                kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/monitors", {
                    "metric": metric["name"], "grain": grain, "threshold_sigma": sens, "every_seconds": 86400}),
                    f"Watching {name}. It starts once an admin approves it.", icon=":material/monitor_heart:")
    if done and editor:
        with cols[1].popover("Repeat it", icon=":material/event_repeat:", width="stretch"):
            st.caption("Lyst asks this again on its own, for the latest period, and keeps the results in Work.")
            ev = st.radio("How often", list(REPEAT), format_func=REPEAT.get, key=f"r_ev_{task}")
            if st.button("Repeat it", type="primary", key=f"r_go_{task}", width="stretch"):
                question = ((t.get("request") or {}).get("question") or t["title"]).strip()
                kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workflows/{wf_id}/schedule", {"every_seconds": ev})
                            if wf_id else api.post(f"/v1/workspaces/{ws}/schedules",
                                                   {"title": t["title"], "question": question, "every_seconds": ev}),
                            "Scheduled. It starts once an admin approves it.", icon=":material/event_repeat:")
    if done:
        with cols[2].popover("Download", icon=":material/download:", width="stretch"):
            for kind, text in EXPORTS:
                st.download_button(text, _export(task, kind), file_name=f"lyst-{task}.{kind}", mime=MIME[kind],
                                   key=f"dl_{kind}_{task}", type="tertiary", on_click="ignore", width="stretch")
    with cols[3].popover("Share", icon=":material/group:", width="stretch"):
        st.caption("Hand this answer to a colleague, or ask someone to check it before it is relied on.")
        how = st.segmented_control("Share how", ["Assign", "Ask for a review"], default="Assign", key=f"sh_{task}",
                                   label_visibility="collapsed") or "Assign"
        if how == "Assign":
            collab.assign("task", task, key=task)
        else:
            collab.request_review("task", task, f"Check: {t['title']}", key=task)
    if cols[4].button("Discuss", type="tertiary", icon=":material/forum:", key=f"disc_{task}", width="stretch"):
        collab.discuss("task", task, t["title"])
    if cols[5].button("How this ran", type="tertiary", icon=":material/timeline:", key=f"trace_{task}", width="stretch"):
        how_it_ran(task)


def explore_row(task: str, t: dict, r: dict) -> None:
    """From an answer to a choice: move its drivers (What if…?) or weigh options (Compare options)."""
    if r.get("status") != "complete":
        return
    metric = ((r.get("supporting") or {}).get("metric") or {}).get("spec") or {}
    models = scenarios.models_for(metric["name"]) if metric.get("name") else []
    editor = st.session_state.get("role") in ("editor", "admin", "owner")
    if not models and not editor:
        return
    c = st.columns([2, 2, 4])
    if models and c[0].button(f"What if…? Explore {kit.lower(metric.get('title') or metric['name'])}", type="tertiary",
                              icon=":material/tune:", key=f"wi_{task}", width="stretch"):
        st.session_state.pop(f"sc_prev_{models[0]}", None)
        scenarios.explore(models[0])
    if editor and c[1].button("Compare options", type="tertiary", icon=":material/balance:", key=f"dec_{task}",
                              width="stretch"):
        decisions.new(task_id=task, question="")
    if (did := st.session_state.pop("open_decision", None)):
        decisions.detail(did)


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
        b = st.columns([1.3, 1.3, 5])
        if t["status"] == "failed" and b[0].button("Try again", type="primary", icon=":material/refresh:", width="stretch"):
            kit.guarded(lambda: api.post(f"/v1/tasks/{task}/retry"), "Running it again")
            st.rerun()
        if b[1].button("How this ran", type="tertiary", icon=":material/timeline:", key=f"trace_{task}", width="stretch"):
            how_it_ran(task)
    elif t["result"]:
        r = t["result"]
        voice.read_back(task)
        answer(r, key=task, pdf=_export(task, "pdf") if r["status"] == "complete" else None)
        act_bar(task, t, r)
        governance.ask_row(task)
        explore_row(task, t, r)
        anticipate.next_steps(task)
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
