# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data: what Lyst reads and what it makes from it. Datasets and their sources, dashboards, saved analyses, SQL."""

import json
import re

import pandas as pd
import streamlit as st

from lyst.ui import (
    anticipate,
    drafts,
    governance,
    integrations,
    kit,
    onboarding,
    reports,
    scenarios,
    transforms,
    visuals,
    workbench,
)
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

api, ws = client(), workspace()
editor = st.session_state.get("role") in ("editor", "admin", "owner")
EVERY = {None: "Manually", 3600: "Every hour", 86400: "Every day", 604800: "Every week"}

# The page's sections, each with its icon, in the order people move through them: what Lyst reads, where it comes
# from, what it makes, and the workbench. Names are kept in the URL (?view=) so links from elsewhere land here.
TABS = {"Datasets": ":material/table_chart:", "Sources": ":material/cable:",
        "Dashboards": ":material/space_dashboard:", "Saved analyses": ":material/bookmark:", "SQL": ":material/terminal:"}

# The Data workspace's own look, after AcuArch's data surface: a wide canvas, a header with the sources' status as a
# pill, icon tabs with Ask docked beside them, a library rail beside a dotted canvas, and "at a glance" pills under
# each view's intro. Everything is written in design tokens (--lyst-*), so both themes follow.
_CSS = """<style>
.block-container:has(.lyst-data) {max-width: var(--lyst-canvas-max);}
/* header: the sources' status as a pill with a coloured dot, beside the primary action */
div[class*="st-key-data_status_"] button {border-radius: var(--lyst-radius-pill); border: 1px solid transparent;
  background: transparent; min-height: 2rem; padding: 0 .7rem; color: var(--lyst-text-muted);
  font-size: var(--lyst-size-sm); white-space: nowrap;}
div[class*="st-key-data_status_"] button:hover {border-color: var(--lyst-border); background: var(--lyst-raised); color: var(--lyst-text);}
div.st-key-data_review button {border-radius: var(--lyst-radius-pill); min-height: 2rem; font-size: var(--lyst-size-sm);
  color: var(--lyst-warn); border-color: color-mix(in srgb, var(--lyst-warn) 45%, transparent) !important;}
div[class*="st-key-data_status_"] [data-testid="stIconMaterial"] {font-size: .7rem;}
div.st-key-data_status_ok button {color: var(--lyst-text-muted);}
div.st-key-data_status_ok [data-testid="stIconMaterial"] {color: var(--lyst-good);}
div.st-key-data_status_warn button {border-color: color-mix(in srgb, var(--lyst-warn) 45%, transparent); color: var(--lyst-warn);}
div.st-key-data_status_warn [data-testid="stIconMaterial"] {color: var(--lyst-warn);}
div.st-key-data_status_bad button {border-color: color-mix(in srgb, var(--lyst-bad) 40%, transparent); color: var(--lyst-bad);}
div.st-key-data_status_bad [data-testid="stIconMaterial"] {color: var(--lyst-bad);}
div.st-key-data_add button {border-radius: var(--lyst-radius-pill);}
/* tabs: pills with icons; the open one raised, its icon in the accent */
div.st-key-sec_data_view [role="radiogroup"] {background: transparent; padding: 0; gap: .35rem; flex-wrap: wrap;}
div.st-key-sec_data_view button[data-variant="segmented_control"] {border-radius: var(--lyst-radius-pill) !important;
  padding: 0 .95rem; min-height: 2.25rem; color: var(--lyst-text-muted);}
div.st-key-sec_data_view button[data-variant="segmented_control"]:hover {background: var(--lyst-raised);
  color: var(--lyst-text);}
div.st-key-sec_data_view button[aria-checked="true"] {box-shadow: var(--lyst-shadow-2); color: var(--lyst-text) !important;
  font-weight: var(--lyst-weight-medium);}
div.st-key-sec_data_view button[aria-checked="true"] [data-testid="stIconMaterial"] {color: var(--lyst-accent);}
/* Ask, docked at the end of the tab row */
div.st-key-data_ask_dock button {border-radius: var(--lyst-radius-pill); white-space: nowrap;}
/* the library rail: a floating panel of everything on the page, searchable, in quiet sections */
div.st-key-lyst_card_data_library, div.st-key-lyst_card_sql_library {background: var(--lyst-material);
  backdrop-filter: saturate(160%) blur(10px); padding: .8rem .7rem .6rem .7rem;}
div.st-key-lyst_card_data_library:hover, div.st-key-lyst_card_sql_library:hover {transform: none;}
.lyst-rail-head {display: flex; align-items: center; gap: .45rem; font-weight: var(--lyst-weight-medium);
  font-size: var(--lyst-size-sm); padding: 0 .3rem .1rem .3rem;}
.lyst-rail-head .dot {width: .55rem; height: .55rem; border-radius: 3px; background: var(--lyst-accent);}
.lyst-rail-label {display: flex; justify-content: space-between; text-transform: uppercase;
  letter-spacing: var(--lyst-tracking-label); font-size: var(--lyst-size-xs); font-weight: 600;
  color: var(--lyst-text-muted); margin: .65rem .35rem .1rem .35rem;}
.lyst-rail-label span:last-child {font-weight: 400; font-variant-numeric: tabular-nums;}
.lyst-rail-none {font-size: var(--lyst-size-xs); color: var(--lyst-text-muted); margin: .1rem .4rem .2rem .4rem;}
div[class*="st-key-lib_"] button, div[class*="st-key-sqlq_"] button, div.st-key-tf_open button {justify-content: flex-start; text-align: left;
  min-height: 0; padding: .32rem .5rem; border-radius: var(--lyst-radius-md); width: 100%;
  font-size: var(--lyst-size-sm);}
div[class*="st-key-lib_"] button > div, div[class*="st-key-sqlq_"] button > div, div.st-key-tf_open button > div {justify-content: flex-start; width: 100%;}
div[class*="st-key-lib_"] button p, div[class*="st-key-sqlq_"] button p {overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;}
div[class*="st-key-lib_"] button:hover, div[class*="st-key-sqlq_"] button:hover {background: var(--lyst-raised);
  box-shadow: var(--lyst-shadow-1);}
div[class*="st-key-lib_"] [data-testid="stIconMaterial"], div[class*="st-key-sqlq_"] [data-testid="stIconMaterial"] {
  color: var(--lyst-text-muted);}
div[class*="st-key-lib_warn_"] [data-testid="stIconMaterial"] {color: var(--lyst-warn);}
div.st-key-tf_open button {color: var(--lyst-link); font-size: var(--lyst-size-xs);}
div[class*="st-key-lib_bad_"] [data-testid="stIconMaterial"] {color: var(--lyst-bad);}
div.st-key-lyst_card_data_library {gap: .1rem;}
/* the canvas: a dotted ground the cards, map and lists sit on */
div[class*="st-key-data_canvas"] {background-color: var(--lyst-surface); border-radius: var(--lyst-radius-xl);
  background-image: radial-gradient(color-mix(in srgb, var(--lyst-text-muted) 22%, transparent) 1px, transparent 1px);
  background-size: 18px 18px; padding: 1rem; border: 1px solid var(--lyst-edge);}
/* a view's intro: what it is, its one action, and the numbers at a glance */
.lyst-intro-title {font-size: var(--lyst-size-lg); font-weight: var(--lyst-weight-strong); line-height: 1.25;}
.lyst-intro-body {color: var(--lyst-text-muted); font-size: var(--lyst-size-sm); margin-top: .2rem; max-width: 46rem;}
dl.lyst-glance {display: flex; flex-wrap: wrap; gap: .4rem; margin: .7rem 0 .2rem 0; padding: 0;}
dl.lyst-glance > div {display: flex; align-items: baseline; gap: .4rem; border: 1px solid var(--lyst-border);
  border-radius: var(--lyst-radius-pill); padding: .2rem .75rem; font-size: var(--lyst-size-xs); background: var(--lyst-raised);}
dl.lyst-glance dt {color: var(--lyst-text-muted); margin: 0;}
dl.lyst-glance dd {margin: 0; font-weight: var(--lyst-weight-strong); font-variant-numeric: tabular-nums;
  font-size: var(--lyst-size-sm);}
dl.lyst-glance > div.warn {border-color: color-mix(in srgb, var(--lyst-warn) 45%, transparent);}
dl.lyst-glance > div.warn dd {color: var(--lyst-warn);}
dl.lyst-glance > div.bad {border-color: color-mix(in srgb, var(--lyst-bad) 40%, transparent);}
dl.lyst-glance > div.bad dd {color: var(--lyst-bad);}
/* Ask's starting questions: quiet one-line choices */
div[class*="st-key-data_ask_s"] button {justify-content: flex-start; text-align: left; min-height: 0; padding: .3rem .4rem;
  border-radius: var(--lyst-radius-md); font-size: var(--lyst-size-sm);}
div[class*="st-key-data_ask_s"] button:hover {background: var(--lyst-surface);}
/* on a phone the work comes first: the rails follow the canvas and the editor */
@media (max-width: 640px) {
  div[data-testid="stColumn"]:has(div.st-key-lyst_card_data_library),
  div[data-testid="stColumn"]:has(div.st-key-lyst_card_sql_library) {order: 2;}
}
/* nothing here yet: a dashed panel, as on the canvas */
div[class*="st-key-data_empty"] {border: 1px dashed var(--lyst-border); border-radius: var(--lyst-radius-xl);
  background: var(--lyst-raised); padding: .5rem 1rem 1.2rem 1rem;}
</style>"""


