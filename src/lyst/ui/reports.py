# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Dashboards and saved analyses (verified workflows), shown inside Data. Imported by the Data page."""

import io

import pandas as pd
import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md


def times(n: object) -> str:
    """A count of runs in words: once, twice, 3 times."""
    k = int(n or 0)
    return {0: "never", 1: "once", 2: "twice"}.get(k, f"{k:,} times")


def _ctx():
    """Read per run, never at import: this module is imported once and shared by every session."""
    return client(), workspace(), st.session_state.get("role") in ("editor", "admin", "owner")

REPEAT = {86400: "Daily", 604800: "Weekly", 2592000: "Monthly"}
HEALTH = {"current": ("Up to date", "ok"), "stale": ("Out of date", "warn"), "invalidated": ("Needs rebuilding", "bad"),
          "broken": ("Broken", "bad"), "repairing": ("Updating", "run")}
KIND = {"report": "PDF briefing", "briefing": "Briefing", "presentation": "Slides", "workbook": "Excel workbook",
        "dashboard": "Dashboard", "workflow": "Saved analysis"}
VALIDATION = {"verified": "verified", "verified_with_warnings": "verified with warnings", "not_verified": "not verified",
              "failed": "failed verification", "unknown": "not checked"}


def assets_by_ref(ref_kind: str) -> dict[str, dict]:
    """The registry's view of each dashboard or workflow (health, version, repair), keyed by what it registers."""
    api, ws, _ = _ctx()
    try:
        return {a["ref_id"]: a for a in api.get(f"/v1/workspaces/{ws}/assets") if a.get("ref_kind") == ref_kind}
    except APIError:
        return {}


def health_chip(asset: dict | None) -> tuple[str, str] | None:
    """Only what needs attention gets a chip from the registry; an up-to-date asset keeps its own status."""
    if not asset or asset.get("health") in (None, "current"):
        return None
    return HEALTH.get(asset["health"], (asset["health"].capitalize(), "warn"))


def asset_panel(asset: dict | None, *, key: str) -> None:
    """Is it still right? Why not, what Lyst proposes, one button to bring it up to date, and its versions."""
    if not asset:
        return
    api, ws, editor = _ctx()
    a = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/assets/{asset['id']}")) or asset
    text, tone = HEALTH.get(a.get("health"), ("Not checked", "off"))
    c = st.columns([4, 1.4], vertical_alignment="center")
    c[0].markdown(md(f"**Version {a.get('version')}** · {VALIDATION.get(a.get('validation'), a.get('validation') or '')}"
                     + (f" · checked {kit.when(a.get('checked_at'))}" if a.get("checked_at") else "")))
    kit.chip(text, tone, c[1])
    if a.get("health") not in (None, "current"):
        if a.get("health_reason"):
            why = a["health_reason"].replace("_", " ")
            st.warning(md(why[:1].upper() + why[1:] + "."), icon=":material/sync_problem:")
        prop = a.get("proposal") or {}
        if prop.get("text"):
            st.caption(md(prop["text"]))
        if editor and prop.get("automatic") and a.get("health") != "repairing":
            label = {"refresh": "Refresh it", "rebuild": "Rebuild it", "recompile": "Rebuild it"}.get(prop.get("action"),
                                                                                                    "Bring it up to date")
            if st.button(label, type="primary", icon=":material/build:", key=f"repair_{key}"):
                r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/assets/{a['id']}/repair"))
                if r and r.get("task_id"):
                    st.toast("Lyst is re-running and re-checking it; the new version appears when it is verified.",
                             icon=":material/build:")
                elif r:
                    st.toast(f"Updated to version {r.get('version')}", icon=":material/check:")
    vers = a.get("versions") or []
    if len(vers) > 1 or a.get("depends"):
        with st.expander(f"Versions and what it depends on · {len(vers)}", icon=":material/history:"):
            dep = a.get("depends") or {}
            parts = [f"{n.replace('_', ' ')} v{v}" for n, v in (dep.get("metrics") or {}).items()] + \
                    [f"{n} data v{v}" for n, v in (dep.get("datasets") or {}).items()]
            if parts:
                st.caption("Built on " + ", ".join(parts))
            for v in vers:
                st.caption(md(f"v{v['version']} · {kit.when(v['created_at'])} · "
                              f"{VALIDATION.get(v.get('validation'), v.get('validation') or '')}"
                              + (f" · {v['note']}" if v.get("note") else "")))



def open_task(tid: str) -> None:
    api, ws, editor = _ctx()
    st.session_state.task_id = tid
    st.switch_page("app_pages/analyst.py", query_params={"task": tid})


# ------------------------------------------------------------------------------------ workflows

