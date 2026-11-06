# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's interface. Run with `lyst ui` (the API must be running: `lyst serve`)."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # src/, when run by `streamlit run`

from lyst.ui import collab, kit, palette  # noqa: E402
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
with st.sidebar:
    palette.trigger(spaces)
    st.selectbox("Workspace", ids, key="workspace", label_visibility="collapsed",
                 format_func=lambda i: next(w["name"] for w in spaces if w["id"] == i))
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


def _recent(ws: str) -> list[dict]:
    """The person's latest questions in this workspace, for one-click return; refreshed like the waiting count."""
    import time  # noqa: PLC0415

    hit = st.session_state.get("_recent")
    if hit and hit[0] == ws and time.monotonic() - hit[1] < WAITING_TTL:
        return hit[2]
    try:
        tasks = [t for t in api.get(f"/v1/workspaces/{ws}/tasks") if t.get("kind", "analysis") == "analysis"][:4]
    except APIError:
        tasks = []
    st.session_state["_recent"] = (ws, time.monotonic(), tasks)
    return tasks


RECENT_ICON = {"queued": ":material/schedule:", "running": ":material/progress_activity:",
               "awaiting_approval": ":material/approval:", "failed": ":material/error:",
               "paused": ":material/pause:"}

def _pins(ws: str) -> list[dict]:
    hit = st.session_state.get("_pins")
    if hit and hit[0] == ws:
        return hit[1]
    try:
        pins = api.get(f"/v1/workspaces/{ws}/pins")[:6]
    except APIError:
        pins = []
    st.session_state["_pins"] = (ws, pins)
    return pins


PIN_PAGE = {"dataset": ("app_pages/data.py", "dataset"), "metric": ("app_pages/brain.py", "metric")}

with st.sidebar:
    if pins := _pins(st.session_state.workspace):
        kit.label("Pinned")
        from lyst.ui import artifacts as _art  # noqa: PLC0415

        for p in pins:
            if st.button(p.get("title") or p["id"], key=f"lyst_recent_pin_{p['kind']}_{p['id']}".replace(":", "_"),
                         type="tertiary", width="stretch", icon=_art.KINDS.get(p["kind"], ("", ":material/keep:"))[1]
                         if p["kind"] in _art.KINDS else ":material/keep:"):
                if p["kind"] in PIN_PAGE:
                    page, qk = PIN_PAGE[p["kind"]]
                    st.switch_page(page, query_params={qk: p["id"]})
                _art.open_artifact({"kind": p["kind"], "id": p["id"]})
    if recent := _recent(st.session_state.workspace):
        kit.label("Recent")
        for t in recent:
            if st.button(t.get("title") or "Untitled", key=f"lyst_recent_{t['id']}", type="tertiary", width="stretch",
                         icon=RECENT_ICON.get(t["status"], ":material/chat_bubble:"),
                         help=f"{t['status'].replace('_', ' ').capitalize()} · {kit.when(t.get('updated_at'))}"):
                st.session_state.task_id = t["id"]
                st.switch_page("app_pages/analyst.py", query_params={"task": t["id"]})
        st.html('<div style="height:.4rem"></div>')
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
    collab.bell()
    from lyst.ui import activity  # noqa: PLC0415 - events lane: live activity, on every page

    activity.live()
    with st.popover("What these words mean", icon=":material/help:", width="stretch"):
        st.markdown(GLOSSARY)
nav.run()
