# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data: what Lyst can read, where it comes from, and a place to query it. Adding data is one button."""

import re

import pandas as pd
import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace

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


@st.dialog("Dataset", width="large")
def dataset_detail(d: dict) -> None:
    st.markdown(f"**{d['title']}** · `{d['name']}`")
    st.caption(f"{d['row_count']:,} rows · version {d['version']} · updated {kit.when(d['refreshed_at'])} · "
               f"{str(d['classification']).capitalize()}" + (f" · {d['description']}" if d.get("description") else "")
               + f" · referred to as {d['name']} in SQL")
    view = st.segmented_control("Show", ["Preview", "Columns", "Quality"], default="Preview",
                                label_visibility="collapsed") or "Preview"
    if view == "Preview":
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/query", {"sql": f'SELECT * FROM "{d["name"]}" LIMIT 50',
                                                                        "max_rows": 50}))
        if r:
            st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, width="stretch", height=320)
    elif view == "Columns":
        st.dataframe(pd.DataFrame(d["schema"]), hide_index=True, width="stretch")
    else:
        p = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{d['name']}/profile"))
        if p:
            issues = [(c["name"], i) for c in p.get("columns", []) for i in (c.get("issues") or [])]
            if issues:
                st.warning("\n".join(f"- **{n}**: {i}" for n, i in issues), icon=":material/report:")
            else:
                st.success("No column-level issues found.", icon=":material/check:")
            st.dataframe(pd.DataFrame([{k: c.get(k) for k in ("name", "role", "dtype", "nulls", "unique")}
                                       for c in p.get("columns", [])]), hide_index=True, width="stretch",
                         column_config={"nulls": st.column_config.ProgressColumn("Nulls", min_value=0, max_value=1)})
    if st.button("Ask Lyst to check its quality", icon=":material/fact_check:"):
        ask(f"Profile {d['name']} for data quality issues")


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
    q = st.text_input("Find", placeholder="Find a dataset", label_visibility="collapsed", key="ds_find") \
        if len(catalog) > 6 else ""
    shown = [d for d in catalog if not q or q.lower() in (d["title"] + d["name"] + (d.get("description") or "")).lower()]
    for i, d in enumerate(shown):
        hit = kit.row(d["title"], " · ".join(x for x in (d.get("description"), f"{d['row_count']:,} rows",
                                                           f"updated {kit.when(d['refreshed_at'])}",
                                                           str(d["classification"]).capitalize()) if x),
                      actions=[("Open", "open")], key=f"d{d['name']}", divider=i < len(shown) - 1)
        if hit:
            dataset_detail(d)


def connections() -> None:
    conns = api.get(f"/v1/workspaces/{ws}/connections")
    if not conns:
        if kit.empty(":material/cable:", "No connections", "Connect PostgreSQL, SQLite, a REST API or a file at a URL. "
                     "Lyst reads with least privilege and loads tables as governed, versioned datasets.",
                     action="Connect a source" if editor else None, key="cn_empty"):
            add_data()
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
    if kit.header("Data", "The data Lyst answers from, where it comes from, and (for analysts) a place to query it.",
                  action="Add data" if editor else None, icon=":material/add:"):
        add_data()
    view = kit.sections(["Datasets", "Connections", "SQL"], key="view")
    {"Datasets": lambda: datasets(catalog), "Connections": connections, "SQL": lambda: sql(catalog)}[view]()
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
