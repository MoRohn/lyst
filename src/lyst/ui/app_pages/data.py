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
    quality,
    reports,
    scenarios,
    transforms,
    visuals,
    workbench,
    zone,
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
/* ---------------------------------------------------------------- Data, as objects
   A title, one search and one way in; Lyst's one line; the sources, datasets and metrics as a flow; whatever is
   clicked opens as a sheet over the page (below), never under the flow. */
.block-container:has(.lyst-data) {padding-top: 3.2rem;}
h1.lyst-data-title {font-size: 2.6rem; letter-spacing: -.035em; font-weight: 640;}
div.st-key-data_tools {gap: .5rem;}
div.st-key-data_tools [data-testid="stTextInputRootElement"] {border-radius: var(--lyst-radius-pill) !important;
  background: var(--lyst-raised); box-shadow: var(--lyst-shadow-1); border-color: var(--lyst-edge) !important;}
div.st-key-data_tools button {border-radius: var(--lyst-radius-pill) !important; min-height: 2.5rem;}
div.st-key-data_more button {background: transparent !important; box-shadow: none !important; border-color: transparent !important;}
p.lyst-data-read {color: var(--lyst-text-muted); margin: .1rem 0 1.4rem 0; font-size: .95rem;}
div[class*="st-key-read_"] {margin: -.2rem 0 1.1rem -.5rem;}
div[class*="st-key-read_"] button {color: var(--lyst-text); font-size: .95rem; border-radius: var(--lyst-radius-pill);
  padding: .3rem .7rem; min-height: 0;}
div[class*="st-key-read_"] button:hover {background: var(--lyst-raised); box-shadow: var(--lyst-shadow-1);}
div[class*="st-key-read_"] [data-testid="stIconMaterial"] {color: var(--lyst-warn); font-size: .8rem;}
div.st-key-data_back button {color: var(--lyst-text-muted); padding-left: 0;}
@media (max-width: 640px) {
  .block-container:has(.lyst-data) {padding-top: 4rem;}
  h1.lyst-data-title {font-size: 2rem;}
}
/* ---------------------------------------------------------------- sheets over the flow
   A dataset, a source or a metric opens over the page: its face, what it is in a sentence, its state as chips, its
   numbers as tiles, then its moves, then its depth. */
[data-testid="stDialog"]:has(.lyst-sh) [role="dialog"] > div:first-child {font-size: 1.45rem; font-weight: 650;
  letter-spacing: -.02em;}
.lyst-sh {display: flex; gap: 1.1rem; align-items: flex-start;}
.lyst-sh-face {flex: none; width: 4.4rem; height: 4.4rem; border-radius: 20px; display: grid; place-items: center;
  background: var(--lyst-surface); border: 1px solid var(--lyst-edge); box-shadow: var(--lyst-shadow-1);}
.lyst-sh-shape {display: flex; align-items: flex-end; gap: 3px; height: 2.1rem; max-width: 3.3rem; overflow: hidden;}
.lyst-sh-shape i {flex: none; width: 5px; border-radius: 2px;}
.lyst-sh-shape .k-n {background: var(--lyst-accent);}
.lyst-sh-shape .k-t {background: color-mix(in srgb, var(--lyst-text-muted) 45%, transparent);}
.lyst-sh-shape .k-d {background: color-mix(in srgb, var(--lyst-info) 70%, transparent);}
.lyst-sh-shape .k-b, .lyst-sh-shape .k-o {background: color-mix(in srgb, var(--lyst-text-muted) 28%, transparent);}
.lyst-sh-icon {font-family: "Material Symbols Rounded"; font-size: 2rem; line-height: 1; color: var(--lyst-accent); font-variation-settings: "FILL" 0, "wght" 400;}
.lyst-sh-head {min-width: 0; flex: 1;}
.lyst-sh-kicker {font-size: var(--lyst-size-xs); color: var(--lyst-text-muted); text-transform: uppercase;
  letter-spacing: var(--lyst-tracking-label); font-weight: 600; padding-top: .25rem;}
.lyst-sh-kicker code {text-transform: none; letter-spacing: 0; font-size: .74rem; padding: .05rem .35rem;}
p.lyst-sh-read {font-size: 1rem; line-height: 1.55; margin: .35rem 0 0 0; max-width: 46rem;}
.lyst-sh-chips {display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .6rem;}
.lyst-sh-chip {display: inline-flex; align-items: center; gap: .4rem; border-radius: var(--lyst-radius-pill);
  padding: .18rem .7rem; font-size: var(--lyst-size-xs); font-weight: var(--lyst-weight-medium);
  background: var(--lyst-surface); border: 1px solid var(--lyst-edge); color: var(--lyst-text-muted);}
