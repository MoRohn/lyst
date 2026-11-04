# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data: what Lyst reads and what it makes from it. Datasets and their sources, dashboards, saved analyses, SQL."""

import json
import re

import pandas as pd
import streamlit as st

from lyst.ui import kit, reports
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

api, ws = client(), workspace()
editor = st.session_state.get("role") in ("editor", "admin", "owner")
EVERY = {None: "Manually", 3600: "Every hour", 86400: "Every day", 604800: "Every week"}


def dataset_name(filename: str) -> str:
    base = re.sub(r"\.(csv|tsv|parquet|txt)$", "", filename.lower())
    return (re.sub(r"[^a-z0-9]+", "_", base).strip("_") or "dataset")[:40]


def ask(question: str) -> None:
    out = kit.guarded(lambda: api.ask(ws, question))
    if out:
        st.session_state.task_id = out["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})


@st.dialog("Add data", width="large")
def add_data() -> None:
    how = st.segmented_control("How", ["Upload a file", "Connect a source"], default="Upload a file",
                               label_visibility="collapsed") or "Upload a file"
    if how == "Upload a file":
        f = st.file_uploader("CSV, TSV or Parquet", type=["csv", "tsv", "parquet"])
        if f:
            c = st.columns(2)
            name = c[0].text_input("Dataset name", value=dataset_name(f.name),
                                   help="How questions and SQL refer to it. Letters, digits and underscores.")
            desc = c[1].text_input("What it contains (optional)")
            if st.button("Add dataset", type="primary", icon=":material/upload:"):
                r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/datasets", None,
                                                 data={"name": name, "description": desc},
                                                 files={"file": (f.name, f.getvalue())}))
                if r:
                    st.toast(f"{r['dataset']}: {r['rows']:,} rows" + (" (no change from the current version)"
                                                                      if r["unchanged"] else ""), icon=":material/check:")
                    st.rerun()
        return
    kinds = api.get("/v1/connectors")
    kind = st.selectbox("Source", [k["kind"] for k in kinds],
                        format_func=lambda k: next(x["label"] for x in kinds if x["kind"] == k))
    man = next(k for k in kinds if k["kind"] == kind)
    st.caption(man["notes"])
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
    name = d["name"]
    st.markdown(f"**{d['title']}**")
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
            st.dataframe(pd.DataFrame([{"Column": c["name"], "Kind": (c.get("semantic_type") or c.get("role") or "").replace("_", " "),
                                        "Type": c.get("type"), "Missing": c.get("nulls"), "Distinct values": c.get("distinct"),
                                        "Notes": "; ".join(c.get("issues") or [])} for c in ins["columns"]]),
                         hide_index=True, width="stretch",
                         column_config={"Missing": st.column_config.ProgressColumn("Missing", min_value=0, max_value=1)})
            cols = st.multiselect("If these columns changed, what would be affected?", [c["name"] for c in ins["columns"]],
                                  key=f"imp_{name}")
            if cols:
                hits = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/impact", columns=",".join(cols))) or []
                st.markdown("\n".join(f"- **{md(h['title'])}** ({h['kind']}): {md(h['why'])}" for h in hits)
                            if hits else "Nothing approved reads those columns.")
    else:
        contract(name)


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


def datasets(catalog: list[dict]) -> None:
    if not catalog:
        if kit.empty(":material/database:", "No data yet", "Upload a CSV or Parquet file, or connect a database, and "
                     "Lyst can answer questions about it.", action="Add data" if editor else None, key="ds_empty"):
            add_data()
        return
    try:
        info = {c["name"]: c for c in api.get(f"/v1/workspaces/{ws}/catalog")}
    except APIError:
        info = {}
    q = st.text_input("Find", placeholder="Find a dataset", label_visibility="collapsed", key="ds_find") \
        if len(catalog) > 6 else ""
    shown = [d for d in catalog if not q or q.lower() in (d["title"] + d["name"] + (d.get("description") or "")).lower()]
    for i, d in enumerate(shown):
        c = {**d, **info.get(d["name"], {})}
        fresh = (c.get("freshness") or {}).get("latest")
        hit = kit.row(d["title"], " · ".join(x for x in (d.get("description"), f"{d['row_count']:,} rows",
                                                           f"data through {fresh}" if fresh else
                                                           f"updated {kit.when(d['refreshed_at'])}",
                                                           str(d["classification"]).capitalize()) if x),
                      status=health(c), actions=[("Open", "open")], key=f"d{d['name']}", divider=i < len(shown) - 1)
        if hit or st.query_params.get("dataset") == d["name"]:
            st.query_params.pop("dataset", None)
            dataset_detail(c)
def connections() -> None:
    conns = api.get(f"/v1/workspaces/{ws}/connections")
    kit.label(f"Sources · {len(conns)}" if conns else "Sources")
    if not conns:
        st.caption("No database or service is connected. Add data › Connect a source links PostgreSQL, SQLite, a REST "
                   "API or a file at a URL; Lyst reads with least privilege and loads tables as versioned datasets.")
        return
    for i, c in enumerate(conns):
        hit = kit.row(c["name"], f"{c['kind']} · tested {kit.when(c.get('last_tested_at')) or 'never'}"
                      + (f" · {c['last_error']}" if c.get("last_error") else ""),
                      status=("Connected", "ok") if c["status"] == "ok" else (c["status"].capitalize(), "warn"),
                      actions=[("Test", "test"), ("Sync", "sync")], key=f"c{c['id']}", divider=i < len(conns) - 1)
        if hit == "test":
            kit.guarded(lambda x=c: api.post(f"/v1/workspaces/{ws}/connections/{x['id']}/test"), "Connection works")
        elif hit == "sync":
            sync(c)


def dashboards_view() -> None:
    if editor and st.button("New dashboard", icon=":material/dashboard_customize:", type="tertiary"):
        reports.new_dashboard()
    reports.dashboards()


def sql(catalog: list[dict]) -> None:
    st.caption("Read-only SQL against dataset names. Column and row policies apply, and every query is recorded.")
    text = st.text_area("SQL", height=130, label_visibility="collapsed", key="sql_text",
                        value=f'SELECT * FROM {catalog[0]["name"]} LIMIT 50' if catalog else "")
    if st.button("Run", type="primary", icon=":material/play_arrow:"):
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/query", {"sql": text, "max_rows": 5000}))
        if r:
            st.caption(f"{len(r['rows']):,} rows · {r['ms']} ms")
            st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, width="stretch")
    mine = [h for h in api.get(f"/v1/workspaces/{ws}/queries", limit=200) if not h.get("task_id")][:8]
    if mine:
        kit.label("Your recent queries")
        for h in mine:
            st.caption(f"{kit.when(h['occurred_at'])} · {h.get('rows') or 0:,} rows · {h['outcome']}")
            st.code(h.get("sql", ""), language="sql")


try:
    catalog = api.get(f"/v1/workspaces/{ws}/datasets")
    if kit.header("Data", "The data Lyst answers from, the dashboards and saved analyses it makes, and a place to "
                  "query it.", action="Add data" if editor else None, icon=":material/add:"):
        add_data()
    view = kit.sections(["Datasets", "Dashboards", "Saved analyses", "SQL"], key="view")
    {"Datasets": lambda: (datasets(catalog), connections()), "Dashboards": dashboards_view,
     "Saved analyses": reports.workflows, "SQL": lambda: sql(catalog)}[view]()
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
