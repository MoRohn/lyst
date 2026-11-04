# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's interface. Run with `lyst ui` (the API must be running: `lyst serve`)."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # src/, when run by `streamlit run`

from lyst.ui import kit  # noqa: E402
from lyst.ui.client import APIError, client  # noqa: E402

ASSETS = Path(__file__).parent / "assets"
st.set_page_config(page_title="Lyst", page_icon=str(ASSETS / "lyst-mark.svg"), layout="wide")


def brand() -> None:
    """The Lyst logo in place of a text wordmark, in the variant that reads on the active theme."""
    dark = (st.context.theme.type or "light") == "dark"
    st.logo(str(ASSETS / ("lyst-logo-on-dark.svg" if dark else "lyst-logo.svg")), size="large",
            icon_image=str(ASSETS / "lyst-mark.svg"))


brand()
kit.style()

here = Path(__file__).parent / "app_pages"

GLOSSARY = """
**Approved measure (metric)**: a definition an admin approved, such as what revenue means here. Lyst answers only
from these, so two people asking the same question get the same number.

**Verified**: every figure in the answer was recomputed independently and every required check passed.

**Depth**: how much of the method an answer shows. Summary is the answer and what to do; Detail adds the tables
and what was tested; Method adds every check and query. **Brief** is the two-line version with a PDF to forward.

**Watch a metric**: Lyst checks it on its own and investigates unusual moves. Sensitivity sets how unusual a move
must be before it tells you.

**Repeat it**: Lyst asks the same question again every day, week or month, for the latest period.

**Saved analysis (workflow)**: a verified answer Lyst can rerun for any period without working it out again.

**Needs you**: approvals, questions Lyst needs answered, and results that did not pass verification.

**Brain**: what Lyst knows about this business: approved definitions, documents, and what it has learned.

**Skill**: a new method a builder adds to Lyst in Python (Settings › Advanced). It is tested in a sandbox and
approved before use.
"""


def make_pages(waiting: int = 0) -> list:
    """The five areas, in one place: the sidebar. Work carries the count of things waiting on you."""
    return [
        st.Page(here / "analyst.py", title="Home", icon=":material/home:", default=True),
        st.Page(here / "work.py", title=f"Work · {waiting}" if waiting else "Work", icon=":material/view_kanban:",
                url_path="work"),
        st.Page(here / "data.py", title="Data", icon=":material/database:"),
        st.Page(here / "brain.py", title="Brain", icon=":material/psychology:"),
        st.Page(here / "settings.py", title="Settings", icon=":material/settings:"),
    ]


pages = make_pages()

KINDS = {"metric": ("Metric", ":material/functions:"), "dataset": ("Dataset", ":material/database:"),
         "column": ("Column", ":material/view_column:"), "term": ("Business term", ":material/menu_book:"),
         "workflow": ("Saved analysis", ":material/bolt:"), "dashboard": ("Dashboard", ":material/dashboard:"),
         "analysis": ("Earlier answer", ":material/chat:"), "document": ("Document", ":material/description:")}


def _destination(hit: dict) -> tuple[str, dict]:
    """Where a search result lives in the five areas, as a page and the query that opens it."""
    kind, name = hit["kind"], hit["name"]
    if kind == "dataset":
        return "app_pages/data.py", {"dataset": name}
    if kind == "column":
        return "app_pages/data.py", {"dataset": name.split(".")[0]}
    if kind == "metric":
        return "app_pages/brain.py", {"metric": name}
    if kind == "term":
        return "app_pages/brain.py", {}
    if kind == "document":
        return "app_pages/brain.py", {"view": "documents"}
    if kind == "workflow":
        return "app_pages/data.py", {"view": "saved analyses"}
    if kind == "dashboard":
        return "app_pages/data.py", {"view": "dashboards"}
    return "app_pages/analyst.py", {"task": name}


@st.dialog("Search", width="large")
def search_dialog(q: str) -> None:
    try:
        hits = api.get(f"/v1/workspaces/{st.session_state.workspace}/catalog/search", q=q)["results"]
    except APIError as e:
        st.error(str(e))
        return
    if not hits:
        st.caption(f"Nothing matches “{q}”. Ask it on Home instead: Lyst can answer questions it has no saved page for.")
        return
    st.caption(f"{len(hits)} result{'s' * (len(hits) != 1)} for “{q}”")
    for i, h in enumerate(hits):
        label, icon = KINDS.get(h["kind"], (h["kind"].capitalize(), None))
        if kit.row(h["title"], f"{label} · {h.get('why') or ''}", actions=[("Open", "go")], key=f"sr{i}",
                   divider=i < len(hits) - 1):
            page, qp = _destination(h)
            if page.endswith("analyst.py"):
                st.session_state.task_id = qp["task"]
            st.switch_page(page, query_params=qp)
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
if _setup:
    st.navigation(pages, position="hidden")
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
    st.selectbox("Workspace", ids, key="workspace", label_visibility="collapsed",
                 format_func=lambda i: next(w["name"] for w in spaces if w["id"] == i))
role = next(w["role"] for w in spaces if w["id"] == st.session_state.workspace)
st.session_state.role = role


def _waiting(ws: str) -> int:
    """Things that need this person: approvals they may decide, findings they have not read."""
    n = 0
    try:
        if role in ("admin", "owner"):
            n += len(api.get(f"/v1/workspaces/{ws}/approvals"))
        n += sum(1 for i in api.get(f"/v1/workspaces/{ws}/inbox") if i["read_status"] == "unread")
    except APIError:
        pass
    return n


st.session_state.waiting = _waiting(st.session_state.workspace)
nav = st.navigation(make_pages(st.session_state.waiting), position="sidebar")
with st.sidebar:
    with st.popover(me["email"], icon=":material/account_circle:", width="stretch"):
        st.caption(f"{me.get('name') or me['email']} · {role} in "
                   f"{next(w['name'] for w in spaces if w['id'] == st.session_state.workspace)}")
        if st.button("Sign out", type="tertiary", icon=":material/logout:"):
            try:
                api.post("/auth/logout")
            except APIError:
                pass
            st.session_state.pop("token", None)
            st.rerun()
    with st.popover("What these words mean", icon=":material/help:", width="stretch"):
        st.markdown(GLOSSARY)
    q = st.text_input("Search", placeholder="Search data, metrics, documents", label_visibility="collapsed",
                      key="search_q", icon=":material/search:",
                      on_change=lambda: st.session_state.update(_open_search=True))
if st.session_state.pop("_open_search", False) and (q or "").strip():
    search_dialog(q.strip())
nav.run()
