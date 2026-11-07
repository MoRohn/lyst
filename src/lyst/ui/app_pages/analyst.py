# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Home: where you ask Lyst. Ask; see what Lyst is doing; read a verified answer at the depth you need; continue."""

import pandas as pd
import streamlit as st

from lyst.ui import (
    agent,
    anticipate,
    collab,
    decisions,
    governance,
    kit,
    onboarding,
    publish,
    scenarios,
    voice,
    zone,
)
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
    st.session_state.pop("_agent", None)  # the new request shows in what Lyst is doing at once
    from lyst.ui.agent_bar import _settle  # noqa: PLC0415

    _settle(api, out["task_id"])  # a quick reply opens finished, not on a progress screen
    open_task(out["task_id"])


# the open task lives in the URL too, so a reload, a bookmark or a shared link returns to the same work
if "task_id" not in st.session_state and (q_tid := st.query_params.get("task")):
    st.session_state.task_id = q_tid
tid = st.session_state.get("task_id")


def ask_box() -> None:
    """A one-line follow-up under the conversation; the agent bar above takes anything, from anywhere."""
    with st.container(key="lyst_followup"):
        q = st.chat_input("Ask a follow-up or something new", key="ask_followup")
    if q and q.strip():
        submit(q.strip())
        st.rerun()


# ----------------------------------------------------------------------------------------- home

def suggestions(metrics: list[dict]) -> list[str]:
    """Questions this workspace can answer well, from its own approved metrics, not a hard-coded list."""
    out: list[str] = []
    # ratios first: "why did it change" is the question a measure of performance invites
    metrics = sorted(metrics, key=lambda m: not (m.get("kind") == "ratio" or m.get("format") == "percent"))
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
             ("Keep watch", "From an answer: Watch this metric, or Repeat it weekly.", watching, "app_pages/brain.py",
              {"view": "work", "work": "recurring"})]
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


def get_started_folded(datasets: list[dict], metrics: list[dict]) -> None:
    """Get started stays one line once anything is done: it opens in place, so the page leads with answers."""
    try:
        stat = onboarding.status()
    except APIError:
        stat = None
    if stat and (stat["state"] == "complete" or st.session_state.get("hide_get_started")):
        return
    steps = (stat or {}).get("steps") or []
    done = sum(s_["done"] for s_ in steps if not s_["optional"])
    need = sum(not s_["optional"] for s_ in steps)
    if stat and done == 0:  # nothing done yet: the guide is the page
        get_started(datasets, metrics)
        return
    label = f"Get started · {done} of {need} done" if stat else "Get started"
    with st.expander(label, icon=":material/flag:"):
        get_started(datasets, metrics)


def home() -> None:
    """Home is a monitor first and a form second: what Lyst is doing, the question box, what waits on you, then the
    answers themselves. Everything else (how an answer was reached, older work) is one click deeper."""
    kit.header("What should Lyst work on?")
    try:
        metrics = api.get(f"/v1/workspaces/{ws}/metrics")
        datasets = api.get(f"/v1/workspaces/{ws}/datasets")
    except APIError:
        metrics, datasets = [], []
    anticipate.home_line()
    get_started_folded(datasets, metrics)
    if not datasets:
        if kit.empty(":material/database:", "Connect data to begin",
                     "Lyst answers from data you load or connect. Add a file or a database, and approved metrics on it "
                     "become questions you can ask.", action="Add data", key="home_add"):
            st.switch_page("app_pages/data.py")
        return
    names = ", ".join(m["title"] for m in metrics[:3]) + (f" and {len(metrics) - 3} more" if len(metrics) > 3 else "")
    pick = st.pills("Try", suggestions(metrics)[:3], label_visibility="collapsed", key="suggest",
                    help=f"Lyst can answer about {len(metrics)} approved measure{'s' * (len(metrics) != 1)} here "
                         f"({names}): why they changed, what drove them, a breakdown, a trend or a forecast.")
    if pick:
        submit(pick)
        st.rerun()
    recent = agent.state(ws)["tasks"][:30]
    zone.board("home")  # what Lyst suggests, what waits on you, what it is doing and what it finished
    anticipate.top_suggestion()
    rest = agent.answers(recent, n=2)
    # what Lyst has made, as artifacts to open (dashboards, briefings, saved analyses)
    from lyst.ui import artifacts  # noqa: PLC0415

    artifacts.shelf(title="Made for you", n=3, key="home", exclude=("analysis",))
    earlier = [t for t in rest if t.get("outcome") not in ("needs_input", "unverified")][:8]
    if earlier:
        with st.expander(f"Earlier answers · {len(earlier)}", icon=":material/history:"):
            for i, t in enumerate(earlier):
                text, color, _ = task_state(t["status"], t.get("outcome"))
                if kit.row(t["title"], kit.when(t["created_at"]), status=(text, TONE.get(color, "info")),
                           actions=[("Open", "open")], key=f"rec{t['id']}", divider=i < len(earlier) - 1):
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
        st.page_link("app_pages/brain.py", label="Review it in Brain › Work", icon=":material/arrow_forward:",
                     query_params={"view": "work", "work": "needs you"})
    elif st.button("Stop", type="tertiary", icon=":material/stop_circle:", key=f"stop_{task}"):
        kit.guarded(lambda: api.post(f"/v1/tasks/{task}/cancel"), "Stopped")