def glance(items: list[tuple[str, object, str | None]]) -> str:
    """'At a glance' pills: (term, value, tone) where tone is None, 'warn' or 'bad'."""
    return '<dl class="lyst-glance">' + "".join(
        f'<div class="{tone or ""}"><dt>{kit._esc(term)}</dt><dd>{kit._esc(str(value))}</dd></div>'
        for term, value, tone in items) + "</dl>"


def intro(key: str, title: str, body: str, *, action: str | None = None, icon: str | None = None,
          pills: list[tuple[str, object, str | None]] | None = None) -> bool:
    """A view's opening card: its name, one line on what it is, its one action, and the numbers at a glance.
    Returns True when the action was pressed."""
    with kit.card(f"intro_{key}"):
        cols = st.columns([5, 1.5] if action else [1], vertical_alignment="top")
        cols[0].html(f'<div class="lyst-intro-title">{kit._esc(title)}</div>'
                     f'<div class="lyst-intro-body">{kit._esc(body)}</div>' + (glance(pills) if pills else ""))
        return bool(action) and cols[1].button(action, icon=icon, key=f"intro_{key}_act", width="stretch")


def empty_panel(key: str, icon: str, title: str, body: str, *, action: str | None = None) -> bool:
    with st.container(key=f"data_empty_{key}"):
        return kit.empty(icon, title, body, action=action, key=f"empty_{key}")


def dataset_name(filename: str) -> str:
    base = re.sub(r"\.(csv|tsv|parquet|txt)$", "", filename.lower())
    return (re.sub(r"[^a-z0-9]+", "_", base).strip("_") or "dataset")[:40]


def ask(question: str) -> None:
    out = kit.guarded(lambda: api.ask(ws, question))
    if out:
        st.session_state.task_id = out["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})


@st.cache_data(ttl=3600)
def _formats() -> dict:
    try:
        return api.get("/v1/ingest/formats")
    except APIError:
        return {"extensions": ["csv", "tsv", "parquet"], "groups": {}, "refused": {}}


def upload() -> None:
    """Any file: a workbook, a document, a CSV. Lyst reads it first and shows what it found before anything loads."""
    fm = _formats()
    groups = fm.get("groups") or {}
    st.caption("Spreadsheets (" + ", ".join(groups.get("spreadsheet", [])) + "), tables ("
               + ", ".join(groups.get("table", [])) + ") and documents (" + ", ".join(groups.get("document", []))
               + "). Lyst shows what it found before anything is loaded.")
    f = st.file_uploader("File", type=fm.get("extensions"), label_visibility="collapsed", key="ingest_file")
    jid = st.session_state.get("ingest_job")
    if f is not None:
        seen = st.session_state.get("ingest_seen")
        if seen != (f.name, f.size):  # upload once per file, not on every rerun
            job = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/ingest", None, files={"file": (f.name, f.getvalue())}))
            st.session_state["ingest_seen"] = (f.name, f.size)
            jid = job["id"] if job else None
            st.session_state["ingest_job"] = jid
    if jid:
        review(jid)


def review(jid: str) -> None:
    job = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/ingest/{jid}"))
    if not job:
        st.session_state.pop("ingest_job", None)
        return
    if job.get("status") != "staged":
        st.caption(f"{job['filename']} was already {job['status']}.")
        st.session_state.pop("ingest_job", None)
        return
    st.markdown(md(f"**{job['filename']}** · {job.get('kind', 'file')}"
                   + (f" · {len(job.get('sheets') or [])} sheet{'s' * (len(job.get('sheets') or []) != 1)}"
                      if job.get("sheets") else "")))
    if job.get("duplicate_of"):
        st.info(md(f"This is the same file as an earlier upload ({job['duplicate_of']})."), icon=":material/content_copy:")
    for w in job.get("warnings") or []:
        st.warning(md(w), icon=":material/report:")
    choices = []
    tables = job.get("tables") or []
    if tables:
        kit.label(f"Tables found · {len(tables)}")
    for t in tables:
        with st.container(border=True):
            c = st.columns([0.5, 3, 2], vertical_alignment="center")
            inc = c[0].checkbox("Include", value=t.get("include", True), key=f"inc_{jid}_{t['key']}",
                                label_visibility="collapsed")
            c[1].markdown(md(f"**{t.get('title') or t.get('sheet') or t['key']}**  \n{t['rows']:,} rows · {len(t['columns'])} columns"
                             + (f" · {t['sheet']} {t['range']}" if t.get("sheet") else "")))
            name = c[2].text_input("Dataset name", value=t.get("proposed_name") or dataset_name(t["key"]),
                                   key=f"nm_{jid}_{t['key']}", disabled=not inc)
            for tot in t.get("totals") or []:
                ok = all(ch["matches"] for ch in tot["checks"])
                (st.caption if ok else st.warning)(md(
                    f"“{tot['label']}” row: " + ("matches the sum of the rows, so it is left out of the data" if ok else
                    "does not match: " + "; ".join(f"{ch['column']} says {ch['stated']:,g} but the rows add up to "
                                                   f"{ch['sum_of_rows']:,g}" for ch in tot["checks"] if not ch["matches"]))))
            for w in (t.get("warnings") or []) + (t.get("notes") or []):
                st.caption(":material/info: " + md(w))
            derived = [col["header"] for col in t["columns"] if col.get("formula") or col.get("derived")]
            if derived:
                st.caption("Calculated columns: " + md(", ".join(map(str, derived))))
            with st.expander("Preview", icon=":material/table:"):
                st.dataframe(pd.DataFrame(t.get("preview") or [], columns=[col["header"] for col in t["columns"]]),
                             hide_index=True, width="stretch")
            choices.append({"key": t["key"], "name": name, "include": inc})
    doc = job.get("document")
    searchable = False
    if doc:
        kit.label("Text")
        st.caption(md(f"{doc.get('words', 0):,} words" + (f" · {doc['pages']} pages" if doc.get("pages") else "")
                      + (" · sections: " + ", ".join(doc.get("headings")[:6]) if doc.get("headings") else "")))
        searchable = st.checkbox("Make the text searchable, so answers can cite it", value=doc.get("include", True),
                                 key=f"doc_{jid}")
        with st.expander("Excerpt", icon=":material/article:"):
            st.markdown(md(doc.get("excerpt") or ""))
    b = st.columns([1.6, 1.2, 4])
    if b[0].button("Load it", type="primary", icon=":material/upload:", key=f"commit_{jid}",
                   disabled=not (any(ch["include"] for ch in choices) or searchable)):
        out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/ingest/{jid}/commit",
                                           {"tables": choices, "document": searchable if doc else None}))
        if out:
            made = [f"{d['dataset']} ({d['rows']:,} rows)" for d in out.get("datasets") or []]
            st.toast("Loaded " + (", ".join(made) if made else "")
                     + (" and the text" if out.get("document") else ""), icon=":material/check:")
            for e in out.get("errors") or []:
                st.error(md(e if isinstance(e, str) else e.get("message") or str(e)))
            st.session_state.pop("ingest_job", None)
            st.rerun()
    if b[1].button("Discard", type="tertiary", key=f"discard_{jid}"):
        kit.guarded(lambda: api.delete(f"/v1/workspaces/{ws}/ingest/{jid}"), "Discarded")
        st.session_state.pop("ingest_job", None)
        st.rerun()


