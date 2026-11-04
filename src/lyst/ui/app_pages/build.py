# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Build: what Lyst makes and reuses. Verified workflows, dashboards, and governed skills."""

import io
import json

import pandas as pd
import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

api, ws = client(), workspace()
editor = st.session_state.get("role") in ("editor", "admin", "owner")
REPEAT = {86400: "Daily", 604800: "Weekly", 2592000: "Monthly"}


def open_task(tid: str) -> None:
    st.session_state.task_id = tid
    st.switch_page("app_pages/analyst.py", query_params={"task": tid})


# ------------------------------------------------------------------------------------ workflows

@st.dialog("Workflow", width="large")
def workflow_detail(w: dict) -> None:
    d = w["definition"]
    grain = (d["inputs"].get("period") or {}).get("grain")
    st.markdown(f"**{w['title']}**")
    st.caption(f"{md(d['purpose'])} · v{w['version']} · verified {w['verified_runs']}× · reused {w['runs']}×")
    if w["status"] == "stale":
        st.warning(f"Out of date: {w.get('invalid_reason')}. The next verified answer to this question rebuilds it.",
                   icon=":material/sync_problem:")
    else:
        c = st.columns([2, 1.2])
        period = c[0].text_input("For which period", placeholder=f"last {grain}" if grain else "", disabled=not grain,
                                 help="For example Q4 2026, Aug 2026 or 2025. Empty means the last complete one.")
        if c[1].button("Run it", type="primary", icon=":material/play_arrow:", width="stretch"):
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workflows/{w['id']}/run", {"period": period.strip() or None}))
            if r:
                open_task(r["task_id"])
        if grain and editor:
            c2 = st.columns([2, 1.2])
            ev = c2[0].selectbox("Repeat", list(REPEAT), format_func=REPEAT.get, index=1)
            if c2[1].button("Schedule", icon=":material/event_repeat:", width="stretch"):
                kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workflows/{w['id']}/schedule", {"every_seconds": ev}),
                            "Proposed. It starts once an admin approves it.")
    with st.expander("How it works", icon=":material/account_tree:"):
        st.markdown("**Plan** " + " → ".join(f"`{x['capability']}`" for x in d["steps"]))
        st.markdown("**Must pass** " + ", ".join(f"`{c}`" for c in d["validation"]["required_checks"]))
        st.markdown("**Depends on** " + ", ".join([f"`{n}` (schema {fp})" for n, fp in w["deps"]["datasets"].items()]
                                                  + [f"`{n}` v{v}" for n, v in w["deps"]["metrics"].items()]))
        st.caption(f"Needs {', '.join(w['permissions'])}. Compiled from {w['source_task']}, last verified by "
                   f"{w['last_verified_task']}.")
    if editor and st.button("Retire", type="tertiary", icon=":material/archive:"):
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workflows/{w['id']}/retire"), "Retired"):
            st.rerun()


def workflows() -> None:
    flows = api.get(f"/v1/workspaces/{ws}/workflows")
    if not flows:
        kit.empty(":material/bolt:", "No workflows yet", "When an answer is verified, Lyst saves how it got there. Asking "
                  "again, for any period, reuses that checked plan instead of working it out from scratch.")
        return
    st.caption("Saved analyses: verified answers Lyst can rerun for any period, or repeat on its own. A changed "
               "definition or data layout marks one out of date until it is verified again.")
    for i, w in enumerate(flows):
        grain = (w["definition"]["inputs"].get("period") or {}).get("grain")
        if kit.row(w["title"], f"Verified {w['verified_runs']}× · reused {w['runs']}×"
                   + (f" · by {grain}" if grain else "") + f" · last used {kit.when(w.get('last_used_at')) or 'not yet'}",
                   status=("Ready", "verified") if w["status"] == "active" else ("Out of date", "warn"),
                   actions=[("Open", "open")], key=f"w{w['id']}", divider=i < len(flows) - 1):
            workflow_detail(w)


# ----------------------------------------------------------------------------------- dashboards

@st.dialog("New dashboard", width="large")
def new_dashboard() -> None:
    metrics = api.get(f"/v1/workspaces/{ws}/metrics")
    st.caption("Lyst designs it on governed metrics, verifies every query, builds the app, checks every page and "
               "proposes the refresh schedule for approval.")
    picks = st.multiselect("Metrics", [m["name"] for m in metrics], max_selections=4,
                           format_func=lambda n: next(m["title"] for m in metrics if m["name"] == n))
    dims = sorted({d for m in metrics if m["name"] in picks for d in m["dimensions"]})
    by = st.selectbox("Split by", [None] + dims, format_func=lambda x: "Nothing" if x is None else x.replace("_", " "))
    every = st.selectbox("Refresh", ["weekly", "daily", "monthly"], format_func=str.capitalize)
    if st.button("Build it", type="primary", icon=":material/dashboard_customize:", disabled=not picks):
        titles = " and ".join(kit.lower(next(m["title"] for m in metrics if m["name"] == n)) for n in picks)
        q = f"Create an automated {titles} reporting dashboard" + (f" by {by.replace('_', ' ')}" if by else "") \
            + f", refreshed {every}"
        r = kit.guarded(lambda: api.ask(ws, q))
        if r:
            open_task(r["task_id"])