.lyst-sh-chip i {width: .45rem; height: .45rem; border-radius: 50%; background: currentColor; opacity: .9;}
.lyst-sh-chip.ok, .lyst-sh-chip.verified {color: var(--lyst-good);}
.lyst-sh-chip.warn {color: var(--lyst-warn); border-color: color-mix(in srgb, var(--lyst-warn) 40%, transparent);}
.lyst-sh-chip.bad {color: var(--lyst-bad); border-color: color-mix(in srgb, var(--lyst-bad) 40%, transparent);}
.lyst-sh-chip.off i {display: none;}
.lyst-sh-stats {display: grid; grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr)); gap: .6rem; margin: 1.1rem 0 .2rem 0;}
.lyst-sh-stat {background: var(--lyst-surface); border: 1px solid var(--lyst-edge); border-radius: 16px;
  padding: .75rem .95rem .7rem; min-width: 0;}
.lyst-sh-stat b {display: block; font-size: 1.3rem; font-weight: 650; letter-spacing: -.015em; line-height: 1.2;
  font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.lyst-sh-stat span {font-size: var(--lyst-size-xs); color: var(--lyst-text-muted);}
.lyst-sh-label {font-size: var(--lyst-size-xs); color: var(--lyst-text-muted); text-transform: uppercase;
  letter-spacing: var(--lyst-tracking-label); font-weight: 600; margin-right: .2rem;}
hr.lyst-sh-rule {border: 0; border-top: 1px solid var(--lyst-border); margin: .2rem 0 .1rem 0;}
.lyst-sh-trend {display: flex; flex-direction: column; gap: .3rem;}
.lyst-sh-trend svg {width: 100%; height: auto;}
div.st-key-dsh_moves {flex-wrap: wrap;}
div.st-key-dsh_moves button {border-radius: var(--lyst-radius-pill) !important; min-height: 2.4rem; padding: 0 1rem;}
div.st-key-dsh_feeds {flex-wrap: wrap; gap: .4rem;}
div[class*="st-key-dsf_"] button {border-radius: var(--lyst-radius-pill) !important; min-height: 2rem; padding: 0 .8rem;
  font-size: var(--lyst-size-sm); background: var(--lyst-surface) !important; box-shadow: none !important;}
div[class*="st-key-dsf_"] [data-testid="stIconMaterial"] {color: var(--lyst-accent);}
div[class*="st-key-dsv_"] [role="radiogroup"] {gap: .3rem; flex-wrap: wrap;}
div[class*="st-key-dsv_"] button[data-variant="segmented_control"] {border-radius: var(--lyst-radius-pill) !important;
  padding: 0 .95rem; min-height: 2.2rem;}
@media (max-width: 640px) {
  .lyst-sh {gap: .8rem;}
  .lyst-sh-face {width: 3.4rem; height: 3.4rem; border-radius: 16px;}
  .lyst-sh-shape {height: 1.7rem; max-width: 2.6rem;}
  .lyst-sh-stats {grid-template-columns: repeat(2, minmax(0, 1fr));}
  .lyst-sh-stat b {font-size: 1.1rem;}
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
    close_sheet()  # leaving the page: the sheet is not waiting here on the way back
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


EXPLORE = ["Preview", "Columns", "Quality", "Relationships", "SQL", "Contract"]
# risks the review table already shows row by row (lyst.quality); the rest (time gaps, personal data ...) stay listed
ROW_LEVEL = ("missing_values", "negative_amount", "outliers")


def explore(d: dict) -> None:
    """A dataset, opened up: preview, columns, quality, relationships, SQL and contract, one at a time."""
    name = d["name"]
    st.session_state.setdefault(f"dsv_{name}", "Preview")
    view = st.segmented_control("Show", EXPLORE, label_visibility="collapsed", key=f"dsv_{name}") or "Preview"
    if view == "Quality":
        anticipate.about("dataset", name, key=f"hf_d{name}")
        # the flagged rows themselves, for a person to review and Lyst to resolve; the table-level risks follow
        q = quality.review(name, key=f"dq_{name}", in_dialog=True)  # drawn in the dataset's sheet
        ins = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/insights")) or {}
        listed = ROW_LEVEL + (("duplicates_at_grain",) if q and (q.get("counts") or {}).get("duplicate") else ())
        risks = [r for r in ins.get("risks") or [] if q is None or r.get("kind") not in listed]
        if risks:
            kit.label("Also worth knowing")
        for k, r in enumerate(risks):
            text, tone = SEVERITY.get(r["severity"], (r["severity"], "info"))
            kit.row(r["text"], ", ".join(c.replace("_", " ") for c in r.get("columns", [])), status=(text, tone),
                    key=f"risk_{name}_{k}", divider=k < len(risks) - 1)
        governance.classification(name, d.get("classification"))
        integrations.authority(name, integrations.source_authority().get(name))
        if st.button("Ask Lyst to check its quality", icon=":material/fact_check:", key=f"askq_{name}", type="tertiary"):
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
    elif view == "Relationships":
        ins = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/insights")) or {}
        rel = ins.get("relationships") or []
        st.markdown("\n".join(f"- {md(x['text'])}" for x in rel) if rel else "No relationship to other data found yet.")
        used = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/impact")) or []
        kit.label("What depends on it")
        st.caption(", ".join(f"{u['title']} ({u['kind']})" for u in used) if used else "Nothing approved reads it yet.")
        transforms.panel(d.get("source"))
    elif view == "SQL":
        q = f'SELECT *\nFROM "{name}"\nLIMIT 50'
        st.code(q, language="sql")
        if st.button("Open in the SQL workspace", icon=":material/terminal:", key=f"sqlo_{name}", type="tertiary"):
            _set_sql(q)
            close_sheet()
            st.query_params["view"] = "sql"
            st.rerun()
    else:
        contract(name)


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


def sync_body(conn: dict, *, after: object = None) -> None:
    """Load one of a source's tables as a dataset. ``after`` runs once it synced (a sheet goes back to the source)."""
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
                if callable(after):
                    after()
                st.rerun()


@st.dialog("Sync a table", width="large")
def sync(conn: dict) -> None:
    sync_body(conn)


def enrich(catalog: list[dict]) -> list[dict]:
    """Each dataset with its catalog entry (freshness, contract, risks, authority), read once per run."""
    try:
        info = {c["name"]: c for c in api.get(f"/v1/workspaces/{ws}/catalog")}
    except APIError:
        info = {}
    return [{**d, **info.get(d["name"], {})} for d in catalog]


_OBJECTS = None
TYPE_KIND = (("date", "d"), ("time", "d"), ("int", "n"), ("double", "n"), ("float", "n"), ("decimal", "n"),
             ("numeric", "n"), ("real", "n"), ("bool", "b"), ("char", "t"), ("text", "t"), ("string", "t"))


def _objects():
    """Register Data's objects once per process (the component's code is read from data_component/)."""
    global _OBJECTS
    if _OBJECTS is None:
        from pathlib import Path  # noqa: PLC0415

        from streamlit.components.v2 import component  # noqa: PLC0415

        here = Path(__file__).resolve().parents[1] / "data_component"
        _OBJECTS = component("lyst_data_objects", css=(here / "objects.css").read_text(encoding="utf-8"),
                             js=(here / "objects.js").read_text(encoding="utf-8"))
    return _OBJECTS


def kind_of(t: str) -> str:
    """A column's type as one letter for its dataset's face: n(umber), t(ext), d(ate), b(oolean), o(ther)."""
    t = (t or "").lower()
    return next((k for w, k in TYPE_KIND if w in t), "o")


def source_of(d: dict, conns: list[dict]) -> str:
    """Which source a dataset came from: a connection, a dataset Lyst built, or an uploaded file."""
    src = str(d.get("source") or "")
    for c in conns:
        if src == c["id"] or src.startswith((f"{c['id']}:", f"{c.get('kind')}:{c.get('name')}:")):  # sync's own mark
            return c["id"]
    return "built" if src.startswith(("transform", "built")) else "files"


def _month(day: str | None) -> str | None:
    if not day:
        return None
    try:
        import datetime as _dt  # noqa: PLC0415

        return _dt.date.fromisoformat(str(day)[:10]).strftime("%b %Y")
    except ValueError:
        return str(day)


def sources_for(every: list[dict], src: dict) -> list[dict]:
    """The source objects: everything, then each place data comes from, with how many datasets it gives."""
    counts: dict[str, int] = {}
    for d in every:
        counts[source_of(d, src["conns"])] = counts.get(source_of(d, src["conns"]), 0) + 1
    out = [{"id": "all", "label": "All data", "icon": "all", "count": len(every)}]
    if counts.get("files") or not src["conns"]:
        staged = len(src["staged"])
        out.append({"id": "files", "label": "Files", "icon": "file", "count": counts.get("files", 0),
                    "tone": "warn" if staged else "", "hint": f"{staged} upload{'s' * (staged != 1)} to review"
                    if staged else "Uploaded spreadsheets and documents"})
    for c in src["conns"]:
        tone = _state(c, src["health"].get(c["id"]) or {})[1]
        out.append({"id": c["id"], "label": c["name"], "icon": "api" if "rest" in c["kind"] or "api" in c["kind"]
                    else "database", "count": counts.get(c["id"], 0), "tone": tone if tone in ("warn", "bad") else "",
                    "hint": f"{c['kind']} · {_state(c, src['health'].get(c['id']) or {})[0]}"})
    if counts.get("built"):
        out.append({"id": "built", "label": "Built by Lyst", "icon": "built", "count": counts["built"]})
    if editor:
        out.append({"id": "+", "label": "Connect", "icon": "add", "add": True, "hint": "Connect a database or service"})
    return out


def uses_for(feeds: dict[str, list[dict]], every: list[dict]) -> list[dict]:
    """What the data feeds, for the flow's right-hand lane: each approved metric once, with the datasets it reads."""
    titles = {d["name"]: d["title"] for d in every}
    out: dict[str, dict] = {}
    for ds, metrics in feeds.items():
        for m in metrics:
            u = out.setdefault(m["name"], {"id": m["name"], "title": m.get("title") or m["name"].replace("_", " "),
                                           "from": [], "fromTitles": []})
            if ds not in u["from"]:
                u["from"].append(ds)
                u["fromTitles"].append(titles.get(ds, ds))
    for u in out.values():
        u["hint"] = f"{u['title']} · from {', '.join(u['fromTitles'])}"
    return sorted(out.values(), key=lambda u: (u["from"][0], u["title"]))


def objects(every: list[dict], src: dict) -> str | None:
    """Sources, datasets and what they feed, as a flow. A click opens the thing as a sheet over the page."""
    q = (st.session_state.get("ds_find") or "").strip().lower()
    where = st.session_state.get("data_source") or "all"
    # a picked source lights its path rather than hiding the rest, so the whole flow stays in view
    shown = [d for d in every if not q or q in (d["title"] + d["name"] + (d.get("description") or "")).lower()]
    pick = st.session_state.get("data_pick")
    if pick and not any(d["name"] == pick for d in every):
        pick = st.session_state["data_pick"] = None
    feeds = _metrics_by_dataset()
    rows = []
    for d in shown:
        h = health(d)
        tone = h[1] if h and h[1] in ("warn", "bad") else "ok"
        meta = " · ".join(x for x in (f"{int(d.get('rows') or d.get('row_count') or 0):,} rows",
                                      f"through {_month((d.get('freshness') or {}).get('latest'))}"
                                      if (d.get("freshness") or {}).get("latest") else None) if x)
        rows.append({"name": d["name"], "title": d["title"], "meta": meta, "tone": tone,
                     "status": h[0] if tone != "ok" else None, "from": source_of(d, src["conns"]),
                     "feeds": len(feeds.get(d["name"]) or []),
                     "types": [kind_of(c.get("type", "")) for c in d.get("schema") or []]})
    data = {"sources": sources_for(every, src), "source": where, "datasets": rows, "selected": pick,
            "uses": uses_for(feeds, every),
            "empty": f"Nothing matches “{q}”." if q else "No datasets from this source yet."}
    res = _objects()(data=data, key="lyst_data_objects", on_action_change=lambda: None)
    ev = getattr(res, "action", None)
    if isinstance(ev, dict) and ev.get("nonce") != st.session_state.get("_data_nonce"):
        st.session_state["_data_nonce"] = ev.get("nonce")
        oid = str(ev.get("id") or ev.get("name") or "")
        if ev.get("type") == "source" and oid == "+":
            st.session_state["add_how_pick"] = "Connect a source"
            add_data()
        elif ev.get("type") == "source":
            if oid == "all":
                close_sheet()
            else:
                open_sheet("source", oid)
            st.rerun()
        elif ev.get("type") == "use":  # a metric the data feeds: what it is and where its number comes from
            open_sheet("metric", oid)
            st.rerun()
        elif ev.get("type") == "pick" and oid:
            open_sheet("dataset", oid)
            st.rerun()
    return pick


# ------------------------------------------------------------------------------------------ sheets over the flow
# Whatever is clicked in the flow opens as a sheet over the page, never below it: a dataset, a source, an approved
# metric (and a source's Sync). What is open is kept in session state, so a sheet survives the reruns its own
# controls cause, one sheet can lead to another (a source to its dataset, a dataset to the metric it feeds), and
# closing it (×, Escape, a click outside) puts the flow back as it was.

SHEET_ICON = {"dataset": ":material/table_chart:", "source": ":material/cable:", "metric": ":material/speed:",
              "sync": ":material/sync:"}
SOURCE_WORDS = {"files": ("Files", "Spreadsheets, tables and documents you uploaded."),
                "built": ("Built by Lyst", "Datasets Lyst built from data it already has, each with the checks that "
                          "prove it adds up.")}


def open_sheet(kind: str, oid: str, *, view: str | None = None) -> None:
    """Open ``kind``/``oid`` as the sheet over the flow (on the page's next run), lighting its path underneath."""
    st.session_state["data_open"] = {"kind": kind, "id": oid}
    if kind == "dataset":
        st.session_state["data_pick"] = oid
        if view:
            st.session_state[f"dsv_{oid}"] = view
    elif kind in ("source", "sync"):
        st.session_state["data_pick"] = None
        st.session_state["data_source"] = oid
    else:
        st.session_state["data_pick"] = None


def close_sheet() -> None:
    """The sheet is closed (or the page is left): nothing open, nothing picked, the whole flow in view."""
    st.session_state.pop("data_open", None)
    st.session_state["data_pick"] = None
    st.session_state["data_source"] = "all"


def _go_sheet(kind: str, oid: str, *, view: str | None = None) -> None:
    """From one sheet to another: a full run, so the sheet's title and contents are the new thing's."""
    open_sheet(kind, oid, view=view)
    st.rerun()


def review_quality(name: str) -> None:
    """Open a dataset's quality review: its sheet, on the Quality view."""
    _go_sheet("dataset", name, view="Quality")


def _face(types: list[str]) -> str:
    """A dataset's face, as on its card in the flow: one bar per column, coloured by what the column holds. Drawn
    in plain elements (the page's HTML drops inline SVG), so its colours follow the theme's tokens."""
    bars = "".join(f'<i class="k-{t}" style="height:{h / 28:.0%}"></i>'
                   for i, t in enumerate(types[:14] or ["o"])
                   for h in [28 if t == "d" else 18 + ((i * 5) % 10) if t == "n" else 9 if t == "b" else 13])
    return f'<span class="lyst-sh-shape" aria-hidden="true">{bars}</span>'


def _hero(face: str, kicker: str, read: str, chips: list[tuple[str, str]], stats: list[tuple[str, str]]) -> None:
    """A sheet's head: its face, what kind of thing it is, what it is in a sentence, its state, its numbers."""
    chip = "".join(f'<span class="lyst-sh-chip {kit._esc(t)}"><i></i>{kit._esc(c)}</span>' for c, t in chips if c)
    nums = "".join(f'<div class="lyst-sh-stat"><b>{kit._esc(v)}</b><span>{kit._esc(k)}</span></div>' for k, v in stats)
    st.html(f'<div class="lyst-sh"><div class="lyst-sh-face">{face}</div><div class="lyst-sh-head">'
            f'<div class="lyst-sh-kicker">{kicker}</div>'
            + (f'<p class="lyst-sh-read">{kit._esc(read)}</p>' if read else "")
            + (f'<div class="lyst-sh-chips">{chip}</div>' if chip else "") + "</div></div>"
            + (f'<div class="lyst-sh-stats">{nums}</div>' if nums else ""))


def _icon_face(name: str) -> str:
    return f'<span class="material-symbols-rounded lyst-sh-icon" aria-hidden="true">{kit._esc(name)}</span>'


def _source_label(sid: str, src: dict) -> str:
    conn = next((c for c in src["conns"] if c["id"] == sid), None)
    return conn["name"] if conn else SOURCE_WORDS.get(sid, (sid, ""))[0]


def _rows_of(d: dict) -> int:
    return int(d.get("rows") or d.get("row_count") or 0)


def dataset_sheet(d: dict, feeds: list[dict], src: dict) -> None:
    """A dataset, opened over the flow: what it is, whether to rely on it, what it feeds, the moves, then its depth
    (preview, columns, quality, relationships, SQL, contract) one view at a time."""
    from lyst.ui import actions, activity  # noqa: PLC0415

    name = d["name"]
    h = health(d)
    fresh = (d.get("freshness") or {}).get("latest")
    sid = source_of(d, src["conns"])
    said = (d.get("summary") or d.get("description") or "").replace("_", " ")
    kicker = (f'Dataset · from {kit._esc(_source_label(sid, src))} · <code>{kit._esc(name)}</code>')
    chips = [h if h else ("Ready to use", "ok")]
    if d.get("classification"):
        chips.append((str(d["classification"]).capitalize(), "off"))
    _hero(_face([kind_of(c.get("type", "")) for c in d.get("schema") or []]), kicker, said, chips,
          [("rows", f"{_rows_of(d):,}"), ("columns", str(len(d.get("schema") or []))),
           ("data through", _month(fresh) or "—"), ("feeds", f"{len(feeds)} metric{'s' * (len(feeds) != 1)}")])
    with st.container(horizontal=True, gap="small", vertical_alignment="center", key="dsh_moves"):
        if h and h[1] in ("bad", "warn") and st.button(f"Review: {h[0].lower()}", key=f"dsq_{name}", type="primary",
                                                        icon=":material/rule:"):
            st.session_state[f"dsv_{name}"] = "Quality"
        if st.button("Analyze", key=f"da_{name}", icon=":material/auto_awesome:",
                     type="secondary" if h and h[1] in ("bad", "warn") else "primary",
                     help=f"Lyst reads {d['title']} and reports what stands out"):
            ask(f"Summarise {name}: what does it contain and what stands out?")
        with st.popover("Ask about it", icon=":material/chat_bubble:", key=f"dq_pop_{name}"):
            qq = st.text_input("Ask about it", key=f"d{name}_q", placeholder=f"Ask about {d['title']}",
                               label_visibility="collapsed")
            if st.button("Use in analysis", key=f"d{name}_use", type="primary", icon=":material/arrow_upward:",
                         disabled=not qq.strip(), width="stretch"):
                ask(f"Using {name}: {qq.strip()}")
        with st.popover("More", icon=":material/more_horiz:", key=f"dm_{name}"):
            if st.button("Review data quality", key=f"d{name}_dq", type="tertiary", icon=":material/rule:",
                         width="stretch", help="Flagged rows in one table: accept, fix, or let Lyst resolve them"):
                st.session_state[f"dsv_{name}"] = "Quality"
            if st.button("Ask Lyst about its quality", key=f"d{name}_dqa", type="tertiary", icon=":material/fact_check:",
                         width="stretch"):
                ask(f"Check {name} for gaps, duplicates and unusual values. What should I know before relying on it?")
            if editor and st.button("Build a dataset from data Lyst has", type="tertiary", icon=":material/add:",
                                    key="tf_open", width="stretch"):
                close_sheet()
                st.session_state["_data_then"] = "build"
                st.rerun()
            activity.panel("dataset", name, key=f"dsact_{name}", admin=st.session_state.get("role") in ("admin", "owner"))
            actions.menu("dataset", name, page="data", label="Actions", exclude=("dataset.open",), inline=True)
    if feeds:
        with st.container(horizontal=True, gap="small", vertical_alignment="center", key="dsh_feeds"):
            st.html('<span class="lyst-sh-label">Feeds</span>', width="content")
            for m in feeds:
                if st.button(m.get("title") or m["name"], key=f"dsf_{name}_{m['name']}", icon=":material/speed:",
                             help=f"What {m.get('title') or m['name']} is and where its number comes from"):
                    _go_sheet("metric", m["name"])
    st.html('<hr class="lyst-sh-rule">')
    st.session_state.setdefault(f"dsv_{name}", "Preview")
    explore(d)


def source_sheet(sid: str, every: list[dict], src: dict) -> None:
    """A source, opened over the flow: a connection's health and what to do about it, or the files and built
    datasets with what they give. Every dataset it gives opens as its own sheet."""
    conn = next((c for c in src["conns"] if c["id"] == sid), None)
    if conn:
        connection_body(sid, in_flow=True)
        return
    label, about = SOURCE_WORDS.get(sid, (sid, ""))
    gives = sorted((d for d in every if source_of(d, src["conns"]) == sid), key=lambda d: d["title"])
    latest = max(((d.get("freshness") or {}).get("latest") or "" for d in gives), default="")
    staged = src["staged"] if sid == "files" else []
    _hero(_icon_face("description" if sid == "files" else "construction"), f"Source · {kit._esc(label)}", about,
          [(f"{len(staged)} upload{'s' * (len(staged) != 1)} waiting for review", "warn")] if staged else [],
          [("datasets", str(len(gives))), ("rows", f"{sum(_rows_of(d) for d in gives):,}"),
           ("data through", _month(latest) or "—")])
    with st.container(horizontal=True, gap="small", vertical_alignment="center", key="dsh_moves"):
        if sid == "files" and editor and st.button("Upload a file", key="ssh_upload", icon=":material/upload:",
                                                   type="primary"):
            close_sheet()
            st.session_state["_data_then"] = "upload"
            st.rerun()
        if staged and editor and st.button("Review uploads", key="ssh_staged", icon=":material/inbox:"):
            close_sheet()
            go("sources")
    kit.label(f"What it gives · {len(gives)}")
    _dataset_rows(gives, key=f"ssh_{sid}")


def _dataset_rows(gives: list[dict], *, key: str) -> None:
    if not gives:
        st.caption("No datasets from it yet.")
        return
    with kit.card(f"{key}_list"):
        for i, d in enumerate(gives):
            fresh = (d.get("freshness") or {}).get("latest")
            if kit.row(d["title"], " · ".join(x for x in (f"{_rows_of(d):,} rows", f"through {_month(fresh)}"
                                                          if fresh else None) if x),
                       status=health(d), actions=[("Open", "open")], key=f"{key}_{d['name']}",
                       divider=i < len(gives) - 1):
                _go_sheet("dataset", d["name"])


def metric_sheet(mid: str, every: list[dict]) -> None:
    """An approved metric the data feeds, opened over the flow: its latest value and trend, what it means, how it
    is calculated and the datasets it reads. Its full definition (versions, retiring) stays in Brain."""
    from lyst.semantic import MetricSpec, value_expr  # noqa: PLC0415

    m = next((x for ms in _metrics_by_dataset().values() for x in ms if x["name"] == mid), None)
    if not m:
        st.caption("This metric is no longer approved.")
        return
    title = m.get("title") or mid.replace("_", " ")
    p = visuals.pulse(api, ws).get(mid) or {}
    dims = ", ".join(x.replace("_", " ") for x in m.get("dimensions") or [])
    _hero(_icon_face("speed"), f"Approved metric · <code>{kit._esc(mid)}</code>",
          m.get("description") or f"{title}, as the workspace agreed to calculate it.",
          [("Higher is better", "ok") if m.get("higher_is_better") is True else
           ("Lower is better", "ok") if m.get("higher_is_better") is False else ("", "")],
          [("latest" + (f" · {p['period']}" if p.get("period") else ""), str(p.get("value_text") or "—")),
           ("split by", dims or "nothing"), ("over time by", str(m.get("time_column") or "—").replace("_", " "))])
    if p.get("history"):
        with kit.card(f"msh_{mid}_trend"):
            st.html(f'<div class="lyst-sh-trend"><div>{visuals.delta(p.get("change_text", "") + " vs prior "
                                                                     + str(p.get("grain", "period")), p.get("good"))}'
                    f'</div>{visuals.spark(p["history"], label=f"{title}, last {len(p["history"])} "
                                           f"{p.get("grain", "period")}s", height=72, width=760)}</div>')
    with st.container(horizontal=True, gap="small", vertical_alignment="center", key="dsh_moves"):
        if st.button("Ask about it", key=f"msh_ask_{mid}", type="primary", icon=":material/auto_awesome:"):
            ask(f"How has {title} moved recently, and why?")
        if st.button("Open in Brain", key=f"msh_brain_{mid}", icon=":material/psychology:",
                     help="Its full definition: versions, synonyms and who owns it"):
            close_sheet()
            st.switch_page("app_pages/brain.py", query_params={"view": "definitions", "metric": mid})
    kit.label("How it's calculated")
    try:
        st.code(value_expr(MetricSpec.model_validate(m)), language="sql")
    except Exception:  # noqa: BLE001 - an older definition the current spec cannot read: say so rather than fail
        st.caption("Its formula is shown in Brain.")
    reads = [d for d in every if d["name"] == m.get("dataset")]
    kit.label("Reads")
    _dataset_rows(reads, key=f"msh_{mid}")


