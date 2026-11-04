# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import pandas as pd
import streamlit as st

from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
st.markdown("## Data")
tabs = st.tabs(["Datasets", "Connections", "Query", "History", "Add data"])
catalog = api.get(f"/v1/workspaces/{ws}/datasets")

with tabs[0]:
    if not catalog:
        st.caption("No datasets yet. Add one in Add data.")
    for d in catalog:
        with st.expander(f"**{d['title']}** · `{d['name']}` · v{d['version']} · {d['row_count']:,} rows · {d['classification']}"):
            st.caption(d.get("description") or "")
            st.dataframe(pd.DataFrame(d["schema"]), hide_index=True, width="stretch")
            if st.toggle("Profile", key=f"prof_{d['name']}"):
                p = api.get(f"/v1/workspaces/{ws}/datasets/{d['name']}/profile")
                cols = pd.DataFrame([{k: c.get(k) for k in ("name", "role", "dtype", "nulls", "unique", "issues")}
                                     for c in p.get("columns", [])])
                st.dataframe(cols, hide_index=True, width="stretch",
                             column_config={"nulls": st.column_config.ProgressColumn("Nulls", min_value=0, max_value=1)})

with tabs[1]:
    st.caption("A connection reads a source with least privilege (PostgreSQL transactions are opened READ ONLY). "
               "A sync loads a table into a governed dataset; with a cursor column, later syncs add only new rows. "
               "Each sync is a new dataset version.")
    conns = api.get(f"/v1/workspaces/{ws}/connections")
    if conns:
        st.dataframe(pd.DataFrame(conns)[["name", "kind", "status", "last_tested_at", "last_error"]], hide_index=True, width="stretch")
        pick = st.selectbox("Connection", [c["id"] for c in conns], format_func=lambda i: next(c["name"] for c in conns if c["id"] == i))
        cc = st.columns([1, 1, 6])
        if cc[0].button("Test"):
            try:
                st.json(api.post(f"/v1/workspaces/{ws}/connections/{pick}/test"))
            except APIError as e:
                st.error(str(e))
        try:
            tables = api.get(f"/v1/workspaces/{ws}/connections/{pick}/tables")
        except APIError as e:
            tables, _ = [], st.error(str(e))
        if tables:
            tref = st.selectbox("Table", [t["ref"] for t in tables])
            tsel = next(t for t in tables if t["ref"] == tref)
            st.dataframe(pd.DataFrame(tsel["columns"]), hide_index=True, width="stretch")
            with st.form("sync"):
                sc = st.columns([2, 2, 2, 1])
                dataset = sc[0].text_input("Dataset name", value=tref.split(".")[-1])
                cursor = sc[1].selectbox("Cursor column (incremental)", [None] + [c["name"] for c in tsel["columns"]])
                every = sc[2].selectbox("Then sync every", [None, 3600, 86400, 604800],
                                        format_func=lambda s: {None: "manually", 3600: "hour", 86400: "day", 604800: "week"}[s])
                if sc[3].form_submit_button("Sync", type="primary"):
                    try:
                        r = api.post(f"/v1/workspaces/{ws}/connections/{pick}/sync",
                                     {"source_table": tref, "dataset": dataset, "cursor_column": cursor, "every_seconds": every})
                        st.success(f"{r['dataset']} v{r['version']}: {r['rows_added']:,} rows added ({r['mode']}), {r['rows_total']:,} total"
                                   + ("; schedule proposed for approval" if r.get("schedule") else ""))
                    except APIError as e:
                        st.error(str(e))
        syncs = api.get(f"/v1/workspaces/{ws}/connections/{pick}/syncs")
        if syncs:
            st.dataframe(pd.DataFrame(syncs)[["source_table", "dataset", "cursor_column", "last_sync_at", "last_rows_added", "rows_total", "syncs"]],
                         hide_index=True, width="stretch")
    with st.expander("Add a connection", expanded=not conns):
        kinds = api.get("/v1/connectors")
        kind = st.selectbox("Kind", [k["kind"] for k in kinds], format_func=lambda k: next(x["label"] for x in kinds if x["kind"] == k))
        man = next(k for k in kinds if k["kind"] == kind)
        st.caption(man["notes"])
        with st.form("connection"):
            name = st.text_input("Name")
            values = {}
            for f in man["fields"]:
                values[f["name"]] = st.text_input(f["label"] + ("" if f["required"] else " (optional)"), value=f["default"] or "",
                                                  type="password" if f["secret"] else "default")
            if st.form_submit_button("Test and save", type="primary") and name:
                try:
                    r = api.post(f"/v1/workspaces/{ws}/connections", {"kind": kind, "name": name, "values": values})
                    st.success(f"Saved {r['name']}: {r['test']}")
                    st.rerun()
                except APIError as e:
                    st.error(str(e))

with tabs[2]:
    sql = st.text_area("SQL (read-only, against dataset names)", height=140,
                       value=f"SELECT * FROM {catalog[0]['name']} LIMIT 50" if catalog else "")
    if st.button("Run query", type="primary", icon=":material/play_arrow:"):
        try:
            r = api.post(f"/v1/workspaces/{ws}/query", {"sql": sql, "max_rows": 5000})
            st.caption(f"{len(r['rows'])} rows · {r['ms']} ms · query #{r['query_id']}")
            st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, width="stretch")
            with st.expander("Executed SQL"):
                st.code(r["executed_sql"], language="sql")
        except APIError as e:
            st.error(str(e))

with tabs[3]:
    st.dataframe(pd.DataFrame(api.get(f"/v1/workspaces/{ws}/queries")), hide_index=True, width="stretch")

with tabs[4]:
    with st.form("upload"):
        f = st.file_uploader("CSV, TSV or Parquet", type=["csv", "tsv", "parquet"])
        name = st.text_input("Dataset name", placeholder="sales_orders")
        desc = st.text_input("Description")
        if st.form_submit_button("Add dataset", type="primary") and f and name:
            try:
                r = api.post(f"/v1/workspaces/{ws}/datasets", None, data={"name": name, "description": desc},
                             files={"file": (f.name, f.getvalue())})
                st.success(f"{r['dataset']} v{r['version']}: {r['rows']:,} rows"
                           + (" (unchanged; same file as the current version)" if r["unchanged"] else ""))
            except APIError as e:
                st.error(str(e))
