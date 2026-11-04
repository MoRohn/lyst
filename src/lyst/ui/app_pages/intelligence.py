import pandas as pd
import streamlit as st

from lyst.semantic import MetricSpec, value_expr
from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
st.markdown("## Intelligence")
tabs = st.tabs(["Metrics", "Glossary", "Memory", "Prior analyses"])

with tabs[0]:
    for m in api.get(f"/v1/workspaces/{ws}/metrics"):
        with st.expander(f"**{m['title']}** · `{m['name']}` · {m['dataset']}"):
            st.write(m["description"])
            st.code(value_expr(MetricSpec.model_validate(m)), language="sql")
            st.caption(f"Split by: {', '.join(m['dimensions']) or 'none'} · time: {m['time_column']} · "
                       f"also called: {', '.join(m['synonyms']) or 'nothing else'}")

with tabs[1]:
    g = api.get(f"/v1/workspaces/{ws}/glossary")
    st.dataframe(pd.DataFrame(g), hide_index=True, width="stretch")
    if st.session_state.get("role") in ("admin", "owner"):
        with st.form("term"):
            term = st.text_input("Term")
            definition = st.text_input("Definition")
            maps = st.text_input("Maps to metric (optional)")
            if st.form_submit_button("Save term") and term and definition:
                try:
                    api.post(f"/v1/workspaces/{ws}/glossary", {"term": term, "definition": definition,
                                                                 "maps_to_metric": maps or None})
                    st.rerun()
                except APIError as e:
                    st.error(str(e))

with tabs[2]:
    mem = api.get(f"/v1/workspaces/{ws}/memory")
    st.dataframe(pd.DataFrame(mem), hide_index=True, width="stretch")
    with st.form("correction"):
        st.caption("Record a correction. It is stored as user-validated team knowledge; an admin can promote it.")
        text = st.text_area("Correction", placeholder="Revenue for EMEA excludes intercompany sales.")
        if st.form_submit_button("Save correction") and text:
            try:
                api.post(f"/v1/workspaces/{ws}/memory/corrections", {"content": text})
                st.rerun()
            except APIError as e:
                st.error(str(e))
    if st.session_state.get("role") in ("admin", "owner") and mem:
        pid = st.selectbox("Promote to organizational knowledge", [m["id"] for m in mem if m["trust"] != "approved"],
                           index=None, format_func=lambda i: next(m["content"][:80] for m in mem if m["id"] == i))
        if pid and st.button("Promote"):
            api.post(f"/v1/workspaces/{ws}/memory/{pid}/promote")
            st.rerun()

with tabs[3]:
    st.dataframe(pd.DataFrame(api.get(f"/v1/workspaces/{ws}/analyses")), hide_index=True, width="stretch")
