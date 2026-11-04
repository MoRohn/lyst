# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import json

import pandas as pd
import streamlit as st

from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
st.markdown("## Build")
tabs = st.tabs(["Skills", "Capabilities", "Evaluation"])

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

with tabs[0]:
    st.caption("A skill is a Python function `run(df, **params) -> dict` over a governed query. It runs in a sandbox, "
               "must pass its own tests and Lyst's adversarial cases, and is registered only after an owner who did "
               "not propose it approves. Then the analyst plans with it whenever a request matches its trigger phrases.")
    skills = api.get(f"/v1/workspaces/{ws}/skills")
    if skills:
        st.dataframe(pd.DataFrame(skills), hide_index=True, width="stretch")
        sel = st.selectbox("Open", [f"{s['name']} v{s['version']}" for s in skills], index=None)
        if sel:
            name, ver = sel.rsplit(" v", 1)
            s = api.get(f"/v1/workspaces/{ws}/skills/{name}", version=int(ver))
            st.code(s["source"], language="python")
            st.code(s["permissions"]["input_sql"], language="sql")
            if s.get("test_report"):
                rep = s["test_report"]
                if rep.get("static"):
                    st.error("Static check: " + "; ".join(rep["static"]))
                if rep.get("cases"):
                    st.dataframe(pd.DataFrame([{k: c.get(k) for k in ("name", "kind", "passed", "error")} for c in rep["cases"]]),
                                 hide_index=True, width="stretch")
            b = st.columns(3)
            try:
                if s["status"] in ("proposed", "failed", "tested") and b[0].button("Run tests", type="primary"):
                    r = api.post(f"/v1/workspaces/{ws}/skills/{name}/{ver}/test")
                    st.toast(f"{name} v{ver}: {r['status']}")
                    st.rerun()
                if s["status"] == "tested" and b[1].button("Request registration"):
                    api.post(f"/v1/workspaces/{ws}/skills/{name}/{ver}/register")
                    st.toast("Approval requested; an owner decides in Work › Approvals.")
                if s["status"] == "approved" and b[2].button("Retire"):
                    api.post(f"/v1/workspaces/{ws}/skills/{name}/retire")
                    st.rerun()
            except APIError as e:
                st.error(str(e))
    with st.expander("Propose a skill", expanded=not skills):
        with st.form("skill"):
            name = st.text_input("Name", placeholder="revenue_concentration")
            purpose = st.text_input("Purpose", placeholder="Share of revenue from the top regions")
            triggers = st.text_input("Trigger phrases (comma-separated)", placeholder="revenue concentration")
            input_sql = st.text_area("Input query (read-only, dataset names)", height=90,
                                     value="SELECT region, sum(revenue) AS revenue FROM pnl GROUP BY region")
            source = st.text_area("Python", value=EXAMPLE_SOURCE, height=240)
            tests = st.text_area("Tests (JSON)", value=EXAMPLE_TESTS, height=200)
            if st.form_submit_button("Propose", type="primary"):
                try:
                    r = api.post(f"/v1/workspaces/{ws}/skills", {
                        "name": name, "purpose": purpose, "source": source, "input_sql": input_sql,
                        "tests": json.loads(tests), "triggers": [t.strip() for t in triggers.split(",") if t.strip()]})
                    if r["status"] == "rejected":
                        st.error("Refused: " + "; ".join(r["problems"]))
                    else:
                        st.success(f"Proposed {r['name']} v{r['version']}. Run its tests next.")
                        st.rerun()
                except (APIError, ValueError) as e:
                    st.error(str(e))

with tabs[1]:
    caps = pd.DataFrame(api.get("/v1/capabilities"))
    st.dataframe(caps, hide_index=True, width="stretch",
                 column_config={"mutating": st.column_config.CheckboxColumn(),
                                "requires_approval": st.column_config.CheckboxColumn()})

with tabs[2]:
    st.caption("Evaluation runs score Lyst's answers against independently computed expectations. "
               "Run `uv run lyst eval` to add one.")
    runs = api.get(f"/v1/workspaces/{ws}/evals")
    if runs:
        st.dataframe(pd.DataFrame(runs), hide_index=True, width="stretch")
    else:
        st.caption("No evaluation runs recorded for this workspace.")