@st.dialog("Dashboard", width="large")
def dashboard_detail(d: dict) -> None:
    st.markdown(f"**{d['title']}**")
    dash = bool(d.get("checks") and any(c["name"] == "dash_check" for c in d["checks"]))
    st.caption(f"{'Plotly Dash' if dash else 'Streamlit'} app · refreshed {kit.when(d['last_refresh_at'])} · "
               f"{d['refresh_count']} refreshes")
    man = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/dashboards/{d['id']}"))
    if man and man.get("metrics"):
        cols = st.columns(len(man["metrics"]))
        for col, m in zip(cols, man["metrics"], strict=True):
            s_ = pd.read_parquet(io.BytesIO(api.http.get(
                f"/v1/workspaces/{ws}/dashboards/{d['id']}/files/{m['series_file']}", headers=api._h()).content)).sort_values("period")
            v = s_["value"].iloc[-1]
            shown = f"{v * 100:.1f}%" if m["format"] == "percent" else (f"${v / 1e6:.2f}M" if m["format"] == "currency" else f"{v:,.0f}")
            col.metric(m["title"], shown)
            col.line_chart(s_.set_index("period")["value"], height=150)
        st.caption("Checks: " + "; ".join(("✓ " if c["passed"] else "✕ ") + c["name"] for c in man["checks"]))
    c = st.columns(3)
    if c[0].button("Refresh now", icon=":material/refresh:"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/dashboards/{d['id']}/refresh"), "Refreshed")

    def bundle() -> bytes:
        return api.http.get(f"/v1/workspaces/{ws}/dashboards/{d['id']}/bundle", headers=api._h()).content

    c[1].download_button("Download app", bundle, file_name=f"{d['id']}.zip", mime="application/zip", on_click="ignore",
                         icon=":material/download:")
    st.caption("Run it anywhere: unzip, `pip install -r requirements.txt`, then `python app.py` (Dash) or "
               "`streamlit run app.py`.")
    if editor and c[2].button("Retire", type="tertiary", icon=":material/archive:"):
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/dashboards/{d['id']}/retire"),
                       "Retired: refreshes stop; the app download stays available"):
            st.rerun()


def dashboards() -> None:
    dbs = api.get(f"/v1/workspaces/{ws}/dashboards")
    if not dbs:
        if kit.empty(":material/dashboard:", "No dashboards yet", "Pick the metrics and how to split them; Lyst builds a "
                     "verified, self-refreshing dashboard you can run anywhere.",
                     action="New dashboard" if editor else None, key="db_empty"):
            new_dashboard()
        return
    for i, d in enumerate(dbs):
        tone = "ok" if d["status"] == "published" else ("off" if d["status"] == "retired" else "warn")
        if kit.row(d["title"], f"Refreshed {kit.when(d['last_refresh_at'])} · {d['refresh_count']} refreshes",
                   status=(d["status"].capitalize(), tone), actions=[("Open", "open")], key=f"db{d['id']}",
                   divider=i < len(dbs) - 1):
            dashboard_detail(d)


# --------------------------------------------------------------------------------------- skills

EXAMPLE_SOURCE = '''def run(df, top=1):
    if df.empty:
        return {"summary": "No rows.", "table": []}
    total = float(df["revenue"].sum())
    ranked = df.sort_values("revenue", ascending=False)
    share = float(ranked["revenue"].head(top).sum()) / total if total else None
    return {"summary": f"The top {top} region(s) earn {share:.1%} of revenue.",
            "table": [{"region": r, "revenue": float(v)} for r, v in zip(ranked["region"], ranked["revenue"])]}
'''
EXAMPLE_TESTS = json.dumps([{"name": "two regions", "columns": ["region", "revenue"], "rows": [["A", 75.0], ["B", 25.0]],
                             "params": {"top": 1}, "expect": {"summary": "The top 1 region(s) earn 75.0% of revenue.",
                                                              "table": [{"region": "A", "revenue": 75.0},
                                                                        {"region": "B", "revenue": 25.0}]}}], indent=1)