@st.dialog("Add data", width="large")
def add_data() -> None:
    st.session_state.setdefault("add_how_pick", "Upload a file")
    how = st.segmented_control("How", ["Upload a file", "Connect a source", "From an API description"],
                               key="add_how_pick", label_visibility="collapsed") or "Upload a file"
    if how == "From an API description":
        drafts.from_description()
        return
    if how == "Upload a file":
        upload()
        return
    kinds = api.get("/v1/connectors")
    kind = st.selectbox("Source", [k["kind"] for k in kinds],
                        format_func=lambda k: next(x["label"] for x in kinds if x["kind"] == k))
    man = next(k for k in kinds if k["kind"] == kind)
    st.caption(man["notes"])
    auth = {"none": "no sign-in", "password": "a user name and password", "token": "an access token",
            "api_key": "an API key"}.get(man.get("auth"), None)
    st.caption(" · ".join(x for x in ("Read-only: Lyst never writes to it" if man.get("read_only") else None,
                                      f"Needs {auth}" if auth else None,
                                      f"Retries up to {man['retry_attempts']} times on a passing failure"
                                      if man.get("retry_attempts") else None) if x))
    with st.form("connection", border=False):
        name = st.text_input("Name", placeholder="Finance warehouse")
        values = {}
        for fld in man["fields"]:
            values[fld["name"]] = st.text_input(fld["label"] + ("" if fld["required"] else " (optional)"),
                                                value=fld["default"] or "", type="password" if fld["secret"] else "default")
        if st.form_submit_button("Test and save", type="primary") and name:
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connections", {"kind": kind, "name": name, "values": values}))
            if r:
                st.toast(f"Connected {r['name']}: {r['test']}", icon=":material/check:")
                st.rerun()


SEVERITY = {"high": ("Fix first", "bad"), "medium": ("Worth a look", "warn"), "low": ("Minor", "off")}
OUTCOME = {"pass": ("Contract kept", "ok"), "warn": ("Contract: warnings", "warn"), "fail": ("Contract broken", "bad"),
           "breaking": ("Contract broken", "bad")}


def health(d: dict) -> tuple[str, str] | None:
    """One chip a business reader understands: can I rely on this table?"""
    risks = d.get("risks") or {}
    if d.get("authority") == "authoritative" and not risks.get("high"):
        return ("Source of truth", "verified")
    outcome = (d.get("contract") or {}).get("outcome")
    if outcome in ("fail", "breaking"):
        return OUTCOME[outcome]
    if risks.get("high"):
        return (f"{risks['high']} to fix", "bad")
    if d.get("trusted"):
        return ("Trusted", "verified")
    if risks.get("medium"):
        return (f"{risks['medium']} to check", "warn")
    return None


@st.dialog("Dataset", width="large")
def dataset_detail(d: dict) -> None:
    from lyst.ui import actions  # noqa: PLC0415

    name = d["name"]
    from lyst.ui import activity  # noqa: PLC0415

    top = st.columns([4, 1.6, 1.2], vertical_alignment="center")
    top[0].markdown(f"**{d['title']}**")
    activity.presence("dataset", name, top[1])
    with top[2]:  # what Lyst can do with this dataset: ask, run, approve, go to (already open here, so not Open)
        actions.menu("dataset", name, page="data", label="More", exclude=("dataset.open",))
    fresh = d.get("freshness") or {}
    st.caption(" · ".join(x for x in (d.get("description"), f"{d.get('rows', d.get('row_count', 0)):,} rows",
                                      f"data through {fresh['latest']}" if fresh.get("latest") else None,
                                      f"updated {kit.when(d.get('refreshed_at'))}",
                                      str(d.get("classification", "")).capitalize(),
                                      f"called {name} in SQL") if x))
    view = st.segmented_control("Show", ["Overview", "Preview", "Columns", "Contract"], default="Overview",
                                label_visibility="collapsed", key=f"dsv_{name}") or "Overview"
    if view == "Overview":
        ins = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/insights"))
        if not ins:
            return
        anticipate.about("dataset", name, key=f"hf_d{name}")
        activity.panel("dataset", name, key=f"dsact_{name}", admin=st.session_state.get("role") in ("admin", "owner"))
        governance.classification(name, d.get("classification"))
        integrations.authority(name, integrations.source_authority().get(name))
        risks = ins.get("risks") or []
        if not risks:
            st.markdown(md(ins.get("summary") or ""))
        kit.label(f"Before you rely on it · {len(risks)}" if risks else "Before you rely on it")
        if not risks:
            st.caption(":material/check_circle: No quality risks found: no duplicate rows at its grain, no gaps in time, "
                       "no unusual values.")
        for k, r in enumerate(risks):
            text, tone = SEVERITY.get(r["severity"], (r["severity"], "info"))
            kit.row(r["text"], ", ".join(c.replace("_", " ") for c in r.get("columns", [])), status=(text, tone),
                    key=f"risk_{name}_{k}", divider=k < len(risks) - 1)
        used = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/impact")) or []
        kit.label("What depends on it")
        st.caption(", ".join(f"{u['title']} ({u['kind']})" for u in used) if used else "Nothing approved reads it yet.")
        rel = ins.get("relationships") or []
        if rel:
            with st.expander(f"How it connects to other data · {len(rel)}", icon=":material/hub:"):
                st.markdown("\n".join(f"- {md(x['text'])}" for x in rel))
        if st.button("Ask Lyst to check its quality", icon=":material/fact_check:", key=f"askq_{name}"):
            ask(f"Profile {name} for data quality issues")
    elif view == "Preview":
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/query", {"sql": f'SELECT * FROM "{name}" LIMIT 50',
                                                                        "max_rows": 50}))
        if r:
            st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, width="stretch", height=320)
    elif view == "Columns":
        ins = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/insights"))
        if ins:
            column_profiles(name, ins)
            cols = st.multiselect("If these columns changed, what would be affected?", [c["name"] for c in ins["columns"]],
                                  key=f"imp_{name}")
            if cols:
                hits = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/impact", columns=",".join(cols))) or []
                st.markdown("\n".join(f"- **{md(h['title'])}** ({h['kind']}): {md(h['why'])}" for h in hits)
                            if hits else "Nothing approved reads those columns.")
    else:
        contract(name)
    if view == "Overview":
        transforms.panel(d.get("source"))


def _fmt(v: object) -> str:
    if isinstance(v, int | float):
        a = abs(v)
        return (f"{v / 1e9:.2f}B" if a >= 1e9 else f"{v / 1e6:.2f}M" if a >= 1e6 else f"{v / 1e3:.1f}K" if a >= 1e4
                else f"{v:,.4g}")
    return str(v)


