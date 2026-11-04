# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Source of truth for a dataset, and webhooks that tell other systems what happened. Reads the session per call."""

from __future__ import annotations

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

AUTHORITY = {None: "Not set", "authoritative": "Source of truth", "secondary": "A secondary copy"}
EVENTS = {"*": "Everything", "task.completed": "An answer is ready", "task.failed": "A task failed",
          "task.awaiting_approval": "Something waits for approval", "dataset.*": "Data changed", "contract.*": "Data contracts",
          "asset.*": "Reports and dashboards", "decision.*": "Decisions", "metric.*": "Definitions"}


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("admin", "owner")


def authority(name: str, current: dict | None) -> None:
    """Dataset Overview: whether this table is the source of truth for something, so answers prefer it."""
    api, ws, admin = _ctx()
    cur = (current or {}).get("level")
    subject = (current or {}).get("subject") or ""
    kit.label("Source of truth")
    st.caption(md({"authoritative": f"This is the source of truth{f' for {subject}' if subject else ''}: when two "
                                    "definitions compute the same number, answers use this one and say so.",
                   "secondary": f"A secondary copy{f' of {subject}' if subject else ''}: answers prefer the source of truth "
                                "when there is one."}.get(cur, "Not marked. Mark it when two tables hold the same numbers.")
                  + (f" Note: {current['note']}" if (current or {}).get("note") else "")))
    if not admin:
        return
    with st.popover("Change", icon=":material/verified:"):
        lvl = st.radio("This table is", list(AUTHORITY), format_func=AUTHORITY.get,
                       index=list(AUTHORITY).index(cur) if cur in AUTHORITY else 0, key=f"au_{name}")
        subj = st.text_input("For (what it is the source of)", value=subject, placeholder="revenue", key=f"aus_{name}")
        note = st.text_input("Note", value=(current or {}).get("note") or "", key=f"aun_{name}")
        if st.button("Save", type="primary", key=f"au_go_{name}"):
            kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/datasets/{name}/authority",
                                        {"level": lvl, "subject": subj, "note": note}), "Saved")


def source_authority() -> dict:
    api, ws, _ = _ctx()
    try:
        return api.get(f"/v1/workspaces/{ws}/source-authority")
    except APIError:
        return {}


def webhooks_section() -> None:
    """Settings › Workspace (admins): send what happens in Lyst to another system, signed, with a delivery log."""
    api, ws, admin = _ctx()
    if not admin:
        return
    try:
        hooks = api.get(f"/v1/workspaces/{ws}/webhooks")
    except APIError:
        return
    kit.label(f"Webhooks · {len(hooks)}" if hooks else "Webhooks")
    if (secret := st.session_state.pop("_new_hook_secret", None)):
        st.warning("Copy this signing secret into the receiving system now. It is shown only once.", icon=":material/key:")
        st.code(secret, language=None)
    if not hooks:
        st.caption("Lyst can POST a signed message to another system when an answer is ready, data changes or something "
                   "waits for approval.")
    for i, h in enumerate(hooks):
        state = ("Active", "ok") if h.get("active") else ("Paused", "off")
        if h.get("disabled_reason"):
            state = ("Stopped", "bad")
        meta = " · ".join(x for x in (", ".join(EVENTS.get(t, t) for t in h.get("types") or []),
                                      h.get("description"),
                                      f"{h['consecutive_failures']} failures in a row" if h.get("consecutive_failures") else None,
                                      h.get("disabled_reason")) if x)
        acts = [("Test", "ping"), ("Pause" if h.get("active") else "Resume", "toggle"), ("Log", "log")]
        hit = kit.row(h["url"], meta, status=state, actions=acts, key=f"wh{h['id']}", divider=i < len(hooks) - 1)
        if hit == "ping":
            r = kit.guarded(lambda x=h: api.post(f"/v1/workspaces/{ws}/webhooks/{x['id']}/ping"))
            if r:
                (st.toast if r.get("ok") else st.error)(f"Delivered (HTTP {r.get('status')})" if r.get("ok")
                                                        else f"Not delivered: {r.get('error') or r.get('status')}")
        elif hit == "toggle":
            kit.guarded(lambda x=h: api.patch(f"/v1/workspaces/{ws}/webhooks/{x['id']}", {"active": not x.get("active")}),
                        "Saved")
            st.rerun()
        elif hit == "log":
            deliveries(h)
    with st.popover("Add a webhook", icon=":material/webhook:"):
        url = st.text_input("Send to (https)", placeholder="https://hooks.example.com/lyst", key="wh_url")
        picks = st.multiselect("When", list(EVENTS), default=["task.completed"], format_func=EVENTS.get, key="wh_types")
        desc = st.text_input("Description (optional)", key="wh_desc")
        if st.button("Add", type="primary", key="wh_add", disabled=not url.startswith("https://")):
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/webhooks",
                                             {"url": url, "types": picks or ["*"], "description": desc or None}))
            if r:
                st.session_state["_new_hook_secret"] = r.get("secret")
                st.rerun()


@st.dialog("Delivery log", width="large")
def deliveries(h: dict) -> None:
    api, ws, _ = _ctx()
    st.caption(md(h["url"]))
    rows = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/webhooks/{h['id']}/deliveries")) or []
    if not rows:
        st.caption("Nothing sent yet.")
    for i, d in enumerate(rows[:30]):
        ok = d.get("status") in ("delivered", "ok", "sent")
        hit = kit.row(EVENTS.get(d.get("type"), d.get("type") or "event"),
                      " · ".join(str(x) for x in (kit.when(d.get("delivered_at") or d.get("created_at")),
                                                  f"{d['attempts']} attempts" if (d.get("attempts") or 0) > 1 else None,
                                                  f"HTTP {d['last_status']}" if d.get("last_status") else None,
                                                  d.get("last_error")) if x),
                      status=("Delivered", "ok") if ok else (str(d.get("status") or "Failed").capitalize(), "warn"),
                      actions=[] if ok else [("Send again", "re")], key=f"whd{i}", divider=i < min(len(rows), 30) - 1)
        if hit == "re":
            kit.guarded(lambda x=d: api.post(f"/v1/workspaces/{ws}/webhook-deliveries/{x['id']}/redeliver"), "Queued again")
    if st.button("Remove this webhook", type="tertiary", icon=":material/delete:", key=f"whdel_{h['id']}"):
        if kit.guarded(lambda: api.delete(f"/v1/workspaces/{ws}/webhooks/{h['id']}"), "Removed"):
            st.rerun()
