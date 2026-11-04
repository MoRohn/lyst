# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's interface. Run with `lyst ui` (the API must be running: `lyst serve`)."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # src/, when run by `streamlit run`

from lyst.ui.client import APIError, client  # noqa: E402

st.set_page_config(page_title="Lyst", page_icon=":material/insights:", layout="wide")

here = Path(__file__).parent / "app_pages"
pages = [
    st.Page(here / "analyst.py", title="Analyst", icon=":material/insights:", default=True),
    st.Page(here / "work.py", title="Work", icon=":material/view_kanban:"),
    st.Page(here / "data.py", title="Data", icon=":material/database:"),
    st.Page(here / "intelligence.py", title="Intelligence", icon=":material/menu_book:"),
    st.Page(here / "build.py", title="Build", icon=":material/construction:"),
    st.Page(here / "settings.py", title="Settings", icon=":material/settings:"),
]
nav = st.navigation(pages, position="top")

api = client()
try:
    me = api.get("/v1/me")
except APIError as e:
    if e.status == 403:
        health = {}
        try:
            health = api.get("/health")
        except APIError:
            pass
        st.markdown("## Lyst")
        if health.get("oidc"):
            st.link_button("Sign in", f"{api.base}/auth/login", type="primary", icon=":material/login:")
        else:
            st.info("Sign-in is not configured on this engine. Paste an API token (an administrator issues one "
                    "with `lyst token you@example.com`).")
            with st.form("token"):
                t = st.text_input("API token", type="password")
                if st.form_submit_button("Continue") and t:
                    st.session_state["token"] = t.strip()
                    st.rerun()
    else:
        st.error(e if e.status else f"{e} Start it with `lyst serve`.", icon=":material/cloud_off:")
    st.stop()

spaces = me["workspaces"]
if not spaces:
    st.info("You are not a member of any workspace yet.")
    st.stop()
ids = [w["id"] for w in spaces]
if st.session_state.get("workspace") not in ids:
    st.session_state.workspace = ids[0]
with st.sidebar:
    st.markdown("### Lyst")
    st.selectbox("Workspace", ids, key="workspace",
                 format_func=lambda i: next(w["name"] for w in spaces if w["id"] == i))
    role = next(w["role"] for w in spaces if w["id"] == st.session_state.workspace)
    st.caption(f"{me['email']} · {role}")
    if st.button("Sign out", type="tertiary", icon=":material/logout:"):
        try:
            api.post("/auth/logout")
        except APIError:
            pass
        st.session_state.pop("token", None)
        st.rerun()
st.session_state.role = role
nav.run()