def column_profiles(name: str, ins: dict) -> None:
    """Every column as a small profile: what it is, how complete, how varied, and its values at a glance (the top
    categories for a category, the range and middle for a number, the span for a date). From the stored profile."""
    prof = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/profile")) or {}
    by = {c["name"]: c for c in prof.get("columns") or []}
    cols = ins.get("columns") or []
    for start in range(0, len(cols), 2):
        row = st.columns(2)
        for slot, c in zip(row, cols[start:start + 2], strict=False):
            p = by.get(c["name"], {})
            stats = p.get("stats") or {}
            kind = (c.get("semantic_type") or c.get("role") or "").replace("_", " ")
            missing = float(c.get("nulls") or 0)
            body = (f'<div class="lyst-card-kicker">{kit._esc(kind)} · {kit._esc(str(c.get("type", "")))}</div>'
                    f'<div class="lyst-card-title">{kit._esc(c["name"])}</div>'
                    + visuals.meter(1 - missing, label=f"{c['name']} complete", tone="good" if missing == 0 else "warn",
                                    text=f"{1 - missing:.0%} complete · {c.get('distinct', p.get('unique', 0)):,} distinct"))
            if c.get("top"):
                body += visuals.bars([(str(k), float(v)) for k, v in c["top"][:5]],
                                     label=f"Most common {c['name']} values")
            elif "min" in stats and isinstance(stats.get("min"), int | float):
                body += visuals.stats([("lowest", _fmt(stats["min"]), None), ("median", _fmt(stats.get("median")), None),
                                       ("highest", _fmt(stats["max"]), None)], compact=True)
            elif stats.get("min"):
                body += visuals.stats([("from", str(stats["min"]), None), ("to", str(stats.get("max", "")), None)],
                                      compact=True)
            if c.get("issues"):
                body += f'<div class="lyst-card-meta" style="color:var(--lyst-warn)">{kit._esc("; ".join(c["issues"]))}</div>'
            with slot, kit.card(f"col_{name}_{c['name']}"):
                st.html(body)


def contract(name: str) -> None:
    """A data contract: what this table promises (columns, types, allowed values, key). Every refresh is checked
    against it, and a break reaches the owner's inbox with what it affects."""
    admin = st.session_state.get("role") in ("admin", "owner")
    c = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/contract"))
    if c is None:
        return
    st.caption("A contract is what this table promises: its columns, their types and allowed values, and what "
               "identifies a row. Lyst checks every refresh against it and tells the owner when one breaks it.")
    if not c.get("active"):
        if not admin:
            st.info("No contract yet. An admin can adopt one Lyst drafts from the data.", icon=":material/handshake:")
            return
        draft = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/contract/draft"))
        if not draft:
            return
        st.markdown(f"Lyst drafted one from the data: **{len(draft['columns'])} columns**"
                    + (f", one row per {' × '.join(k.replace('_', ' ') for k in draft['key'])}" if draft.get("key") else "")
                    + (f", {len(draft.get('references') or [])} link(s) to other tables" if draft.get("references") else "") + ".")
        st.dataframe(pd.DataFrame([{"Column": x["name"], "Type": x["type"], "Required": x["required"],
                                    "Allowed values": ", ".join(map(str, x["accepted_values"] or [])),
                                    "Minimum": x["min"]} for x in draft["columns"]]), hide_index=True, width="stretch")
        with st.expander("Edit before adopting (JSON)", icon=":material/code:"):
            raw = st.text_area("Contract", json.dumps(draft, indent=1, default=str), height=240, key=f"cdraft_{name}",
                               label_visibility="collapsed")
        if st.button("Adopt this contract", type="primary", icon=":material/handshake:", key=f"cadopt_{name}"):
            try:
                spec = json.loads(raw)
            except ValueError as e:
                st.error(f"The contract is not valid JSON: {e}")
                return
            out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/datasets/{name}/contract", {"spec": spec}))
            if out:
                text, _ = OUTCOME.get((out.get("check") or {}).get("outcome"), ("Checked", "info"))
                st.toast(f"Contract v{out['version']} adopted · {text}", icon=":material/handshake:")
        return
    status = c.get("status") or {}
    text, tone = OUTCOME.get(status.get("outcome"), ("Not checked yet", "off"))
    top = st.columns([3, 1.4], vertical_alignment="center")
    top[0].markdown(f"**Contract v{status.get('version') or c['active'].get('version', '')}**"
                    + (f" · last checked {kit.when(status.get('checked_at'))}" if status.get("checked_at") else ""))
    kit.chip(text, tone, top[1])
    b = st.columns([1.4, 1.4, 4])
    if b[0].button("Check now", icon=":material/rule:", key=f"ccheck_{name}"):
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/datasets/{name}/contract/check"))
        if r:
            st.toast(OUTCOME.get(r["outcome"], (r["outcome"], ""))[0], icon=":material/rule:")
    if admin and b[1].button("Retire", type="tertiary", icon=":material/archive:", key=f"cretire_{name}"):
        kit.guarded(lambda: api.delete(f"/v1/workspaces/{ws}/datasets/{name}/contract"), "Contract retired")
    checks = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/contract/checks")) or []
    if checks:
        last = checks[0]
        problems = [r for r in last.get("results", []) if r.get("severity") in ("breaking", "fail", "warn")]
        if problems:
            kit.label("What the last check found")
            st.markdown("\n".join(f"- {md(r.get('text') or r.get('detail') or r)}" for r in problems))
            if last.get("impacted"):
                st.caption("Affects " + ", ".join(i.get("title") or i.get("name") for i in last["impacted"]))
        with st.expander(f"Check history · {len(checks)}", icon=":material/history:"):
            for k in checks[:10]:
                st.caption(f"{kit.when(k['checked_at'])} · contract v{k['contract_version']} on data v{k['dataset_version']} · "
                           f"{OUTCOME.get(k['outcome'], (k['outcome'], ''))[0]}")


@st.dialog("Sync a table", width="large")
def sync(conn: dict) -> None:
    try:
        tables = api.get(f"/v1/workspaces/{ws}/connections/{conn['id']}/tables")
    except APIError as e:
        st.error(str(e))
        return
    if not tables:
        st.caption("This source has no tables Lyst can read.")
        return
    tref = st.selectbox("Table", [t["ref"] for t in tables])
    tsel = next(t for t in tables if t["ref"] == tref)
    st.caption("Columns: " + ", ".join(c["name"] for c in tsel["columns"]))
    with st.form("sync", border=False):
        c = st.columns(3)
        dataset = c[0].text_input("Into dataset", value=dataset_name(tref.split(".")[-1]))
        cursor = c[1].selectbox("Only new rows by", [None] + [col["name"] for col in tsel["columns"]],
                                format_func=lambda x: "Reload everything" if x is None else x)
        every = c[2].selectbox("Then", list(EVERY), format_func=EVERY.get)
        if st.form_submit_button("Sync now", type="primary"):
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connections/{conn['id']}/sync", {
                "source_table": tref, "dataset": dataset, "cursor_column": cursor, "every_seconds": every}))
            if r:
                st.toast(f"{r['dataset']}: {r['rows_added']:,} rows added, {r['rows_total']:,} in total"
                         + ("; the schedule awaits approval" if r.get("schedule") else ""), icon=":material/check:")
                st.rerun()


def enrich(catalog: list[dict]) -> list[dict]:
    """Each dataset with its catalog entry (freshness, contract, risks, authority), read once per run."""
    try:
        info = {c["name"]: c for c in api.get(f"/v1/workspaces/{ws}/catalog")}
    except APIError:
        info = {}
    return [{**d, **info.get(d["name"], {})} for d in catalog]