@st.dialog("Workflow", width="large")
def workflow_detail(w: dict, asset: dict | None = None) -> None:
    api, ws, editor = _ctx()
    d = w["definition"]
    grain = (d["inputs"].get("period") or {}).get("grain")
    st.markdown(f"**{w['title']}**")
    st.caption(f"{md(d['purpose'])} · v{w['version']} · verified {times(w['verified_runs'])}"
               + (f" · reused {times(w['runs'])}" if w.get("runs") else ""))
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
    asset_panel(asset, key=f"wf_{w['id']}")
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
    api, ws, editor = _ctx()
    flows = api.get(f"/v1/workspaces/{ws}/workflows")
    if not flows:
        kit.empty(":material/bolt:", "No workflows yet", "When an answer is verified, Lyst saves how it got there. Asking "
                  "again, for any period, reuses that checked plan instead of working it out from scratch.")
        return
    st.caption("A changed definition or data layout marks one out of date until it is verified again.")
    reg = assets_by_ref("workflow")
    for i, w in enumerate(flows):
        grain = (w["definition"]["inputs"].get("period") or {}).get("grain")
        own = ("Ready", "verified") if w["status"] == "active" else ("Out of date", "warn")
        used = kit.when(w.get("last_used_at"))
        if kit.row(w["title"], " · ".join(x for x in (
                       f"Verified {times(w['verified_runs'])}" if w.get("verified_runs") else "Not verified yet",
                       f"reused {times(w['runs'])}" if w.get("runs") else "not reused yet",
                       f"by {grain}" if grain else None, f"last used {used}" if used else None) if x),
                   status=health_chip(reg.get(w["id"])) or own,
                   actions=[("Open", "open")], key=f"w{w['id']}", divider=i < len(flows) - 1):
            workflow_detail(w, reg.get(w["id"]))


# ----------------------------------------------------------------------------------- dashboards

@st.dialog("New dashboard", width="large")
def new_dashboard() -> None:
    api, ws, editor = _ctx()
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
def dashboard_detail(d: dict, asset: dict | None = None) -> None:
    api, ws, editor = _ctx()
    st.markdown(f"**{d['title']}**")
    dash = bool(d.get("checks") and any(c["name"] == "dash_check" for c in d["checks"]))
    st.caption(f"{'Plotly Dash' if dash else 'Streamlit'} app · refreshed {kit.when(d['last_refresh_at'])} · "
               f"{d['refresh_count']} refreshes")
    asset_panel(asset, key=f"db_{d['id']}")
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
    api, ws, editor = _ctx()
    dbs = api.get(f"/v1/workspaces/{ws}/dashboards")
    if not dbs:
        if kit.empty(":material/dashboard:", "No dashboards yet", "Pick the metrics and how to split them; Lyst builds a "
                     "verified, self-refreshing dashboard you can run anywhere.",
                     action="New dashboard" if editor else None, key="db_empty"):
            new_dashboard()
        return
    reg = assets_by_ref("dashboard")
    for i, d in enumerate(dbs):
        tone = "ok" if d["status"] == "published" else ("off" if d["status"] == "retired" else "warn")
        if kit.row(d["title"], f"Refreshed {kit.when(d['last_refresh_at'])} · {d['refresh_count']} refreshes",
                   status=health_chip(reg.get(d["id"])) or (d["status"].capitalize(), tone), actions=[("Open", "open")],
                   key=f"db{d['id']}", divider=i < len(dbs) - 1):
            dashboard_detail(d, reg.get(d["id"]))


FILE_KINDS = ("report", "briefing", "presentation", "workbook")
EXT = {"report": "pdf", "briefing": "md", "presentation": "pptx", "workbook": "xlsx"}


@st.dialog("Report", width="large")
def report_detail(a: dict) -> None:
    api, ws, editor = _ctx()
    st.markdown(md(f"**{a['title']}** · {KIND.get(a['kind'], a['kind'])}"))
    if a.get("question"):
        st.caption(md(f"From the answer to: {a['question']}"))
    asset_panel(a, key=f"rp_{a['id']}")
    c = st.columns([1.6, 1.6, 3])

    def latest() -> bytes:
        return api.http.get(f"/v1/artifacts/{a['ref_id']}", headers=api._h()).content

    c[0].download_button("Download", latest, file_name=f"{a['title'][:60]}.{EXT.get(a['kind'], 'bin')}",
                         on_click="ignore", icon=":material/download:", key=f"rpdl_{a['id']}")
    if a.get("task_id") and c[1].button("Open the answer", icon=":material/arrow_outward:", type="tertiary",
                                        key=f"rpopen_{a['id']}"):
        open_task(a["task_id"])
    if editor and c[2].button("Retire", type="tertiary", icon=":material/archive:", key=f"rpret_{a['id']}"):
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/assets/{a['id']}/retire"), "Retired"):
            st.rerun()


def saved_reports() -> None:
    """Files made from answers (PDF briefings, slides, workbooks): each knows the data and definitions it was built
    on, so Lyst can say when one no longer matches them and rebuild it."""
    api, ws, editor = _ctx()
    try:
        files = [a for a in api.get(f"/v1/workspaces/{ws}/assets") if a["kind"] in FILE_KINDS]
    except APIError:
        return
    kit.label(f"Reports from answers · {len(files)}" if files else "Reports from answers")
    if not files:
        st.caption("Download a PDF briefing, slides or a workbook from any answer and it is kept here, with a note "
                   "when the numbers behind it change.")
        return
    top = st.columns([5, 1.4])
    if editor and top[1].button("Check all", type="tertiary", icon=":material/rule:", key="assets_scan", width="stretch"):
        changed = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/assets/scan")) or []
        st.toast(f"{len(changed)} changed" if changed else "Everything is still up to date", icon=":material/rule:")
    for i, a in enumerate(files):
        if kit.row(a["title"], f"{KIND.get(a['kind'], a['kind'])} · v{a['version']} · updated {kit.when(a['updated_at'])}",
                   status=HEALTH.get(a.get("health"), ("Not checked", "off")), actions=[("Open", "open")],
                   key=f"rp{a['id']}", divider=i < len(files) - 1):
            report_detail(a)
