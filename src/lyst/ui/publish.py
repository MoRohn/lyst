# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Publishing a finished answer: to the workspace's members by default, or (admins) to anyone with a link that
expires. The link is shown once. Published pages are listed in Work with View, Republish and Revoke."""

from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

AUDIENCE = {"members": "People in this workspace", "link": "Anyone with the link"}
ROLES = {"viewer": "Everyone in the workspace", "editor": "Editors and above", "admin": "Admins and above"}


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("admin", "owner")


def form(task: str, title: str, *, key: str) -> None:
    """Inside Share: publish this answer as a page."""
    api, ws, admin = _ctx()
    if (link := st.session_state.get(f"pub_link_{key}")):
        st.success("Published. Copy the link now: it is shown only once.", icon=":material/link:")
        st.code(link, language=None)
        return
    st.caption("A read-only page of this answer and its evidence, frozen as it is now. Republish later with a newer answer.")
    name = st.text_input("Title", value=title[:120], key=f"pub_t_{key}")
    who = st.radio("Who can see it", list(AUDIENCE) if admin else ["members"], format_func=AUDIENCE.get, key=f"pub_a_{key}",
                   horizontal=True)
    body = {"task_id": task, "title": name, "audience": who}
    if who == "members":
        body["min_role"] = st.selectbox("Within the workspace", list(ROLES), format_func=ROLES.get, key=f"pub_r_{key}")
    else:
        body["expires_days"] = int(st.number_input("Link works for (days)", 1, 365, 30, key=f"pub_d_{key}"))
        st.caption("Anyone who has the link can read the page without signing in. Lyst refuses a link when the data "
                   "policy does not allow the data to leave the workspace.")
    if st.button("Publish", type="primary", icon=":material/public:", key=f"pub_go_{key}", width="stretch"):
        try:
            out = api.post(f"/v1/workspaces/{ws}/publications", body)
        except APIError as e:
            st.error(md(str(e)), icon=":material/block:")  # e.g. a confidential dataset, or the export ceiling
            return
        if out.get("url"):
            st.session_state[f"pub_link_{key}"] = f"{api.base.rstrip('/')}{out['url']}"
            st.rerun()
        st.toast("Published to the workspace; it is listed in Work › Activity › Published.", icon=":material/public:")


@st.dialog("Published page", width="large")
def view(p: dict) -> None:
    api, ws, _ = _ctx()
    try:
        html = api.http.get(f"/v1/workspaces/{ws}/publications/{p['id']}/content", headers=api._h()).text
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    st.caption(md(f"{p['title']} · version {p.get('current_version')} · {p.get('views', 0)} views"))
    components.html(html, height=640, scrolling=True)


def listed() -> None:
    """Work › Activity › Published."""
    api, ws, admin = _ctx()
    try:
        pubs = api.get(f"/v1/workspaces/{ws}/publications")
    except APIError as e:
        st.caption(str(e))
        return
    if not pubs:
        st.caption("Nothing published yet. On a finished answer, Share › Publish makes a read-only page of it.")
        return
    for i, p in enumerate(pubs):
        live = p.get("status") == "published"
        meta = " · ".join(x for x in (AUDIENCE.get(p.get("audience"), p.get("audience")), f"version {p.get('current_version')}",
                                      f"{p.get('views', 0)} views", f"expires {kit.when(p['expires_at'])}" if p.get("expires_at") else None,
                                      kit.when(p.get("created_at"))) if x)
        acts = ([("View", "view"), ("Republish", "re")] + ([("Revoke", "rv")] if admin or p.get("created_by") == st.session_state.get("me_id") else [])) if live else []
        hit = kit.row(p["title"], meta, status=("Published", "ok") if live else (str(p.get("status")).capitalize(), "off"),
                      actions=acts, key=f"pub{p['id']}", divider=i < len(pubs) - 1)
        if hit == "view":
            view(p)
        elif hit == "re":
            republish(p)
        elif hit == "rv":
            if kit.guarded(lambda x=p: api.post(f"/v1/workspaces/{ws}/publications/{x['id']}/revoke"), "Revoked: the page and its link stop working"):
                st.rerun()


@st.dialog("Republish", width="large")
def republish(p: dict) -> None:
    api, ws, _ = _ctx()
    st.caption("Replace the page with a newer finished answer; the link stays the same and the old version is kept.")
    try:
        done = [t for t in api.get(f"/v1/workspaces/{ws}/tasks") if t["status"] == "done"][:30]
    except APIError:
        done = []
    if not done:
        st.caption("No finished answers to publish.")
        return
    pick = st.selectbox("With", [t["id"] for t in done], format_func=lambda i: next(t["title"] for t in done if t["id"] == i),
                        key=f"rp_t_{p['id']}")
    note = st.text_input("What changed", key=f"rp_n_{p['id']}")
    if st.button("Republish", type="primary", key=f"rp_go_{p['id']}"):
        try:
            api.post(f"/v1/workspaces/{ws}/publications/{p['id']}/republish", {"task_id": pick, "note": note})
        except APIError as e:
            st.error(md(str(e)), icon=":material/block:")
            return
        st.toast("Republished", icon=":material/check:")
        st.rerun()