@st.dialog("Skill", width="large")
def skill_detail(name: str, ver: int) -> None:
    s = api.get(f"/v1/workspaces/{ws}/skills/{name}", version=ver)
    st.markdown(f"**{name}** · v{ver}")
    st.caption(f"{s.get('purpose') or ''} · {s['status']}")
    st.code(s["source"], language="python")
    st.code(s["permissions"]["input_sql"], language="sql")
    rep = s.get("test_report") or {}
    if rep.get("static"):
        st.error("Static check: " + "; ".join(rep["static"]))
    if rep.get("cases"):
        st.dataframe(pd.DataFrame([{k: c.get(k) for k in ("name", "kind", "passed", "error")} for c in rep["cases"]]),
                     hide_index=True, width="stretch")
    b = st.columns(3)
    if s["status"] in ("proposed", "failed", "tested") and b[0].button("Run tests", type="primary"):
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/skills/{name}/{ver}/test"))
        if r:
            st.toast(f"Tests: {r['status']}")
            st.rerun()
    if s["status"] == "tested" and b[1].button("Request registration"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/skills/{name}/{ver}/register"),
                    "Approval requested; an owner decides in Work")
    if s["status"] == "approved" and b[2].button("Retire", type="tertiary"):
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/skills/{name}/retire"), "Retired"):
            st.rerun()


@st.dialog("Propose a skill", width="large")
def propose_skill() -> None:
    st.caption("A skill is a Python function `run(df, **params) -> dict` over a governed query. It runs in a sandbox, "
               "must pass its own tests and Lyst's adversarial cases, and is registered only after an owner who did "
               "not propose it approves.")
    with st.form("skill", border=False):
        name = st.text_input("Name", placeholder="revenue_concentration")
        purpose = st.text_input("Purpose", placeholder="Share of revenue from the top regions")
        triggers = st.text_input("Use it when a request mentions (comma-separated)", placeholder="revenue concentration")
        input_sql = st.text_area("Input query (read-only)", height=80,
                                 value="SELECT region, sum(revenue) AS revenue FROM pnl GROUP BY region")
        source = st.text_area("Python", value=EXAMPLE_SOURCE, height=220)
        tests = st.text_area("Tests (JSON)", value=EXAMPLE_TESTS, height=160)
        if st.form_submit_button("Propose", type="primary"):
            try:
                body = {"name": name, "purpose": purpose, "source": source, "input_sql": input_sql,
                        "tests": json.loads(tests), "triggers": [t.strip() for t in triggers.split(",") if t.strip()]}
            except ValueError as e:
                st.error(f"The tests are not valid JSON: {e}")
                return
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/skills", body))
            if r and r["status"] == "rejected":
                st.error("Refused: " + "; ".join(r["problems"]))
            elif r:
                st.toast(f"Proposed {r['name']} v{r['version']}. Run its tests next.")
                st.rerun()


def skills() -> None:
    sk = api.get(f"/v1/workspaces/{ws}/skills")
    if not sk:
        kit.empty(":material/extension:", "No skills yet", "A skill adds a method Lyst does not have. When a request needs "
                  "one, Lyst can draft it itself; you can also propose one.")
    tone = {"approved": "ok", "tested": "wait", "proposed": "wait", "failed": "bad", "retired": "off"}
    for i, s in enumerate(sk):
        if kit.row(s["name"], f"v{s['version']} · {s.get('purpose') or ''}",
                   status=(s["status"].capitalize(), tone.get(s["status"], "info")), actions=[("Open", "open")],
                   key=f"sk{s['name']}{s['version']}", divider=i < len(sk) - 1):
            skill_detail(s["name"], int(s["version"]))
    if editor and st.button("Propose a skill", icon=":material/add:", type="tertiary"):
        propose_skill()
    with st.expander("Everything Lyst can do (capabilities)", icon=":material/widgets:"):
        st.dataframe(pd.DataFrame(api.get("/v1/capabilities")), hide_index=True, width="stretch",
                     column_config={"mutating": st.column_config.CheckboxColumn(),
                                    "requires_approval": st.column_config.CheckboxColumn()})
    with st.expander("Evaluation runs", icon=":material/rule:"):
        runs = api.get(f"/v1/workspaces/{ws}/evals")
        if runs:
            st.dataframe(pd.DataFrame(runs), hide_index=True, width="stretch")
        else:
            st.caption("None recorded here. `lyst eval` scores Lyst against independently computed answers.")


try:
    view = st.query_params.get("view", "workflows")
    if kit.header("Build", "Dashboards, saved analyses Lyst can rerun, and (for builders) new skills.",
                  action="New dashboard" if editor else None, icon=":material/dashboard_customize:"):
        new_dashboard()
    view = kit.sections(["Dashboards", "Workflows", "Skills"], key="view")
    {"Workflows": workflows, "Dashboards": dashboards, "Skills": skills}[view]()
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
