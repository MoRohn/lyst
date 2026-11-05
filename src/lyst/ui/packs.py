# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Settings › Workspace › Move or copy this workspace: seal it into a passphrase-encrypted pack to download, and
(for installation administrators) check a pack and bring it in as a new workspace. Reads the session per call."""

from __future__ import annotations

import re

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

MIN = 12


def _ctx():
    return client(), workspace()


def _size(n: int | None) -> str:
    n = n or 0
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


def section() -> None:
    api, ws = _ctx()
    st.caption("A pack holds this workspace's data, definitions, documents and settings (never secrets or other "
               "people's sign-ins), sealed with a passphrase. Download it within 24 hours.")
    c = st.columns(2)
    p1 = c[0].text_input("Passphrase", type="password", key="pk_p1", help=f"At least {MIN} characters. Lyst cannot recover it.")
    p2 = c[1].text_input("Passphrase again", type="password", key="pk_p2")
    hist = st.checkbox("Include history (past answers, runs and audit)", key="pk_hist")
    ok = len(p1) >= MIN and p1 == p2
    if p1 and p2 and p1 != p2:
        st.caption(":material/error: The two passphrases differ.")
    elif p1 and len(p1) < MIN:
        st.caption(f":material/error: Use at least {MIN} characters.")
    if st.button("Make a pack", type="primary", icon=":material/inventory_2:", key="pk_make", disabled=not ok):
        out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/exports",
                                           {"passphrase": p1, "encrypt": True, "include_history": hist}))
        if out:
            st.toast(f"Pack ready: {out.get('tables')} tables, {out.get('rows', 0):,} rows, {_size(out.get('bytes'))}",
                     icon=":material/inventory_2:")
    try:
        packs = api.get(f"/v1/workspaces/{ws}/exports")
    except APIError:
        packs = []
    for i, pk in enumerate(packs[:5]):
        c = st.columns([5, 1.6], vertical_alignment="center")
        c[0].markdown(md(f"**{pk['id']}** · {_size(pk.get('bytes'))} · made {kit.when(pk.get('created_at'))}"))

        def fetch(x=pk) -> bytes:
            return api.http.get(x["download"], headers=api._h()).content

        c[1].download_button("Download", fetch, file_name=f"{ws}-{pk['id']}.lystpack", on_click="ignore",
                             icon=":material/download:", key=f"pk_dl_{pk['id']}", width="stretch")
        if i < min(len(packs), 5) - 1:
            st.divider()
    with st.expander("Bring a pack into this installation", icon=":material/move_to_inbox:"):
        bring()


def bring() -> None:
    """Installation administrators: check a pack (nothing changes), then import it as a new workspace."""
    api, _ = _ctx()
    if (done := st.session_state.pop("pk_done", None)):
        st.success(md(done), icon=":material/check:")
    f = st.file_uploader("Pack", type=["lystpack"], key="pk_file")
    pw = st.text_input("Its passphrase", type="password", key="pk_in_pw")
    if not f:
        st.caption("Only installation administrators can bring a workspace in.")
        return
    if st.button("Check it", icon=":material/fact_check:", key="pk_check"):
        st.session_state["pk_checked"] = kit.guarded(lambda: api.post("/v1/imports/inspect", None, data={"passphrase": pw},
                                                                      files={"file": (f.name, f.getvalue())}))
    rep = st.session_state.get("pk_checked")
    if not rep:
        return
    comp = rep.get("compatibility") or {}
    mem = rep.get("members") or {}
    st.markdown(md(f"**{rep.get('name')}** ({rep.get('workspace')}) · exported {kit.when(rep.get('exported_at'))} · "
                   f"{rep.get('tables')} tables, {rep.get('rows', 0):,} rows"))
    kit.chip("Intact" if rep.get("intact") else "Damaged", "ok" if rep.get("intact") else "bad")
    kit.chip("Compatible" if comp.get("compatible") else "Made by a newer Lyst", "ok" if comp.get("compatible") else "bad")
    if rep.get("problems"):
        st.error(md("; ".join(map(str, rep["problems"]))))
    st.caption(md(f"Members found here: {len(mem.get('mapped') or [])}; not found: "
                  + (", ".join(m.get("email", str(m)) for m in mem.get("not_found") or []) or "none")))
    if not (rep.get("intact") and comp.get("compatible")):
        return
    c = st.columns(2)
    base = re.sub(r"[^a-z0-9_]+", "_", str(rep.get("workspace") or "workspace").lower())
    wid = c[0].text_input("New workspace id", value=f"{base}_copy" if rep.get("workspace_exists") else base, key="pk_wid")
    name = c[1].text_input("Name", value=str(rep.get("name") or ""), key="pk_name")
    run = st.checkbox("Run its schedules and monitors here", key="pk_run",
                      help="Off by default, so a copy doesn't send the same reports twice.")
    if st.button("Bring it in", type="primary", icon=":material/move_to_inbox:", key="pk_go", disabled=not wid):
        out = kit.guarded(lambda: api.post("/v1/imports", None, files={"file": (f.name, f.getvalue())},
                                           data={"passphrase": pw, "workspace_id": wid, "name": name,
                                                 "activate": str(run).lower()}))
        if out:
            st.session_state.pop("pk_checked", None)
            missing = (out.get("members") or {}).get("not_found") or []
            st.session_state["pk_done"] = (
                f"Brought in {out.get('tables')} tables and {out.get('rows', 0):,} rows as “{name or wid}”; switch to it "
                "from the sidebar." + (f" Paused: {', '.join(out['paused'])}." if out.get("paused") else "")
                + (f" Members not found: {', '.join(missing)}." if missing else ""))
            st.rerun()