def sheet(every: list[dict], src: dict) -> None:
    """Draw the open sheet, if any, over the page."""
    o = st.session_state.get("data_open")
    if not isinstance(o, dict):
        return
    kind, oid = o.get("kind"), str(o.get("id") or "")
    d = next((x for x in every if x["name"] == oid), None)
    conn = next((c for c in src["conns"] if c["id"] == oid), None)
    if kind == "dataset" and d:
        title, body = d["title"], lambda: dataset_sheet(d, _metrics_by_dataset().get(oid) or [], src)
    elif kind == "source" and (conn or oid in SOURCE_WORDS):
        title, body = _source_label(oid, src), lambda: source_sheet(oid, every, src)
    elif kind == "metric":
        m = next((x for ms in _metrics_by_dataset().values() for x in ms if x["name"] == oid), None)
        title, body = (m or {}).get("title") or oid.replace("_", " "), lambda: metric_sheet(oid, every)
    elif kind == "sync" and conn:
        title, body = f"Sync a table from {conn['name']}", lambda: sync_body(conn, after=lambda: open_sheet("source", oid))
    else:
        close_sheet()
        return
    st.html('<span class="lyst-sheet-open" hidden></span>')
    st.dialog(title, width="large", icon=SHEET_ICON.get(str(kind)), on_dismiss=close_sheet)(body)()