def _secs(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def _tokens(step: dict) -> str:
    """A step's tokens as "155 (95 in, 60 out)", or a dash when none of its model calls reported any."""
    if step.get("tokens") is None:
        return "—"
    return f"{step['tokens']:,} ({step.get('input_tokens') or 0:,} in, {step.get('output_tokens') or 0:,} out)"


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
                                "Tokens": _tokens(x), "Queries": x["queries"], "Error": x["error"] or ""}
                               for x in tr["steps"]]),
                 hide_index=True, width="stretch",
                 column_config={"Tokens": st.column_config.TextColumn(
                     "Tokens", help="Tokens in and out across the model calls matched to this step.")})
    if tr["model_calls"]:
        st.markdown("**Model calls**")
        st.dataframe(pd.DataFrame([{"Purpose": c["purpose"], "Model": c["model"], "Level": c["tier"] or "", "Outcome": c["outcome"],
                                    "Time": _secs(c["latency_ms"]), "Tokens in": c["input_tokens"], "Tokens out": c["output_tokens"],
                                    "Cost": c["cost_usd"]} for c in tr["model_calls"]]),
                     hide_index=True, width="stretch", placeholder="—",
                     column_config={"Tokens in": st.column_config.NumberColumn("Tokens in", format="%d"),
                                    "Tokens out": st.column_config.NumberColumn("Tokens out", format="%d"),
                                    "Cost": st.column_config.NumberColumn("Cost", format="$%.5f")})
    st.caption(f"Task {task}. Model calls and queries are matched to a step when exactly one step was running at the time.")


VERDICTS = {"correct": ":material/check: Right", "partly": ":material/rule: Partly", "incorrect": ":material/close: Wrong"}
VERDICT_SAID = {"correct": "Thanks. Lyst will rely on this answer with more confidence.",
                "partly": "Thanks. Noted on this answer.",
                "incorrect": "Thanks. Lyst stops reusing this answer and works the next one out afresh."}


def verdict(task: str) -> None:
    """Was this right? One click records a person's verdict (docs/LEARNING.md); a wrong or partial answer can take a
    correction. A new verdict replaces the earlier one, and Undo withdraws it."""
    aid = f"a_{task}"
    try:
        fb = api.get(f"/v1/workspaces/{ws}/analyses/{aid}/feedback")
    except APIError:
        return  # no analysis behind this result (a reply), so nothing to judge
    me = st.session_state.get("me_id")
    mine = next((f for f in fb.get("feedback") or [] if f.get("given_by") == me and not f.get("withdrawn_at")), None)
    cur = mine["verdict"] if mine else None
    st.caption("Is this right?", width="content")
    pick = st.segmented_control("Is this right?", list(VERDICTS), format_func=VERDICTS.get, default=cur,
                                  key=f"vd_{task}_{cur}", label_visibility="collapsed")
    if mine and st.button("Undo", type="tertiary", key=f"vd_undo_{task}", help="Withdraw your verdict"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/feedback/{mine['id']}/withdraw"), "Verdict withdrawn")
        st.rerun()
    if pick and pick != cur:
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/analyses/{aid}/feedback", {"verdict": pick}),
                       VERDICT_SAID[pick], icon=":material/how_to_reg:") is not None:
            st.session_state[f"vd_fix_{task}"] = pick != "correct"
        st.rerun()
    st.session_state[f"vd_cur_{task}"] = cur


def verdict_fix(task: str) -> None:
    """After Partly or Wrong: an optional correction, which Lyst recalls ahead of what it observed."""
    cur = st.session_state.get(f"vd_cur_{task}")
    if not (cur and cur != "correct" and st.session_state.get(f"vd_fix_{task}")):
        return
    aid = f"a_{task}"
    with st.form(f"vd_form_{task}", border=True):
        fix = st.text_area("What should it have said? (optional)", height=68, key=f"vd_fix_text_{task}",
                           placeholder="For example: EMEA's Q3 figures exclude the September restatement.")
        b = st.columns([1.2, 1, 5])
        if b[0].form_submit_button("Save", type="primary", width="stretch"):
            kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/analyses/{aid}/feedback",
                                         {"verdict": cur, "correction": fix.strip() or None}),
                        "Saved. Lyst recalls the correction next time." if fix.strip() else "Saved")
            st.session_state[f"vd_fix_{task}"] = False
            st.rerun()
        if b[1].form_submit_button("Skip", type="tertiary", width="stretch"):
            st.session_state[f"vd_fix_{task}"] = False
            st.rerun()