def datasets(catalog: list[dict], every: list[dict]) -> None:
    """The data workspace: a library rail of everything here (datasets, approved metrics, dashboards) beside a canvas
    that shows the datasets as cards, as a map of how they connect, or as a plain list."""
    if not catalog:
        if empty_panel("datasets", ":material/database:", "No data yet", "Upload a spreadsheet, a CSV or a document, "
                       "or connect a database, and Lyst can answer questions about it.",
                       action="Add data" if editor else None):
            add_data()
        return
    by = _metrics_by_dataset()
    rail, canvas = st.columns([1, 3.2], gap="medium")
    with rail:
        q, opened = library(every, by)
    shown = [d for d in every if not q or q.lower() in (d["title"] + d["name"] + (d.get("description") or "")).lower()]
    asked = st.query_params.get("dataset")
    with canvas:
        with st.container(key="data_canvas_ds"):
            bar = st.columns([4, 1.4], vertical_alignment="center")
            layout = bar[1].segmented_control("Layout", ["Cards", "Map", "List"], default="Cards", key="ds_layout",
                                              label_visibility="collapsed") or "Cards"
            bar[0].caption(LAYOUTS[layout])
            if not shown:
                st.caption(f"Nothing matches “{q}”.")
            elif layout == "Map":
                data_map(shown)
            elif layout == "List":
                hit = data_list(shown, by)
                opened = opened or hit
            else:
                for start in range(0, len(shown), 2):
                    cols = st.columns(2)
                    for col, c in zip(cols, shown[start:start + 2], strict=False):
                        with col, kit.card(f"ds_{c['name']}"):
                            if dataset_card(c, by.get(c["name"]) or []):
                                opened = c["name"]
    if asked and not opened and any(d["name"] == asked for d in every):  # a link from search, a pin or a source
        opened = asked
    if asked:
        st.query_params.pop("dataset", None)
    if opened:
        dataset_detail(next(d for d in every if d["name"] == opened))


LAYOUTS = {"Cards": "Each dataset: its shape, how fresh it is, and what it feeds.",
           "Map": "How the datasets connect, through the columns they share.",
           "List": "Every dataset in one list, and every relationship between them."}


def library(every: list[dict], by: dict[str, list[dict]]) -> tuple[str, str | None]:
    """The rail: find anything on the page, then each dataset, approved metric and dashboard one click away. Returns
    (the search text, the dataset opened from it if any)."""
    from lyst.ui import artifacts  # noqa: PLC0415

    opened = None
    with kit.card("data_library"):
        st.html('<div class="lyst-rail-head"><span class="dot"></span>Library</div>')
        q = st.text_input("Find", placeholder="Find anything", label_visibility="collapsed",
                          key="ds_find", icon=":material/search:")

        def hit(*t: str | None) -> bool:
            return not q or q.lower() in " ".join(x or "" for x in t).lower()

        sets = [d for d in every if hit(d["title"], d["name"], d.get("description"))]
        _rail_label("Datasets", len(sets))
        for d in sets:
            h = health(d)
            tone = h[1] if h and h[1] in ("warn", "bad") else "ok"
            rows = int(d.get("rows") or d.get("row_count") or 0)
            if st.button(d["title"], key=f"lib_{tone}_ds_{d['name']}", type="tertiary",
                         icon=":material/error:" if tone != "ok" else ":material/table:",
                         help=" · ".join(x for x in (d["name"], f"{rows:,} rows", h[0] if h else None) if x)):
                opened = d["name"]
        if editor and st.button("Build a dataset from data Lyst has", type="tertiary", icon=":material/add:",
                                key="tf_open", help="Join, filter or reshape datasets Lyst already has into a new one, "
                                "with checks that it adds up"):
            transforms.build()
        metrics = [m for ms in by.values() for m in ms if hit(m.get("title"), m.get("name"), m.get("dataset"))]
        _rail_label("Approved metrics", len(metrics))
        if not metrics:
            st.html('<div class="lyst-rail-none">None yet. Define one in Brain.</div>')
        for m in metrics:
            if st.button(m.get("title") or m["name"], key=f"lib_ok_m_{m['name']}", type="tertiary",
                         icon=":material/speed:", help=f"From {m.get('dataset')}. Opens in Brain."):
                st.switch_page("app_pages/brain.py", query_params={"metric": m["name"]})
        boards = [a for a in artifacts.catalog(limit=200) if a["kind"] == "dashboard" and hit(a["title"])]
        _rail_label("Dashboards", len(boards))
        if not boards:
            st.html('<div class="lyst-rail-none">None yet. Make one under Dashboards.</div>')
        for a in boards:
            if st.button(a["title"], key=f"lib_ok_db_{a['id']}".replace(":", "_"), type="tertiary",
                         icon=":material/space_dashboard:", help="Opens in the viewer"):
                artifacts.open_artifact(a)
    return q, opened


def _rail_label(text: str, n: int) -> None:
    st.html(f'<div class="lyst-rail-label"><span>{kit._esc(text)}</span><span>{n}</span></div>')


def data_list(full: list[dict], by: dict[str, list[dict]]) -> str | None:
    """The datasets as a plain list, and every relationship between them, for keyboards, screen readers and a quick
    scan. Returns the dataset opened, if any."""
    pairs = _pairs(full)
    links = {n: sum(1 for (a, b) in pairs if n in (a, b)) for n in (c["name"] for c in full)}
    opened = None
    with kit.card("ds_list"):
        for i, c in enumerate(full):
            fresh = (c.get("freshness") or {}).get("latest")
            feeds = len(by.get(c["name"]) or [])
            meta = " · ".join(x for x in (
                c["name"], f"{int(c.get('rows') or c.get('row_count') or 0):,} rows", f"{len(c.get('schema') or [])} columns",
                f"{links[c['name']]} relationship{'s' * (links[c['name']] != 1)}",
                f"feeds {feeds} metric{'s' * (feeds != 1)}" if feeds else None,
                f"data through {fresh}" if fresh else None) if x)
            if kit.row(c["title"], meta, status=health(c), actions=[("Open", "open")], key=f"dsl_{c['name']}",
                       divider=i < len(full) - 1) == "open":
                opened = c["name"]
    kit.label(f"Relationships · {len(pairs)}")
    if not pairs:
        st.caption("No relationships were found between these datasets.")
    else:
        titles = {c["name"]: c["title"] for c in full}
        st.dataframe(pd.DataFrame([{"Dataset": titles[a], "Connects to": titles[b],
                                    "Shared columns": ", ".join(sorted(cols)).replace("_", " ")}
                                   for (a, b), cols in sorted(pairs.items())]), hide_index=True, width="stretch")
    return opened


ROLE_TONE = {"time": ("Time", "info"), "dimension": ("Categories", "neutral"), "measure": ("Numbers", "accent")}


def _roles(schema: list[dict]) -> dict[str, int]:
    """Columns by what they are for: time, categories (to split by), numbers (to add up)."""
    out = {"time": 0, "dimension": 0, "measure": 0}
    for col in schema or []:
        t = str(col.get("type", "")).lower()
        out["time" if any(k in t for k in ("date", "time")) else
            "measure" if any(k in t for k in ("int", "double", "float", "decimal", "numeric", "real")) else
            "dimension"] += 1
    return out


def _metrics_by_dataset() -> dict[str, list[dict]]:
    try:
        metrics = api.get(f"/v1/workspaces/{ws}/metrics")
    except APIError:
        return {}
    out: dict[str, list[dict]] = {}
    for m in metrics:
        out.setdefault(m["dataset"], []).append(m)
    return out


def _pairs(full: list[dict]) -> dict[tuple[str, str], set[str]]:
    """Pairs of datasets that share a column with matching values (from discovery's evidence), with those columns."""
    try:
        rels = api.get(f"/v1/workspaces/{ws}/relations")
    except APIError:
        rels = []
    names = {c["name"] for c in full}
    pairs: dict[tuple[str, str], set[str]] = {}
    for r in rels:
        if r.get("from_kind") == "column" and r.get("to_kind") == "column":
            fd, fc = str(r["from_name"]).split(".", 1)
            td, _ = str(r["to_name"]).split(".", 1)
            if fd in names and td in names and fd != td:
                pairs.setdefault(tuple(sorted((fd, td))), set()).add(fc)
    return pairs


def data_map(full: list[dict]) -> None:
    """How the datasets connect: each one on a ring, linked by the columns they share."""
    nodes = [(c["name"], c["title"], f"{int(c.get('rows') or c.get('row_count') or 0):,} rows") for c in full]
    edges = [(a, b, ", ".join(sorted(cols)[:3]).replace("_", " ")) for (a, b), cols in sorted(_pairs(full).items())]
    with kit.card("data_map"):
        st.html(visuals.ring_graph(nodes, edges, label="Datasets and the columns they share"))
        st.caption("Lines join datasets that share a column with matching values, so Lyst can compare or combine them. "
                   "List shows every relationship; open a dataset to see its evidence.")