@st.dialog("How the data connects", width="large")
def data_map_sheet(every: list[dict]) -> None:
    data_map(every)
    pairs = _pairs(every)
    if pairs:
        titles = {c["name"]: c["title"] for c in every}
        st.dataframe(pd.DataFrame([{"Dataset": titles[a], "Connects to": titles[b],
                                    "Shared columns": ", ".join(sorted(cols)).replace("_", " ")}
                                   for (a, b), cols in sorted(pairs.items())]), hide_index=True, width="stretch")


def _rail_label(text: str, n: int) -> None:
    st.html(f'<div class="lyst-rail-label"><span>{kit._esc(text)}</span><span>{n}</span></div>')


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


STATE = {"healthy": ("Healthy", "ok"), "degraded": ("Degraded", "warn"), "failing": ("Failing", "bad"),
         "needs_action": ("Needs action", "bad"), "untested": ("Not tested", "off")}
CATEGORY = {"auth": "sign-in", "permission": "permissions", "rate_limit": "rate limit", "timeout": "time-out",
            "network": "network", "source": "the source itself", "not_found": "a missing table or path",
            "schema": "a changed layout", "config": "settings", "unknown": "an unknown cause"}
ACTION = {"test": "Test", "discover": "List tables", "read": "Read", "load": "Load", "schema_change": "Layout changed"}


