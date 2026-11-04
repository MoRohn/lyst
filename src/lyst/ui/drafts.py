# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Connections Lyst proposes from an API description: one per list endpoint, with the reasons, a dry run that reads
two pages and writes nothing, and promotion by an admin other than the person who proposed it. Tokens are typed
into a password field, sent once, and never shown back."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

STATUS = {"draft": ("Proposed", "info"), "tested": ("Tested", "ok"), "promoted": ("Connected", "verified"),
          "rejected": ("Rejected", "off")}


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("editor", "admin", "owner")


def _flat(d: dict) -> dict:
    """Stored drafts keep the proposal (endpoint, summary, fields, reasons) under ``proposal``."""
    return {**(d.get("proposal") or {}), **{k: v for k, v in d.items() if k != "proposal"}}


def from_description() -> None:
    """Add data › From an API description."""
    api, ws, editor = _ctx()
    if not editor:
        st.caption("Editors and admins can propose connections.")
        return
    st.caption("Give Lyst the service's OpenAPI description. It proposes a read-only connection for each list it "
               "offers, with its reasons. Nothing connects until it has been tested and another admin approves it.")
    name = st.text_input("Name", placeholder="Procurement API", key="cd_name")
    f = st.file_uploader("OpenAPI description (JSON)", type=["json"], key="cd_file")
    pasted = st.text_area("…or paste it", height=100, key="cd_paste", placeholder='{"openapi": "3.0.3", …}')
    base = st.text_input("Base URL (optional, when the description has none)", key="cd_base")
    if st.button("Propose connections", type="primary", icon=":material/auto_awesome:", key="cd_go",
                 disabled=not name or not (f or pasted.strip())):
        try:
            spec = json.loads(f.getvalue() if f else pasted)
        except ValueError as e:
            st.error(f"That is not valid JSON: {e}")
            return
        out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connector-drafts",
                                           {"name": name, "openapi": spec, "base_url": base or None}))
        if out:
            st.toast(f"{len(out)} connection{'s' * (len(out) != 1)} proposed; they are listed under Sources.",
                     icon=":material/auto_awesome:")
            st.rerun()


def listed() -> None:
    """Data › Sources: proposed connections waiting to be tested or approved."""
    api, ws, _ = _ctx()
    try:
        found = [_flat(d) for d in api.get(f"/v1/workspaces/{ws}/connector-drafts") if d.get("status") in ("draft", "tested")]
    except APIError:
        return
    if not found:
        return
    kit.label(f"Proposed connections · {len(found)}")
    for i, d in enumerate(found):
        if kit.row(f"{d['name']}: {d.get('endpoint') or ''}", d.get("summary") or "", status=STATUS.get(d["status"]),
                   actions=[("Open", "open")], key=f"cd{d['id']}", divider=i < len(found) - 1):
            detail(d["id"])


@st.dialog("Proposed connection", width="large")
def detail(did: str) -> None:
    api, ws, editor = _ctx()
    admin = st.session_state.get("role") in ("admin", "owner")
    d = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/connector-drafts/{did}"))
    if not d:
        return
    d = _flat(d)
    top = st.columns([4, 1.4], vertical_alignment="center")
    top[0].markdown(md(f"**{d['name']}** · `{d.get('endpoint') or ''}` → dataset {d.get('table') or ''}"))
    kit.chip(*STATUS.get(d["status"], (d["status"], "info")), where=top[1])
    if d.get("summary"):
        st.markdown(md(d["summary"]))
    if d.get("why"):
        with st.expander("Why Lyst proposes it this way", icon=":material/lightbulb:", expanded=True):
            st.markdown("\n".join(f"- {md(w)}" for w in d["why"]))
    fields = d.get("fields") or []
    if fields:
        st.caption("Fields: " + md(", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in fields)))
    if editor and d["status"] in ("draft", "tested"):
        names = [str(x.get("name") if isinstance(x, dict) else x) for x in fields]
        cur = d.get("cursor_column")
        pick = st.selectbox("Load only new rows using", [None] + names, index=([None] + names).index(cur) if cur in names else 0,
                            format_func=lambda x: "Reload everything each time" if x is None else x, key=f"cd_cur_{did}")
        if pick != cur and st.button("Save", key=f"cd_save_{did}"):
            kit.guarded(lambda: api.patch(f"/v1/workspaces/{ws}/connector-drafts/{did}",
                                          {"config": d.get("config") or {}, "cursor_column": pick}), "Saved")
        kit.label("Try it")
        tok = st.text_input("Access token" + ("" if d.get("needs_token") else " (not needed)"), type="password",
                            key=f"cd_tok_{did}", help="Used for this call only and never stored or shown again.")
        if st.button("Run a dry test", icon=":material/science:", key=f"cd_test_{did}"):
            st.session_state[f"cd_rep_{did}"] = kit.guarded(lambda: api.post(
                f"/v1/workspaces/{ws}/connector-drafts/{did}/test", {"token": tok or None}))
        rep = (st.session_state.get(f"cd_rep_{did}") or {}).get("report") or d.get("test_report")
        if rep:
            if rep.get("ok"):
                st.success(md(f"Read {rep.get('pages_read', 0)} pages ({rep.get('first_page_rows', 0)} rows on the first)"
                              + ("; the second page differs, so paging works" if rep.get("second_page_differs") else "")
                              + ". Nothing was written."), icon=":material/check:")
            else:
                st.error(md(rep.get("action") or rep.get("error") or "The test did not pass."), icon=":material/error:")
            if rep.get("missing_fields"):
                st.caption("Described but not seen: " + md(", ".join(rep["missing_fields"])))
            if rep.get("undeclared_fields"):
                st.caption("Seen but not described: " + md(", ".join(rep["undeclared_fields"])))
            if rep.get("sample"):
                with st.expander("Sample (sensitive values masked)", icon=":material/table:"):
                    st.dataframe(pd.DataFrame(rep["sample"]), hide_index=True, width="stretch")
    if admin and d["status"] == "tested":
        kit.label("Approve")
        st.caption("Another admin than the one who proposed it turns it into a connection.")
        tok2 = st.text_input("Access token for the connection", type="password", key=f"cd_ptok_{did}")
        note = st.text_input("Note", key=f"cd_note_{did}")
        b = st.columns([1.6, 1.2, 4])
        if b[0].button("Approve and connect", type="primary", key=f"cd_prom_{did}"):
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connector-drafts/{did}/promote",
                                             {"token": tok2 or None, "note": note}))
            if r:
                st.toast("Connected. Sync it from Sources.", icon=":material/check:")
                st.rerun()
        if b[1].button("Reject", type="tertiary", key=f"cd_rej_{did}"):
            if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connector-drafts/{did}/reject", {"note": note}), "Rejected"):
                st.rerun()
