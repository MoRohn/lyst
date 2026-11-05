# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data › Python: code over governed data in the OS sandbox. Inputs are named SQL queries; the result, output and
error show below, every run is recorded and can be reproduced or saved as a dataset."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

EXAMPLE = 'result = df.describe()\nprint(len(df), "rows read")'


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("editor", "admin", "owner")


def available() -> dict:
    api, _, _ = _ctx()
    try:
        return (api.get("/v1/environment") or {}).get("python") or {}
    except APIError:
        return {}


def editor(catalog: list[dict]) -> None:
    api, ws, editor_ = _ctx()
    env = available()
    if not env.get("available"):
        st.caption(md(env.get("detail") or "Python is not available on this installation."))
        return
    lim = env.get("limits") or {}
    st.caption(md(f"Runs in a sandbox ({env.get('detail')}); up to {lim.get('timeout_seconds', 60):g} s and "
                  f"{lim.get('max_input_rows', 0):,} input rows. Assign the answer to `result`."))
    names = [d["name"] for d in catalog]
    first = "pnl" if "pnl" in names else (names[0] if names else "pnl")
    n = st.number_input("Inputs", 1, 5, 1, key="wb_n")
    inputs = {}
    for i in range(int(n)):
        c = st.columns([1, 4])
        nm = c[0].text_input("Name", value="df" if i == 0 else f"df{i + 1}", key=f"wb_in_{i}")
        sql = c[1].text_input("SQL", value=f"SELECT * FROM {first}" if i == 0 else "", key=f"wb_sql_{i}")
        if nm.strip() and sql.strip():
            inputs[nm.strip()] = sql.strip()
    code = st.text_area("Python", EXAMPLE, height=160, key="wb_code")
    if st.button("Run", type="primary", icon=":material/play_arrow:", key="wb_run", disabled=not editor_):
        st.session_state["wb_last"] = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workbench/python",
                                                                   {"code": code, "inputs": inputs}))
    show(st.session_state.get("wb_last"))
    recent(ws)


def show(r: dict | None) -> None:
    if not r:
        return
    api, ws, editor_ = _ctx()
    ok = r.get("status") == "ok"
    st.caption(md(" · ".join(x for x in ("Ran" if ok else "Failed", f"{r['seconds']:.2f} s" if r.get("seconds") else None,
                                         ", ".join(f"{k}: {v.get('rows', 0):,} rows" for k, v in (r.get("inputs") or {}).items()),
                                         f"run {r['id']}") if x)))
    if r.get("error"):
        st.error(md(r["error"]), icon=":material/error:")
    res = r.get("result") or {}
    if r.get("result_kind") == "table" and res.get("preview"):
        st.dataframe(pd.DataFrame(res["preview"], columns=res.get("columns")), hide_index=True, width="stretch")
        st.caption(f"{res.get('rows', len(res['preview'])):,} rows")
    elif res:
        st.code(str(res.get("value", res)), language=None)
    if r.get("stdout"):
        with st.expander("Output", icon=":material/terminal:"):
            st.code(r["stdout"], language=None)
    c = st.columns([1.4, 2.4, 1.6, 2])
    if c[0].button("Reproduce", icon=":material/replay:", key=f"wb_rep_{r['id']}"):
        again = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workbench/runs/{r['id']}/reproduce"))
        if again:
            same = again.get("result_digest") and again.get("result_digest") == r.get("result_digest")
            st.toast("Same result" if same else "The result differs now", icon=":material/replay:")
    if ok and r.get("result_kind") == "table" and editor_:
        name = c[1].text_input("Dataset name", placeholder="revenue_by_region", label_visibility="collapsed", key=f"wb_nm_{r['id']}")
        if c[2].button("Save as dataset", icon=":material/save:", key=f"wb_save_{r['id']}", disabled=not name):
            kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workbench/runs/{r['id']}/save", {"name": name}), "Saved")


def recent(ws: str) -> None:
    api, _, _ = _ctx()
    try:
        runs = api.get(f"/v1/workspaces/{ws}/workbench/runs", limit=8)
    except APIError:
        return
    runs = [x for x in runs if x.get("kind") == "python"]
    if not runs:
        return
    with st.expander(f"Recent runs · {len(runs)}", icon=":material/history:"):
        for i, x in enumerate(runs):
            if kit.row(((x.get("code") or "").strip().splitlines() or ["(empty)"])[0][:80], f"{x.get('status')} · {kit.when(x.get('created_at'))}",
                       actions=[("Open", "open")], key=f"wbr{x['id']}", divider=i < len(runs) - 1):
                st.session_state["wb_last"] = kit.guarded(lambda y=x: api.get(f"/v1/workspaces/{ws}/workbench/runs/{y['id']}"))
                st.rerun()