@st.dialog("Source", width="large")
def source_detail(cid: str) -> None:
    connection_body(cid)


def connection_body(cid: str, *, in_flow: bool = False) -> None:
    """Why a source is healthy or not, what to do about it, what it feeds, and what happened recently. ``in_flow``:
    drawn as the Data flow's sheet, where its datasets and Sync open as sheets of their own."""
    h = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/connections/{cid}/health"))
    if not h:
        return
    text, tone = STATE.get(h["state"], (h["state"], "info"))
    day = h.get("last_24h") or {}
    if in_flow:
        _hero(_icon_face("api" if "rest" in h["kind"] or "api" in h["kind"] else "database"),
              f"Source · {kit._esc(h['kind'])}",
              "Lyst reads it with least privilege and never writes back; each table loads as a versioned dataset.",
              [(text, tone)], [("datasets", str(len(h.get("tables") or []))),
                               ("last worked", kit.when(h.get("last_success_at")) or "never"),
                               ("operations, 24 h", str(day.get("operations", 0))),
                               ("errors, 24 h", str(day.get("errors", 0)))])
        with st.container(horizontal=True, gap="small", vertical_alignment="center", key="dsh_moves"):
            if st.button("Test", icon=":material/network_check:", key=f"src_test_{cid}", type="primary"):
                kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connections/{cid}/test"), "Connection works")
            if editor and st.button("Sync a table", icon=":material/sync:", key=f"src_sync_{cid}"):
                _go_sheet("sync", cid)
    else:
        top = st.columns([4, 1.4], vertical_alignment="center")
        top[0].markdown(f"**{md(h['name'])}** · {h['kind']}")
        kit.chip(text, tone, top[1])
    if h.get("required_action"):
        st.error(md(h["required_action"]), icon=":material/build:")
    anticipate.about("connection", cid, key=f"hf_c{cid}")
    for r in h.get("reasons") or []:
        st.caption(md(r))
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
            if in_flow:
                _go_sheet("dataset", t["dataset"])
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
    if in_flow:
        return
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