def _watch_form(task: str, metric: dict) -> None:
    name = kit.lower(metric.get("title") or metric["name"])
    st.caption(f"Lyst checks {name} on its own and investigates when it moves unusually. An admin approves it once.")
    sens = st.radio("Tell me about", list(SENSITIVITY), format_func=SENSITIVITY.get, index=1,
                    key=f"w_sens_{task}", help="Big moves only alerts beyond 4 standard deviations of the "
                    "usual change, notable beyond 3, any unusual move beyond 2.")
    grain = st.radio("Looking at", ["month", "quarter"], format_func=lambda g: f"Each {g}", horizontal=True,
                     key=f"w_grain_{task}")
    if st.button("Start watching", type="primary", key=f"w_go_{task}", width="stretch"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/monitors", {
            "metric": metric["name"], "grain": grain, "threshold_sigma": sens, "every_seconds": 86400}),
            f"Watching {name}. It starts once an admin approves it.", icon=":material/monitor_heart:")


def _repeat_form(task: str, t: dict, wf_id: str | None) -> None:
    st.caption("Lyst asks this again on its own, for the latest period, and keeps the results in Work.")
    ev = st.radio("How often", list(REPEAT), format_func=REPEAT.get, key=f"r_ev_{task}")
    if st.button("Repeat it", type="primary", key=f"r_go_{task}", width="stretch"):
        question = ((t.get("request") or {}).get("question") or t["title"]).strip()
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workflows/{wf_id}/schedule", {"every_seconds": ev})
                    if wf_id else api.post(f"/v1/workspaces/{ws}/schedules",
                                           {"title": t["title"], "question": question, "every_seconds": ev}),
                    "Scheduled. It starts once an admin approves it.", icon=":material/event_repeat:")


def act_bar(task: str, t: dict, r: dict) -> None:
    """Four menus, each a place for one kind of next move: keep it current (watch or repeat), take it away
    (download), hand it on (share) and everything else (More: brief, discuss, what if, compare, how it ran and what
    the engine offers for this task)."""
    editor = st.session_state.get("role") in ("editor", "admin", "owner")
    sup = r.get("supporting") or {}
    metric = (sup.get("metric") or {}).get("spec") or {}
    wf = sup.get("workflow") or {}
    wf_id = (wf.get("used") or {}).get("id") or (wf.get("compiled") or {}).get("id")
    done = r["status"] == "complete"
    left, right = st.columns([1.15, 1], vertical_alignment="center")
    if done:
        with right.container(horizontal=True, horizontal_alignment="right", vertical_alignment="center", gap="small"):
            verdict(task)
    row = left.container(horizontal=True, vertical_alignment="center", gap="small")
    cols = [row] * 4
    if done and editor:
        with cols[0].popover("Keep watch", icon=":material/monitor_heart:", width="content",
                             help="Watch this metric, or repeat the question on a schedule"):
            how = st.segmented_control("Keep watch how", ["Watch this metric", "Repeat it"] if metric.get("name")
                                       else ["Repeat it"], default="Watch this metric" if metric.get("name")
                                       else "Repeat it", key=f"kw_{task}", label_visibility="collapsed")
            if how == "Watch this metric" and metric.get("name"):
                _watch_form(task, metric)
            elif how:
                _repeat_form(task, t, wf_id)
    if done:
        with cols[1].popover("Download", icon=":material/download:", width="content"):
            for kind, text in EXPORTS:
                st.download_button(text, _export(task, kind), file_name=f"lyst-{task}.{kind}", mime=MIME[kind],
                                   key=f"dl_{kind}_{task}", type="tertiary", on_click="ignore", width="stretch")
    with cols[2].popover("Share", icon=":material/group:", width="content"):
        st.caption("Hand this answer to a colleague, or ask someone to check it before it is relied on.")
        opts = ["Assign", "Ask for a review"] + (["Publish"] if done and editor else [])
        how = st.segmented_control("Share how", opts, default="Assign", key=f"sh_{task}",
                                   label_visibility="collapsed") or "Assign"
        if how == "Assign":
            collab.assign("task", task, key=task)
        elif how == "Publish":
            publish.form(task, t["title"], key=task)
        else:
            collab.request_review("task", task, f"Check: {t['title']}", key=task)
    with cols[3].popover("More", icon=":material/more_horiz:", width="content"):
        more_menu(task, t, r, metric, editor)


