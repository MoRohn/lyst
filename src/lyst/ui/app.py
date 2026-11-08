# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's interface. Run with `lyst ui` (the API must be running: `lyst serve`)."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # src/, when run by `streamlit run`

from lyst.ui import collab, glossary, kit, palette  # noqa: E402
from lyst.ui.client import APIError, client  # noqa: E402

ASSETS = Path(__file__).parent / "assets"
st.set_page_config(page_title="Lyst", page_icon=str(ASSETS / "lyst-mark.svg"), layout="wide")


def brand() -> None:
    """Lyst's mark at the top of the rail: the navigation is a slim column of icons, so the mark is the identity."""
    st.logo(str(ASSETS / "lyst-mark.svg"), size="large", icon_image=str(ASSETS / "lyst-mark.svg"))


brand()
kit.style()

here = Path(__file__).parent / "app_pages"


def make_pages(waiting: int = 0) -> list:
    """The four areas, in one place: the sidebar. Home carries the count of things waiting on you; Work lives inside
    Brain (Brain › Work), and the hidden work page forwards old /work links there."""
    return [
        st.Page(here / "analyst.py", title=f"Home · {waiting}" if waiting else "Home", icon=":material/home:",
                default=True),
        st.Page(here / "data.py", title="Data", icon=":material/database:"),
        st.Page(here / "brain.py", title="Brain", icon=":material/psychology:"),
        st.Page(here / "settings.py", title="Settings", icon=":material/settings:"),
        # the artifact viewer: reached from wherever an artifact is shown, never a navigation area of its own
        st.Page(here / "artifact.py", title="Artifact", icon=":material/open_in_full:", url_path="artifact",
                visibility="hidden"),
        st.Page(here / "work.py", title="Work", icon=":material/view_kanban:", url_path="work", visibility="hidden"),
    ]


pages = make_pages()

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
if (pending := st.session_state.pop("_switch_ws", None)) in ids:  # chosen in the command palette
    st.session_state.workspace = pending
if st.session_state.get("workspace") not in ids:
    st.session_state.workspace = ids[0]
role = next(w["role"] for w in spaces if w["id"] == st.session_state.workspace)
st.session_state.role = role
st.session_state.me_id = me.get("user_id")


WAITING_TTL = 20  # seconds; any successful action (kit.guarded) refreshes it at once


def _waiting(ws: str) -> int:
    """Things that need this person, counted at most every WAITING_TTL seconds per workspace instead of five API
    calls on every rerun of every widget. Acting on something through kit.guarded drops the cached count."""
    import time  # noqa: PLC0415

    hit = st.session_state.get("_waiting")
    if hit and hit[0] == ws and time.monotonic() - hit[1] < WAITING_TTL:
        return hit[2]
    n = _count_waiting(ws)
    st.session_state["_waiting"] = (ws, time.monotonic(), n)
    return n


def _count_waiting(ws: str) -> int:
    n = 0
    try:
        if role in ("admin", "owner"):
            n += len(api.get(f"/v1/workspaces/{ws}/approvals"))
        n += sum(1 for i in api.get(f"/v1/workspaces/{ws}/inbox") if i["read_status"] == "unread")
        n += len(api.get(f"/v1/workspaces/{ws}/assignments", mine=True, status="open"))
        n += len(api.get(f"/v1/workspaces/{ws}/reviews", status="pending", for_me=True))
        from lyst.ui.anticipate import _wants_person  # noqa: PLC0415

        n += sum(1 for a in api.get(f"/v1/workspaces/{ws}/anticipations") if _wants_person(a))
    except APIError:
        pass
    return n


st.session_state.waiting = _waiting(st.session_state.workspace)
from lyst.ui import agent  # noqa: E402 - what Lyst is doing; Home's count adds answers that wait on the person

nav = st.navigation(make_pages(agent.waiting(agent.state(st.session_state.workspace))), position="sidebar")


def _ws_name(i: str) -> str:
    return next(w["name"] for w in spaces if w["id"] == i)


def _footer() -> None:
    """The rail's foot: search, notifications, the glossary, and who and where (workspace, account, sign out), as
    four quiet icons. Every label stays in the accessible name and the tooltip; only the eye sees icons."""
    with st.container(key="lyst_side_foot"), st.container(key="lyst_side_icons", gap="small"):
        palette.trigger(spaces)
        collab.bell()
        with st.popover("What these words mean", icon=":material/help:", key="lyst_side_help",
                        help="What these words mean"):
            st.html(f"<style>{glossary.CSS}</style>" + glossary.html())
        who = me.get("name") or me["email"]
        with st.popover(f"{_ws_name(st.session_state.workspace)} · {who}", icon=":material/account_circle:",
                        key="lyst_side_account", help=f"{who} · {_ws_name(st.session_state.workspace)}"):
            st.html(f'<p class="lyst-side-who"><b>{kit._esc(who)}</b>'
                    + (f'<br>{kit._esc(me["email"])}' if who != me["email"] else "")
                    + f'<br>{kit._esc(role.capitalize())} · {kit._esc(_ws_name(st.session_state.workspace))}</p>')
            if len(ids) > 1:
                st.selectbox("Workspace", ids, key="workspace", format_func=_ws_name)
            if st.button("Sign out", type="tertiary", icon=":material/logout:", key="lyst_side_signout"):
                try:
                    api.post("/auth/logout")
                except APIError:
                    pass
                st.session_state.pop("token", None)
                st.rerun()


with st.sidebar:
    _footer()
    from lyst.ui import activity  # noqa: PLC0415 - events lane: live activity, on every page

    activity.live()

from lyst.ui import agent_bar  # noqa: E402 - the Lyst agent bar: ask, speak, go anywhere, watch Lyst work

if nav.url_path != "artifact":
    agent_bar.bar(agent_bar.PAGE_OF.get(nav.url_path, "home"))
nav.run()
