# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Skills, capabilities and evaluation runs: the builder tools, shown in Settings › Advanced."""

import json

import pandas as pd
import streamlit as st

from lyst.ui import kit
from lyst.ui.client import client, workspace


def _ctx():
    """Read per run, never at import: this module is imported once and shared by every session."""
    return client(), workspace(), st.session_state.get("role") in ("editor", "admin", "owner")

# --------------------------------------------------------------------------------------- skills

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

@st.dialog("Skill", width="large")
def skill_detail(name: str, ver: int) -> None:
    api, ws, editor = _ctx()
    s = api.get(f"/v1/workspaces/{ws}/skills/{name}", version=ver)
    st.markdown(f"**{name}** · v{ver}")
    st.caption(f"{s.get('purpose') or ''} · {s['status']}")
    st.code(s["source"], language="python")
    st.code(s["permissions"]["input_sql"], language="sql")
    rep = s.get("test_report") or {}
    if rep.get("static"):
        st.error("Static check: " + "; ".join(rep["static"]))
    if rep.get("cases"):
        st.dataframe(pd.DataFrame([{k: c.get(k) for k in ("name", "kind", "passed", "error")} for c in rep["cases"]]),
                     hide_index=True, width="stretch")
    b = st.columns(3)
    if s["status"] in ("proposed", "failed", "tested") and b[0].button("Run tests", type="primary"):
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/skills/{name}/{ver}/test"))
        if r:
            st.toast(f"Tests: {r['status']}")
            st.rerun()
    if s["status"] == "tested" and b[1].button("Request registration"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/skills/{name}/{ver}/register"),
                    "Approval requested; an owner decides in Work")
    if s["status"] == "approved" and b[2].button("Retire", type="tertiary"):
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/skills/{name}/retire"), "Retired"):
            st.rerun()


@st.dialog("Propose a skill", width="large")
def propose_skill() -> None:
    api, ws, editor = _ctx()
    st.caption("A skill is a Python function `run(df, **params) -> dict` over a governed query. It runs in a sandbox, "
               "must pass its own tests and Lyst's adversarial cases, and is registered only after an owner who did "
               "not propose it approves.")
    with st.form("skill", border=False):
        name = st.text_input("Name", placeholder="revenue_concentration")
        purpose = st.text_input("Purpose", placeholder="Share of revenue from the top regions")
        triggers = st.text_input("Use it when a request mentions (comma-separated)", placeholder="revenue concentration")
        input_sql = st.text_area("Input query (read-only)", height=80,
                                 value="SELECT region, sum(revenue) AS revenue FROM pnl GROUP BY region")
        source = st.text_area("Python", value=EXAMPLE_SOURCE, height=220)
        tests = st.text_area("Tests (JSON)", value=EXAMPLE_TESTS, height=160)
        if st.form_submit_button("Propose", type="primary"):
            try:
                body = {"name": name, "purpose": purpose, "source": source, "input_sql": input_sql,
                        "tests": json.loads(tests), "triggers": [t.strip() for t in triggers.split(",") if t.strip()]}
            except ValueError as e:
                st.error(f"The tests are not valid JSON: {e}")
                return
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/skills", body))
            if r and r["status"] == "rejected":
                st.error("Refused: " + "; ".join(r["problems"]))
            elif r:
                st.toast(f"Proposed {r['name']} v{r['version']}. Run its tests next.")
                st.rerun()


def skills() -> None:
    api, ws, editor = _ctx()
    st.caption("For builders: new methods written in Python, everything Lyst can do, and how it scores against "
               "independently computed answers.")
    sk = api.get(f"/v1/workspaces/{ws}/skills")
    if not sk:
        kit.empty(":material/extension:", "No skills yet", "A skill adds a method Lyst does not have. When a request needs "
                  "one, Lyst can draft it itself; you can also propose one.")
    tone = {"approved": "ok", "tested": "wait", "proposed": "wait", "failed": "bad", "retired": "off"}
    for i, s in enumerate(sk):
        if kit.row(s["name"], f"v{s['version']} · {s.get('purpose') or ''}",
                   status=(s["status"].capitalize(), tone.get(s["status"], "info")), actions=[("Open", "open")],
                   key=f"sk{s['name']}{s['version']}", divider=i < len(sk) - 1):
            skill_detail(s["name"], int(s["version"]))
    if editor and st.button("Propose a skill", icon=":material/add:", type="tertiary"):
        propose_skill()
    with st.expander("Everything Lyst can do (capabilities)", icon=":material/widgets:"):
        st.dataframe(pd.DataFrame(api.get("/v1/capabilities")), hide_index=True, width="stretch",
                     column_config={"mutating": st.column_config.CheckboxColumn(),
                                    "requires_approval": st.column_config.CheckboxColumn()})
    with st.expander("Evaluation runs", icon=":material/rule:"):
        runs = api.get(f"/v1/workspaces/{ws}/evals")
        if runs:
            st.dataframe(pd.DataFrame(runs), hide_index=True, width="stretch")
        else:
            st.caption("None recorded here. `lyst eval` scores Lyst against independently computed answers.")
    from lyst.ui import anticipate  # noqa: PLC0415  (working-ahead quality and the methods Lyst could add)

    anticipate.quality_block()
    from lyst.ui import continuity  # noqa: PLC0415  (installation administrators only)

    continuity.section()