def more_menu(task: str, t: dict, r: dict, metric: dict, editor: bool) -> None:
    """The answer's less common moves, in one place."""
    if st.button("Discuss", type="tertiary", icon=":material/forum:", key=f"disc_{task}", width="stretch"):
        collab.discuss("task", task, t["title"])
    if r.get("status") == "complete":
        models = scenarios.models_for(metric["name"]) if metric.get("name") else []
        if models and st.button(f"What if…? Explore {kit.lower(metric.get('title') or metric['name'])}",
                                type="tertiary", icon=":material/tune:", key=f"wi_{task}", width="stretch"):
            st.session_state.pop(f"sc_prev_{models[0]}", None)
            scenarios.explore(models[0])
        if editor and st.button("Compare options", type="tertiary", icon=":material/balance:", key=f"dec_{task}",
                                width="stretch"):
            decisions.new(task_id=task, question="")
    if st.button("How this ran", type="tertiary", icon=":material/timeline:", key=f"trace_{task}", width="stretch"):
        how_it_ran(task)
    from lyst.ui import actions  # noqa: PLC0415 - what the engine offers for this task

    actions.menu("task", task, page="home", exclude=("task.open",), inline=True)


KIND_WORDS = {"dashboard": "Dashboard", "report": "Report", "workbook": "Workbook", "presentation": "Presentation",
              "briefing": "Briefing", "data_model": "Data model", "dataset": "Dataset", "metric": "Metric", "rule": "Rule",
              "monitor": "Watch", "workflow": "Repeatable analysis", "scenario": "Scenario model"}


def reply_view(task: str, r: dict) -> None:
    """A conversational reply: what Lyst said, the real things it found, and what it offers to do next. No Verified
    badge, evidence or exports, because nothing was computed."""
    talk = r.get("conversation") or {}
    with st.container(border=True):
        st.markdown("##### " + md(r.get("answer") or ""))
        for note in r.get("actions") or []:
            st.caption(f":material/info: {md(note)}")
        items = talk.get("items") or []
        if items:
            kit.label("In this workspace")
            for i, it in enumerate(items):
                kit.row(it.get("title") or it.get("id") or "", " · ".join(x for x in (KIND_WORDS.get(it.get("kind"), ""),
                                                                                    str(it.get("note") or "")) if x),
                        key=f"talk_item_{task}_{i}", divider=i < len(items) - 1)
        st.caption(":material/chat: A reply from what's in this workspace. Nothing was computed, so there was nothing to verify.")
    offers = talk.get("suggestions") or []
    if offers:
        kit.label("Want me to")
        nxt = st.pills("Want me to", offers, label_visibility="collapsed", key=f"offer_{task}")
        if nxt:
            submit(nxt)
            st.rerun()


def follow_ups(task: str, r: dict) -> None:
    """One row of follow-ups: how the answer was figured (its trust questions open together), then the metric's
    next questions."""
    metric = ((r.get("supporting") or {}).get("metric") or {}).get("spec", {})
    opts = [governance.ASK["how"]]
    if metric:
        m = kit.lower(metric["title"])
        opts += [f"Forecast {m}", f"Show the {m} trend over time"]
        opts += [f"What was {m} last quarter by {d.replace('_', ' ')}?" for d in metric.get("dimensions", [])][:1]
    nxt = st.pills("Follow up", opts, label_visibility="collapsed", key=f"fu_{task}")
    if nxt == governance.ASK["how"]:
        st.session_state.pop(f"fu_{task}", None)
        governance.explain(task, "how")
    elif nxt:
        submit(nxt)
        st.rerun()


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
    trig = (t.get("request") or {}).get("trigger") or {}
    if trig.get("activity_id"):  # started by an event rule: one calm line saying why, the details a click away
        from lyst.ui import activity  # noqa: PLC0415

        why = st.columns([6, 1.4], vertical_alignment="center")
        why[0].caption(f":material/bolt: Lyst started this on its own, from the rule \"{trig.get('rule_id', '')}\".")
        if why[1].button("Why", key=f"why_{task}", type="tertiary", icon=":material/info:", width="stretch"):
            activity.details(trig["activity_id"])
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
    elif t["result"] and t["result"].get("mode") == "conversation":
        voice.read_back(task)
        reply_view(task, t["result"])
    elif t["result"]:
        r = t["result"]
        voice.read_back(task)
        answer(r, key=task, pdf=_export(task, "pdf") if r["status"] == "complete" else None)
        act_bar(task, t, r)
        verdict_fix(task)
        if (did := st.session_state.pop("open_decision", None)):
            decisions.detail(did)
        anticipate.next_steps(task)
        follow_ups(task, r)
    ask_box()


if tid:
    task_view(tid)
else:
    home()