def read(full: list[dict], src: dict) -> tuple[str, str, list[tuple[dict, tuple[str, str]]]]:
    """Lyst's one-line answer about the data: (tone, sentence, datasets that need attention)."""
    if not full:
        return "off", "No data yet. Add a file or connect a source to begin.", []
    latest = max(((c.get("freshness") or {}).get("latest") or "" for c in full), default="")
    flagged = [(c, h) for c in full if (h := health(c)) and h[1] in ("bad", "warn")]
    rows = sum(int(c.get("rows") or c.get("row_count") or 0) for c in full)
    facts = " · ".join(x for x in (f"{len(full)} dataset{'s' * (len(full) != 1)}", f"{rows:,} rows",
                                   f"data through {latest}" if latest else None) if x)
    if flagged:
        names = ", ".join(c["title"] for c, _ in flagged[:2]) + (" and more" if len(flagged) > 2 else "")
        return "wait", f"{len(flagged)} of {len(full)} need{'s' * (len(flagged) == 1)} attention ({names}) · {facts}", flagged
    return "ok", f"Ready to answer from · {facts}", []


def attention(flagged: list[tuple[dict, tuple[str, str]]]) -> None:
    """What needs attention, folded behind one button; each dataset one click from its full record."""
    with st.popover("Review", icon=":material/rule:", key="data_review", width="stretch"):
        for i, (c, h) in enumerate(flagged[:6]):
            risks = c.get("risks") or {}
            why = f"{risks.get('high', 0)} high, {risks.get('medium', 0)} medium findings" if risks else ""
            if kit.row(c["title"], why, status=h, actions=[("Open", "open")], key=f"read_{c['name']}",
                       divider=i < min(len(flagged), 6) - 1) == "open":
                st.query_params["dataset"] = c["name"]
                st.query_params["view"] = "datasets"
                st.session_state.pop("sec_data_view", None)
                st.rerun()


def dataset_card(c: dict, feeds: list[dict]) -> bool:
    """One dataset as a quiet card: what it is, whether to rely on it, its shape in one line and one bar. The full
    record (profile, contract, lineage, activity) opens in the dataset viewer."""
    fresh = c.get("freshness") or {}
    schema = c.get("schema") or []
    roles = _roles(schema)
    top = st.columns([4, 1.6], vertical_alignment="top")
    top[0].html(f'<div class="lyst-card-kicker">{kit._esc(str(c.get("classification", "")).capitalize())} · '
                f'v{c.get("version", 1)}</div>'
                f'<div class="lyst-card-title">{kit._esc(c["title"])}</div>')
    if h := health(c):
        kit.chip(h[0], h[1], top[1])
    grain = " × ".join(g.replace("_", " ") for g in c.get("grain") or [])
    shape = " · ".join(x for x in (f"{int(c.get('rows') or c.get('row_count') or 0):,} rows", f"{len(schema)} columns",
                                   f"one row per {grain}" if grain else None,
                                   f"through {fresh['latest']}" if fresh.get("latest") else None) if x)
    st.html(f'<div class="lyst-card-meta lyst-clamp" style="margin:.15rem 0 .35rem 0">'
            f'{kit._esc(c.get("description") or "")}</div>'
            f'<div class="lyst-card-meta lyst-num" style="margin-bottom:.45rem">{kit._esc(shape)}</div>'
            + visuals.stacked([(f"{ROLE_TONE[k][0]} {v}", v, ROLE_TONE[k][1]) for k, v in roles.items()],
                              label=f"{c['title']} columns by role"))
    if feeds:
        st.caption(f"Feeds {', '.join(m['title'] for m in feeds[:3])}" + (f" and {len(feeds) - 3} more" if len(feeds) > 3
                                                                           else ""))
    b = st.columns([1, 1, 2.5])
    opened = b[0].button("Open", key=f"d{c['name']}_open", type="tertiary", icon=":material/open_in_full:")
    if b[1].button("Ask", key=f"d{c['name']}_ask", type="tertiary", icon=":material/chat:",
                   help=f"Ask Lyst what is in {c['title']}"):
        ask(f"Summarise {c['name']}: what does it contain and what stands out?")
    return opened


STATE = {"healthy": ("Healthy", "ok"), "degraded": ("Degraded", "warn"), "failing": ("Failing", "bad"),
         "needs_action": ("Needs action", "bad"), "untested": ("Not tested", "off")}
CATEGORY = {"auth": "sign-in", "permission": "permissions", "rate_limit": "rate limit", "timeout": "time-out",
            "network": "network", "source": "the source itself", "not_found": "a missing table or path",
            "schema": "a changed layout", "config": "settings", "unknown": "an unknown cause"}
ACTION = {"test": "Test", "discover": "List tables", "read": "Read", "load": "Load", "schema_change": "Layout changed"}


