# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import pandas as pd
import streamlit as st

from lyst.semantic import MetricSpec, value_expr
from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
st.markdown("## Intelligence")
tabs = st.tabs(["Metrics", "Glossary", "Knowledge", "Documents", "Memory", "Prior analyses"])

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
    st.caption("Entities and how they connect. Lyst records metric → dataset → column lineage and analysis → metric "
               "links itself; admins add business units, teams, systems, processes and objectives.")
    ents = api.get(f"/v1/workspaces/{ws}/entities")
    if ents:
        st.dataframe(pd.DataFrame(ents)[["kind", "name", "description", "owner", "source"]], hide_index=True, width="stretch")
        pick = st.selectbox("Lineage of", [f"{e['kind']}:{e['name']}" for e in ents if e["kind"] in ("metric", "dataset", "analysis")],
                            index=None)
        if pick:
            k, n = pick.split(":", 1)
            lin = api.get(f"/v1/workspaces/{ws}/lineage/{k}/{n}")
            c1, c2 = st.columns(2)
            c1.markdown("**Upstream** (what it depends on)")
            c1.dataframe(pd.DataFrame(lin["upstream"]), hide_index=True, width="stretch")
            c2.markdown("**Downstream** (what depends on it)")
            c2.dataframe(pd.DataFrame(lin["downstream"]), hide_index=True, width="stretch")
    if st.session_state.get("role") in ("admin", "owner"):
        with st.expander("Add an entity or a relation"):
            with st.form("entity"):
                ec = st.columns([1, 2, 3])
                kind = ec[0].selectbox("Kind", ["business_unit", "team", "person", "system", "process", "objective"])
                name = ec[1].text_input("Name")
                desc = ec[2].text_input("Description")
                if st.form_submit_button("Save entity") and name:
                    api.post(f"/v1/workspaces/{ws}/entities", {"kind": kind, "name": name, "description": desc})
                    st.rerun()
            with st.form("relation"):
                rc = st.columns(5)
                fk = rc[0].text_input("From kind", "team")
                fn = rc[1].text_input("From name")
                rel = rc[2].text_input("Relation", "owns")
                tk = rc[3].text_input("To kind", "metric")
                tn = rc[4].text_input("To name")
                if st.form_submit_button("Save relation") and fn and tn:
                    api.post(f"/v1/workspaces/{ws}/relations", {"from_kind": fk, "from_name": fn, "rel": rel, "to_kind": tk, "to_name": tn})
                    st.rerun()

with tabs[3]:
    docs = api.get(f"/v1/workspaces/{ws}/documents")
    if docs:
        st.dataframe(pd.DataFrame(docs)[["title", "filename", "classification", "min_role", "passages", "added_at"]],
                     hide_index=True, width="stretch")
    q = st.text_input("Search documents", placeholder="revenue recognition policy")
    if q:
        for hit in api.get(f"/v1/workspaces/{ws}/documents/search", q=q):
            with st.container(border=True):
                st.caption(f"{hit['title']} · passage {hit['passage']} · score {hit['score']}")
                st.write(hit["text"])
    with st.form("doc"):
        f = st.file_uploader("Text, Markdown or PDF", type=["txt", "md", "pdf", "csv"])
        dt = st.text_input("Title")
        mr = st.selectbox("Readable by", ["viewer", "editor", "admin", "owner"])
        if st.form_submit_button("Add document", type="primary") and f:
            try:
                r = api.post(f"/v1/workspaces/{ws}/documents", None, data={"title": dt or f.name, "min_role": mr},
                             files={"file": (f.name, f.getvalue())})
                st.success(f"Added {r['title']}: {r['passages']} passages")
            except APIError as e:
                st.error(str(e))

with tabs[4]:
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

with tabs[5]:
    st.dataframe(pd.DataFrame(api.get(f"/v1/workspaces/{ws}/analyses")), hide_index=True, width="stretch")
