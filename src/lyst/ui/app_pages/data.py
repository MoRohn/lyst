# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import pandas as pd
import streamlit as st

from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
st.markdown("## Data")
tabs = st.tabs(["Datasets", "Query", "History", "Add data"])
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

with tabs[2]:
    st.dataframe(pd.DataFrame(api.get(f"/v1/workspaces/{ws}/queries")), hide_index=True, width="stretch")

with tabs[3]:
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