@st.dialog("Source", width="large")
def source_detail(cid: str) -> None:
    """Why a source is healthy or not, what to do about it, what it feeds, and what happened recently."""
    h = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/connections/{cid}/health"))
    if not h:
        return
    text, tone = STATE.get(h["state"], (h["state"], "info"))
    top = st.columns([4, 1.4], vertical_alignment="center")
    top[0].markdown(f"**{md(h['name'])}** · {h['kind']}")
    kit.chip(text, tone, top[1])
    if h.get("required_action"):
        st.error(md(h["required_action"]), icon=":material/build:")
    anticipate.about("connection", cid, key=f"hf_c{cid}")
    for r in h.get("reasons") or []:
        st.caption(md(r))
    day = h.get("last_24h") or {}
    st.caption(" · ".join(x for x in (
        f"last worked {kit.when(h.get('last_success_at'))}" if h.get("last_success_at") else "has not worked yet",
        f"last failed {kit.when(h.get('last_failure_at'))}" if h.get("last_failure_at") else None,
        f"{h['consecutive_failures']} failures in a row" if h.get("consecutive_failures") else None,
        f"cause: {CATEGORY.get(h['failure_category'], h['failure_category'])}" if h.get("failure_category") else None,
        f"24 h: {day.get('operations', 0)} operations, {day.get('errors', 0)} errors, {day.get('retries', 0)} retries"
        + (f", {day['rate_limited']} rate limited" if day.get("rate_limited") else "")) if x))
    tables = h.get("tables") or []
    kit.label(f"What it feeds · {len(tables)}" if tables else "What it feeds")
    if not tables:
        st.caption("Nothing synced from it yet. Use Sync to load a table as a dataset.")
    for k, t in enumerate(tables):
        sched = t.get("schedule") or {}
        fresh = (t.get("data_freshness") or {}).get("latest")
        con = t.get("contract") or {}
        status = (("Overdue", "warn") if t.get("overdue") else
                  ("Contract broken", "bad") if con.get("outcome") in ("fail", "breaking") else None)
        if kit.row(t["dataset"], " · ".join(x for x in (
                f"from {t['source_table']}", "new rows only" if t.get("mode") == "incremental" else "full reload",
                f"{t.get('rows') or 0:,} rows", f"data through {fresh}" if fresh else None,
                f"synced {kit.when(t.get('last_sync_at'))}" if t.get("last_sync_at") else "never synced",
                (f"repeats {EVERY.get(sched.get('every_seconds'), 'on a schedule').lower()}"
                 + ("" if sched.get("enabled") else " (awaiting approval)")) if sched else None) if x),
                status=status, actions=[("Open", "open")], key=f"st_{cid}_{k}", divider=k < len(tables) - 1):
            st.query_params["dataset"] = t["dataset"]
            st.rerun()
    cred = h.get("credential")
    if cred:
        st.caption(f"Credential v{cred.get('version')} · {cred.get('status')}"
                   + (f" · last used {kit.when(cred.get('last_used_at'))}" if cred.get("last_used_at") else "")
                   + ". Replace or revoke it in Settings › Connections.")
    with st.expander("Recent activity", icon=":material/history:"):
        ev = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/connections/{cid}/events", limit=50)) or []
        if not ev:
            st.caption("Nothing recorded yet.")
        else:
            st.dataframe(pd.DataFrame([{
                "When": kit.when(e["occurred_at"]), "What": ACTION.get(e["action"], e["action"]),
                "Table": e.get("source_table") or "", "Outcome": "OK" if e["outcome"] == "ok" else "Error",
                "Cause": CATEGORY.get(e.get("category"), e.get("category") or ""), "Message": e.get("message") or "",
                "Attempts": e.get("attempts"), "Time": f"{e['ms']} ms" if e.get("ms") is not None else ""} for e in ev]),
                hide_index=True, width="stretch", height=min(38 + 35 * len(ev), 360))
    b = st.columns([1.2, 1.2, 4])
    if b[0].button("Test", icon=":material/network_check:", key=f"src_test_{cid}"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connections/{cid}/test"), "Connection works")
    if editor and b[1].button("Sync", icon=":material/sync:", key=f"src_sync_{cid}"):
        st.session_state["_sync_after"] = cid
        st.rerun()


def sources_state() -> dict:
    """Everything the header and Sources need, read once per run: sources, their health, and uploads to review."""
    out: dict = {"conns": [], "health": {}, "staged": []}
    try:
        out["conns"] = api.get(f"/v1/workspaces/{ws}/connections")
        if out["conns"]:
            out["health"] = {h["id"]: h for h in api.get(f"/v1/workspaces/{ws}/connections/health")}
    except APIError:
        pass
    try:
        out["staged"] = [j for j in api.get(f"/v1/workspaces/{ws}/ingest") if j.get("status") == "staged"]
    except APIError:
        pass
    return out


def _state(c: dict, h: dict) -> tuple[str, str]:
    return STATE.get(h.get("state")) or (("Connected", "ok") if c["status"] == "ok" else (c["status"].capitalize(), "warn"))


def status_pill(src: dict) -> tuple[str, str]:
    """The header's one line about where the data comes from: (text, tone), worst news first."""
    tones = [_state(c, src["health"].get(c["id"]) or {})[1] for c in src["conns"]]
    n, bad, warn = len(tones), tones.count("bad"), tones.count("warn")
    staged = len(src["staged"])
    if bad:
        return f"{bad} source{'s' * (bad != 1)} need{'s' * (bad == 1)} action", "bad"
    if staged:
        return f"{staged} upload{'s' * (staged != 1)} to review", "warn"
    if warn:
        return f"{warn} source{'s' * (warn != 1)} to check", "warn"
    if n:
        return f"{n} source{'s' * (n != 1)} · all working", "ok"
    return "Uploaded files only", "off"


def waiting_review(staged: list[dict]) -> None:
    """Uploads Lyst has read but nobody has loaded or discarded yet."""
    if not staged:
        return
    kit.label(f"Waiting for review · {len(staged)}")
    with kit.card("src_staged"):
        for i, j in enumerate(staged):
            hit = kit.row(j["filename"], " · ".join(x for x in (j.get("kind"), f"uploaded {kit.when(j.get('created_at'))}")
                                                    if x), actions=[("Review", "rev")] if editor else [],
                          key=f"stg{j['id']}", divider=i < len(staged) - 1)
            if hit == "rev":
                st.session_state["ingest_job"] = j["id"]
                st.session_state["add_how_pick"] = "Upload a file"
                add_data()


def sources_view(src: dict) -> None:
    """Where the data comes from: every connected source with its health, proposed connections, and uploads that
    wait for review."""
    conns, health_ = src["conns"], src["health"]
    tones = [_state(c, health_.get(c["id"]) or {})[1] for c in conns]
    if intro("sources", "Sources", "Databases, services and files Lyst reads from. It reads with least privilege, "
             "never writes back, and loads each table as a versioned dataset.",
             action="Connect a source" if editor else None, icon=":material/add_link:",
             pills=[("Sources", len(conns), None), ("Working", tones.count("ok") + tones.count("verified"), None),
                    ("Need action", tones.count("bad") + tones.count("warn"),
                     ("bad" if "bad" in tones else "warn") if ("bad" in tones or "warn" in tones) else None),
                    ("Waiting for review", len(src["staged"]), "warn" if src["staged"] else None)]):
        st.session_state["add_how_pick"] = "Connect a source"
        add_data()
    waiting_review(src["staged"])
    drafts.listed()
    connections(conns, health_)


def connections(conns: list[dict], health_: dict[str, dict]) -> None:
    kit.label(f"Connected · {len(conns)}" if conns else "Connected")
    if not conns:
        empty_panel("sources", ":material/cable:", "No source connected", "Connect PostgreSQL, SQLite, a REST API or a "
                    "file at a URL. Uploaded files work without one.")
        return
    if (cid := st.session_state.pop("_sync_after", None)) and (c := next((x for x in conns if x["id"] == cid), None)):
        sync(c)
    ahead = anticipate.health_by_subject()
    with kit.card("src_list"):
        for i, c in enumerate(conns):
            h = health_.get(c["id"]) or {}
            state = _state(c, h)
            seen = ahead.get(("connection", c["id"]))
            if seen and state[1] == "ok":
                state = anticipate.VERDICT.get(seen.get("verdict"), state)
            if st.query_params.get("connection") == c["id"]:
                st.query_params.pop("connection", None)
                source_detail(c["id"])
            n = len(h.get("tables") or [])
            meta = h.get("required_action") or " · ".join(x for x in (
                c["kind"], f"feeds {n} dataset{'s' * (n != 1)}" if n else None,
                f"last worked {kit.when(h['last_success_at'])}" if h.get("last_success_at") else
                f"tested {kit.when(c.get('last_tested_at')) or 'never'}") if x)
            hit = kit.row(c["name"], meta, status=state, actions=[("Open", "open"), ("Sync", "sync")], key=f"c{c['id']}",
                          divider=i < len(conns) - 1)
            if hit == "open":
                source_detail(c["id"])
            elif hit == "sync":
                sync(c)


def dashboards_view() -> None:
    """The studio's gallery: every dashboard and report as a preview to open in its own viewer; the maintenance lists
    (health, rebuild, retire) one step away."""
    from lyst.ui import artifacts  # noqa: PLC0415

    kinds = ("dashboard", "report", "briefing", "presentation", "workbook")
    items = [i for i in artifacts.catalog(limit=200) if i["kind"] in kinds]
    attention = [i for i in items if (i.get("status") or ("", ""))[1] in ("warn", "bad")]
    if intro("dashboards", "Dashboards and reports", "Lyst builds dashboards from approved metrics and checks every "
             "version before it is shared. Each one opens in its own viewer, to refine in words, pin or share.",
             action="New dashboard" if editor else None, icon=":material/dashboard_customize:",
             pills=[("Dashboards", sum(1 for i in items if i["kind"] == "dashboard"), None),
                    ("Reports and briefings", sum(1 for i in items if i["kind"] != "dashboard"), None),
                    ("Need attention", len(attention), "warn" if attention else None)]):
        reports.new_dashboard()
    if items:
        artifacts.gallery(key="studio", kinds=kinds)
    else:
        empty_panel("dashboards", ":material/space_dashboard:", "No dashboards yet", "Describe one in words, or ask "
                     "a question and keep the answer as a report. Each opens in its own viewer.")
    with st.expander("Manage dashboards and reports", icon=":material/build:"):
        reports.dashboards()
        reports.saved_reports()


def saved_view() -> None:
    """Analyses kept to run again, and what-if scenarios to explore."""
    intro("saved", "Saved analyses", "Analyses Lyst can run again on fresh data, and scenarios to try a change "
          "before you make it.")
    reports.workflows()
    scenarios.saved_list()


def _set_sql(text: str) -> None:
    st.session_state["sql_text"] = text


def _result_chart(cols: list[str], rows: list[list]) -> dict | None:
    """A first chart for a result: a date or category column against the first number, when there is one."""
    if not rows or len(cols) < 2:
        return None
    df = pd.DataFrame(rows, columns=cols)
    num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    other = [c for c in cols if c not in num]
    if not num or not other:
        return None
    x, y = other[0], num[0]
    if df[x].nunique() > 60:
        return None
    dated = pd.to_datetime(df[x].astype(str), errors="coerce", format="mixed").notna().all()
    agg = df.groupby(x, sort=dated)[y].sum().reset_index()
    if not dated:
        agg = agg.sort_values(y, ascending=False).head(20)
    return {"type": "line" if dated else "bar", "title": f"{y} by {x}".replace("_", " "),
            "x": [str(v)[:10] if dated else str(v) for v in agg[x]], "y": [float(v) for v in agg[y]]}


def schema_navigator(catalog: list[dict]) -> None:
    """Every dataset with its columns and types; one click writes a starting query."""
    _rail_label("Datasets", len(catalog))
    for d in catalog:
        with st.expander(d["title"], icon=":material/table:"):
            st.caption(f"`{d['name']}` · {d.get('row_count', 0):,} rows")
            st.html('<div class="lyst-card-meta">' + "<br>".join(
                f'{kit._esc(c["name"])} <span style="opacity:.7">{kit._esc(str(c.get("type", "")))}</span>'
                for c in d.get("schema") or []) + "</div>")
            st.button("Query it", key=f"sqlq_{d['name']}", type="tertiary", icon=":material/edit_note:",
                      on_click=_set_sql, args=(f'SELECT *\nFROM "{d["name"]}"\nLIMIT 50',))


def sql_editor(catalog: list[dict]) -> None:
    """Read-only SQL with the schema beside it, results that survive a rerun, a first chart, and recent queries."""
    if "sql_text" not in st.session_state:
        st.session_state["sql_text"] = f'SELECT *\nFROM "{catalog[0]["name"]}"\nLIMIT 50' if catalog else ""
    side, main = st.columns([1, 3.2], gap="medium")
    with side, kit.card("sql_library"):
        st.html('<div class="lyst-rail-head"><span class="dot"></span>Schema</div>')
        schema_navigator(catalog)
        mine = []
        try:
            seen: set[str] = set()
            for h in api.get(f"/v1/workspaces/{ws}/queries", limit=200):
                key_ = " ".join(str(h.get("sql", "")).split())
                if not h.get("task_id") and key_ and key_ not in seen:
                    seen.add(key_)
                    mine.append(h)
            mine = mine[:6]
        except APIError:
            pass
        if mine:
            _rail_label("Recent", len(mine))
            for i, h in enumerate(mine):
                sql_ = " ".join(str(h.get("sql", "")).split())
                st.button(sql_[:60] + ("…" if len(sql_) > 60 else ""), key=f"sqlr_{i}", type="tertiary",
                          help=f"{kit.when(h['occurred_at'])} · {h.get('rows') or 0:,} rows · {h['outcome']}",
                          on_click=_set_sql, args=(h.get("sql", ""),))
    with main:
        with kit.card("sql_editor"):
            top = st.columns([5, 1.1], vertical_alignment="center")
            top[0].caption("Read-only SQL against dataset names. Column and row policies apply, and every query is "
                           "recorded.")
            run = top[1].button("Run", type="primary", icon=":material/play_arrow:", key="sql_run", width="stretch")
            text = st.text_area("SQL", height=170, label_visibility="collapsed", key="sql_text")
        if run:
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/query", {"sql": text, "max_rows": 5000}))
            st.session_state["sql_result"] = (ws, text, r) if r else None
        held = st.session_state.get("sql_result")
        if held and held[0] == ws and held[2]:
            _, ran, r = held
            n = len(r["rows"])
            st.html(glance([("Rows", f"{n:,}", None), ("Time", f"{r['ms']} ms", None)]
                           + ([("Edited since it ran", "yes", "warn")] if ran != text else [])))
            chart = _result_chart(r["columns"], r["rows"])
            tabs = st.tabs(["Table", "Chart"] if chart else ["Table"])
            with tabs[0]:
                st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, width="stretch",
                             placeholder="—", height="auto" if n <= 12 else 420)
            if chart:
                with tabs[1]:
                    from lyst.ui.components import chart as draw  # noqa: PLC0415

                    draw(chart)


