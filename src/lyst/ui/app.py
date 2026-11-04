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
api = client()


def setup_page() -> None:
    """First run: no trust anchor holds the master key. Initialise it from the engine host, once."""
    st.markdown("## Welcome to Lyst")
    try:
        status = api.get("/setup/status")
    except APIError as e:
        st.error(str(e))
        st.stop()
    st.write("Lyst encrypts everything at rest: its databases and every credential you connect. The master key is kept "
             "outside the data, in this machine's credential store, and never beside the database.")
    if st.session_state.get("recovery_key"):
        st.success("Encryption is initialised and the engine is running.", icon=":material/verified_user:")
        st.warning("This is the only time the recovery copy is shown. Store it offline (a password manager or a sealed envelope). "
                   "Without it, a machine whose credential store is lost cannot open these databases.", icon=":material/key:")
        st.code(st.session_state["recovery_key"], language=None)
        if st.button("I have stored it. Continue", type="primary"):
            del st.session_state["recovery_key"]
            st.rerun()
        st.stop()
    if not status["loopback"]:
        st.info("Initialise from the machine that runs the engine (open the interface there, or run `lyst init` on it). "
                "Unattended deployments mount the key at $LYST_SECRETS_DIR/LYST_MASTER_KEY or serve it from OpenBao.",
                icon=":material/computer:")
        st.stop()
    if status["keyring"] is None:
        st.error("This machine has no usable credential store. Mount the key at $LYST_SECRETS_DIR/LYST_MASTER_KEY, set "
                 "LYST_OPENBAO_URL, or inject LYST_MASTER_KEY from your orchestrator, then restart the engine.")
        st.stop()
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**New installation**")
        st.caption(f"Generate a master key into the {status['keyring']} credential store and show a one-time recovery copy.")
        if st.button("Initialise encryption", type="primary", icon=":material/enhanced_encryption:"):
            try:
                out = api.post("/setup/initialize", {})
                st.session_state["recovery_key"] = out["recovery_key"]
                st.rerun()
            except APIError as e:
                st.error(str(e))
    with c2:
        st.markdown("**Recover an installation**")
        st.caption("Paste the recovery copy of an existing installation's master key.")
        with st.form("recover"):
            key = st.text_input("Recovery key", type="password", autocomplete="off")
            if st.form_submit_button("Restore") and key:
                try:
                    api.post("/setup/initialize", {"recovery_key": key.strip()})
                    st.rerun()
                except APIError as e:
                    st.error(str(e))
    st.stop()


try:
    _setup = bool(api.get("/health").get("setup_required"))
except APIError as e:
    st.error(e if e.status else f"{e} Start it with `lyst serve`.", icon=":material/cloud_off:")
    st.stop()
nav = st.navigation(pages, position="hidden" if _setup else "top")
if _setup:
    setup_page()

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