ADVANCED = {"sources": ("Sources", ":material/cable:"), "dashboards": ("Dashboards", ":material/space_dashboard:"),
            "saved analyses": ("Saved analyses", ":material/bookmark:"), "sql": ("SQL", ":material/terminal:")}


def go(view: str | None) -> None:
    if view:
        st.query_params["view"] = view
    else:
        st.query_params.pop("view", None)
    st.rerun()


def header(every: list[dict]) -> None:
    """The title, one search, one way to add, and the deeper places (sources, dashboards, saved analyses, SQL)
    folded behind one quiet menu."""
    cols = st.columns([3, 2.4], vertical_alignment="center")
    cols[0].html('<div class="lyst-data"></div><h1 class="lyst-title lyst-data-title">Data</h1>')
    with cols[1].container(horizontal=True, gap="small", vertical_alignment="center", horizontal_alignment="right",
                           key="data_tools"):
        st.text_input("Find data", placeholder="Find a dataset", label_visibility="collapsed", key="ds_find",
                      icon=":material/search:", width=220)
        if editor and st.button("Add data", type="primary", icon=":material/add:", key="data_add"):
            st.session_state["add_how_pick"] = "Upload a file"
            add_data()
        with st.popover("More", icon=":material/more_horiz:", key="data_more", help="Sources, dashboards, saved "
                        "analyses, SQL and how the data connects"):
            for v, (label, icon) in ADVANCED.items():
                if st.button(label, key=f"data_go_{v.replace(' ', '_')}", icon=icon, type="tertiary", width="stretch"):
                    go(v)
            if st.button("How the data connects", key="data_go_map", icon=":material/hub:", type="tertiary",
                         width="stretch"):
                data_map_sheet(every)


