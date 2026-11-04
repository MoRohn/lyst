# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Build a dataset from data Lyst already has: describe it in a sentence (or write the SELECT), see it run with
its checks, save it as a dataset that follows its inputs. Reads the session on every call."""

from __future__ import annotations

import re

import pandas as pd
import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

CHECK = {"not_empty": "Has rows", "key_unique": "One row per key", "no_null_keys": "No missing keys"}


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("editor", "admin", "owner")


def _check_label(name: str) -> str:
    if name.startswith("reconciles:"):
        return f"{name.split(':', 1)[1].replace('_', ' ').capitalize()} adds up to its source"
    return CHECK.get(name, name.replace("_", " ").capitalize())


def show_checks(checks: list[dict], advice: list[dict]) -> None:
    for i, c in enumerate(checks):
        tone = ("Passed", "ok") if c["passed"] else (("Failed", "bad") if c.get("severity") == "error" else ("Warning", "warn"))
        kit.row(_check_label(c["name"]), c.get("detail") or "", status=tone, key=f"tfc{i}_{c['name']}",
                divider=i < len(checks) - 1)
    for a in advice or []:
        st.caption(":material/lightbulb: " + md(a.get("text") or ""))


@st.dialog("Build a dataset", width="large")
def build() -> None:
    api, ws, _ = _ctx()
    st.caption("Describe the table you want from data Lyst already has. Lyst writes the query, runs it, checks the "
               "result adds up to its sources, and saves it as a dataset that refreshes when its inputs change.")
    want = st.text_input("What do you need", placeholder="Monthly revenue and operating income by region", key="tf_want")
    d = st.session_state.setdefault("tf_draft", {})
    if st.button("Draft it", icon=":material/auto_awesome:", key="tf_design", disabled=not want.strip()):
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/transforms/design", {"request": want.strip()}))
        if r:
            d.clear()
            d.update({k: r.get(k) for k in ("sql", "name", "key", "explanation", "message", "ok")})
            if r.get("sql"):
                st.session_state["tf_sql"] = r["sql"]
            if r.get("name"):
                st.session_state["tf_name"] = r["name"]
            if r.get("key"):
                st.session_state["tf_key"] = ", ".join(r["key"])
    if d.get("explanation"):
        st.markdown(md(d["explanation"]))
    if d.get("message") and not d.get("ok"):
        st.info(md(d["message"]), icon=":material/edit_note:")
    with st.expander("The query", icon=":material/code:", expanded=bool(d) and not d.get("ok")):
        sql = st.text_area("SQL", height=140, key="tf_sql", label_visibility="collapsed",
                           placeholder="SELECT month, region, sum(revenue) AS revenue FROM pnl GROUP BY month, region")
        key = st.text_input("One row per (columns, comma-separated)", key="tf_key", placeholder="month, region")
    keys = [k.strip() for k in (key or "").split(",") if k.strip()]
    if st.button("Run and check", type="primary", icon=":material/rule:", key="tf_preview", disabled=not (sql or "").strip()):
        st.session_state["tf_result"] = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/transforms/preview",
                                                                     {"sql": sql, "key": keys}))
    res = st.session_state.get("tf_result")
    if not res:
        return
    st.caption(f"{res.get('rows', 0):,} rows from {', '.join(res.get('inputs') or [])}")
    show_checks(res.get("checks") or [], res.get("advice") or [])
    if res.get("sample"):
        with st.expander("Sample", icon=":material/table:"):
            st.dataframe(pd.DataFrame(res["sample"]), hide_index=True, width="stretch")
    if not res.get("ok"):
        st.warning("Fix the failed checks before saving.", icon=":material/report:")
        return
    c = st.columns(2)
    title = c[0].text_input("Title", value=want.strip()[:80].capitalize(), key="tf_title")
    name = c[1].text_input("Dataset name", key="tf_name",
                           value=st.session_state.get("tf_name") or re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40])
    if st.button("Save as a dataset", type="primary", icon=":material/save:", key="tf_save", disabled=not name):
        out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/transforms", {
            "name": name, "sql": sql, "title": title, "description": want.strip(), "key": keys, "note": ""}))
        if out:
            for k in ("tf_draft", "tf_result", "tf_sql", "tf_key", "tf_name", "tf_want", "tf_title"):
                st.session_state.pop(k, None)
            st.toast(f"Saved {out['name']} v{out['version']}", icon=":material/check:")
            st.query_params["dataset"] = out["name"]
            st.rerun()


def panel(source: str | None) -> None:
    """Inside a dataset built by Lyst: how it is made, in words; its runs; run again or retire."""
    m = re.match(r"transform:([a-z0-9_]+)@v\d+", source or "")
    if not m:
        return
    api, ws, editor = _ctx()
    name = m.group(1)
    t = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/transforms/{name}"))
    if not t:
        return
    ex = t.get("explain") or {}
    parts = [f"Built from {', '.join(ex.get('inputs') or t.get('inputs') or [])}"]
    if ex.get("joins"):
        parts.append("joined on " + "; ".join(str(j) for j in ex["joins"]))
    if ex.get("filter"):
        parts.append(f"keeping rows where {ex['filter']}")
    if ex.get("group_by"):
        parts.append("one row per " + " × ".join(g.replace("_", " ") for g in ex["group_by"]))
    kit.label("How it is made")
    st.markdown(md(", ".join(parts) + "."))
    if ex.get("computed"):
        st.caption(md("; ".join(f"{c['column'].replace('_', ' ')} = {c['expression']}" for c in ex["computed"])))
    runs = t.get("runs") or []
    if runs:
        r = runs[0]
        st.caption(md(f"Last run {kit.when(r['ran_at'])} · {r.get('rows') or 0:,} rows · {r['outcome']}"
                      + (f" · {r['error']}" if r.get("error") else "")))
    for a in t.get("advice") or []:
        st.caption(":material/lightbulb: " + md(a.get("text") or ""))
    if editor:
        b = st.columns([1.4, 1.2, 4])
        if b[0].button("Run again", icon=":material/refresh:", key=f"tfrun_{name}"):
            kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/transforms/{name}/run?full=true"), "Rebuilt")
        if b[1].button("Retire", type="tertiary", icon=":material/archive:", key=f"tfret_{name}"):
            kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/transforms/{name}/retire"), "Retired")
    with st.expander("The query", icon=":material/code:"):
        st.code(t.get("sql") or "", language="sql")


def listed() -> list[dict]:
    api, ws, _ = _ctx()
    try:
        return api.get(f"/v1/workspaces/{ws}/transforms")
    except APIError:
        return []