# The workbench's tabs: SQL today; Python joins it here when the engine offers a sandbox.
WORKBENCH = {"SQL": sql_editor} | ({"Python": workbench.editor} if workbench.available().get("available") else {})


def sql(catalog: list[dict]) -> None:
    if len(WORKBENCH) == 1:
        sql_editor(catalog)
        return
    for tab, fn in zip(st.tabs(list(WORKBENCH)), WORKBENCH.values(), strict=True):
        with tab:
            fn(catalog)


def tabs() -> str:
    """The sections as icon pills, kept in the URL (?view=) like kit.sections, so links and reloads land on them."""
    names = list(TABS)
    if st.query_params.get("connection"):  # a link to one source opens it where sources live
        st.query_params["view"] = "sources"
    asked = (st.query_params.get("view") or "").lower()
    default = next((n for n in names if n.lower() == asked), names[0])
    pick = st.segmented_control("view", names, default=default, key="sec_data_view", label_visibility="collapsed",
                                format_func=lambda n: f"{TABS[n]} {n}") or default
    if st.query_params.get("view") != pick.lower():
        st.query_params["view"] = pick.lower()
    return pick


def header(src: dict, every: list[dict]) -> None:
    """One calm starting point: the title and the one action, then a single line on whether the data can be relied
    on. Sources and anything that needs attention sit behind small buttons on that line, not on the page."""
    cols = st.columns([5, 1.4], vertical_alignment="bottom")
    cols[0].html('<div class="lyst-data"></div><h1 class="lyst-title">Data</h1>')
    if editor and cols[1].button("Add data", type="primary", icon=":material/add:", key="data_add", width="stretch"):
        st.session_state["add_how_pick"] = "Upload a file"
        add_data()
    tone, sentence, flagged = read(every, src)
    line = st.columns([5, 1, 1.6] if flagged else [5.9, 1.6], vertical_alignment="center")
    line[0].html(f'<div class="lyst-pulse" role="status"><span class="lyst-pulse-dot {tone}" aria-hidden="true"></span>'
                 f'<span>{kit._esc(sentence)}</span></div>')
    if flagged:
        with line[1]:
            attention(flagged)
    text, stone = status_pill(src)
    if line[-1].button(text, key=f"data_status_{stone}", icon=":material/fiber_manual_record:", width="stretch",
                       type="tertiary", help="Where the data comes from: open Sources"):
        st.query_params["view"] = "sources"
        st.session_state.pop("sec_data_view", None)
        st.rerun()


ASK_STARTERS = ("What changed in the latest data?", "Which dataset should I trust for revenue?",
                "Are there gaps or duplicates I should know about?")


def ask_dock() -> None:
    """Ask, docked beside the tabs: a question about the data goes straight to Lyst and opens on Home."""
    with st.popover("Ask about the data", icon=":material/forum:", key="data_ask_dock", width="stretch"):
        q = st.text_area("Question", placeholder="Which region drove the change in margin?", key="data_ask_q",
                         height=90)
        if st.button("Ask", type="primary", icon=":material/arrow_upward:", key="data_ask_go", disabled=not q.strip()):
            ask(q.strip())
        st.caption("Or start from one of these")
        for i, s_ in enumerate(ASK_STARTERS):
            if st.button(s_, key=f"data_ask_s{i}", type="tertiary"):
                ask(s_)


try:
    st.html(_CSS)
    catalog = api.get(f"/v1/workspaces/{ws}/datasets")
    every = enrich(catalog)
    src = sources_state()
    header(src, every)
    row = st.columns([5.4, 1.6], vertical_alignment="center")
    with row[0]:
        view = tabs()
    with row[1]:
        ask_dock()
    {"Datasets": lambda: (datasets(catalog, every), onboarding.data_suggestions()),
     "Sources": lambda: sources_view(src),
     "Dashboards": dashboards_view,
     "Saved analyses": saved_view,
     "SQL": lambda: sql(catalog)}[view]()
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