def focus(every: list[dict], src: dict) -> None:
    """Lyst's one line about the data: the dataset that most needs a look, or that everything is ready."""
    tone, sentence, flagged = read(every, src)
    staged = len(src["staged"])
    if flagged:
        c, h = flagged[0]
        more = f" · {len(flagged) - 1} more" if len(flagged) > 1 else ""
        if st.button(f"{c['title']}: {h[0].lower()} before you rely on it{more}", key=f"read_{c['name']}_open",
                     type="tertiary", icon=":material/fiber_manual_record:", help=sentence):
            review_quality(c["name"])
    elif staged and editor:
        if st.button(f"{staged} upload{'s' * (staged != 1)} waiting for review", key="read_staged", type="tertiary",
                     icon=":material/fiber_manual_record:"):
            go("sources")
    else:
        st.html(f'<p class="lyst-data-read" role="status">{kit._esc(sentence)}</p>')


def advanced(view: str, catalog: list[dict], src: dict) -> None:
    st.html('<div class="lyst-data"></div>')
    if st.button("Data", key="data_back", icon=":material/arrow_back:", type="tertiary", help="Back to the datasets"):
        go(None)
    if view == "sql":  # the other views open with their own named intro
        st.html('<h1 class="lyst-title" style="font-size:1.6rem">SQL</h1>')
    {"sources": lambda: sources_view(src), "dashboards": dashboards_view, "saved analyses": saved_view,
     "sql": lambda: sql(catalog)}[view]()


try:
    st.html(_CSS)
    catalog = api.get(f"/v1/workspaces/{ws}/datasets")
    every = enrich(catalog)
    src = sources_state()
    if st.query_params.get("connection"):  # a link to one source opens it where sources live
        st.query_params["view"] = "sources"
    if (asked := st.query_params.get("dataset")) and any(d["name"] == asked for d in every):
        st.query_params.pop("dataset", None)  # a link from search, a pin or a source opens it as a sheet
        # "Review data quality" from anywhere opens the sheet on its Quality view
        open_sheet("dataset", asked, view="Quality" if st.query_params.pop("review", None) == "quality" else None)
    view = (st.query_params.get("view") or "").lower()
    if view in ADVANCED:
        advanced(view, catalog, src)
    else:
        header(every)
        if st.query_params.get("add") and editor:  # "Add data" from Home's composer opens the same sheet here
            st.query_params.pop("add", None)
            st.session_state["add_how_pick"] = "Upload a file"
            add_data()
        if not catalog:
            if empty_panel("datasets", ":material/database:", "No data yet", "Upload a spreadsheet, a CSV or a "
                           "document, or connect a database, and Lyst can answer questions about it.",
                           action="Add data" if editor else None):
                add_data()
        else:
            focus(every, src)
            objects(every, src)
            then = st.session_state.pop("_data_then", None)  # a sheet's move that opens a sheet of its own
            if then == "build" and editor:
                transforms.build()
            elif then == "upload" and editor:
                st.session_state["add_how_pick"] = "Upload a file"
                add_data()
            else:
                sheet(every, src)
            onboarding.data_suggestions()
        zone.board("data")
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
