# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Questions about an answer (how it was figured, where it came from, what could change it, can it be repeated),
how sensitive each dataset is, and what each level may be used for. Reads the session on every call."""

from __future__ import annotations

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

ASK = {"how": "How was this figured?", "sources": "Where is it from?", "change": "What could change it?",
       "reproduce": "Can it be repeated?"}
LEVELS = {"public": "Public", "internal": "Internal", "confidential": "Confidential", "restricted": "Restricted"}
ROLES = {"viewer": "Everyone", "editor": "Editors and above", "admin": "Admins and above", "owner": "Owners only"}


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("admin", "owner")


def _lines(detail) -> list[str]:
    """Readable lines from an explanation's detail, whatever its shape."""
    if isinstance(detail, str):
        return [detail]
    if isinstance(detail, list):
        out = []
        for d in detail:
            if isinstance(d, str):
                out.append(d)
            elif isinstance(d, dict):
                if d.get("figure"):
                    steps = [s.get("description") or s.get("capability") for s in d.get("steps") or []]
                    out.append(f"{d['figure']}: " + "; ".join(x for x in steps if x))
                elif d.get("query"):
                    out.append(f"{d['query']}: {d.get('outcome')} ({d.get('rows_then')} rows then, {d.get('rows_now')} now)")
                elif d.get("capability"):
                    out.append(f"{d['capability'].replace('_', ' ')}: {d.get('why') or ''}")
        return out
    if isinstance(detail, dict):
        ds = detail.get("datasets") or []
        out = [f"{d.get('label')}: {d.get('rows', 0):,} rows, loaded {str(d.get('loaded_at'))[:16]}" for d in ds]
        cur = detail.get("currency") or {}
        if cur.get("changed"):
            out.append("Changed since: " + "; ".join(map(str, cur["changed"])))
        out += [f"Warning: {w}" for w in detail.get("warnings") or []]
        return out
    return []


@st.dialog("About this answer", width="large")
def explain(task: str, q: str) -> None:
    api, _, _ = _ctx()
    pick = st.segmented_control("Question", list(ASK), default=q, format_func=ASK.get, label_visibility="collapsed",
                                key=f"ex_q_{task}") or q
    r = kit.guarded(lambda: api.get(f"/v1/tasks/{task}/explain", q=pick))
    if not r:
        return
    st.markdown(md(r.get("short") or ""))
    lines = _lines(r.get("detail"))
    if lines:
        with st.expander("Details", icon=":material/list:", expanded=pick in ("change", "sources")):
            st.markdown("\n".join(f"- {md(x)}" for x in lines[:20]))


def ask_row(task: str) -> None:
    """Under an answer: the questions people ask about a number before they repeat it."""
    pick = st.pills("Ask about this answer", list(ASK), format_func=ASK.get, label_visibility="collapsed",
                    key=f"ex_{task}")
    if pick:
        st.session_state.pop(f"ex_{task}", None)
        explain(task, pick)


def classification(name: str, current: str | None) -> None:
    """Dataset Overview: how sensitive it is and what that allows; admins change it with a reason."""
    api, ws, admin = _ctx()
    try:
        pol = (api.get(f"/v1/workspaces/{ws}/data-policy").get("spec")) or {}
    except APIError:
        pol = {}
    lvl = (current or "internal").lower()
    who = ROLES.get((pol.get("read_min_role") or {}).get(lvl, "viewer"), "Everyone")
    rank = list(LEVELS)
    ext = rank.index(lvl) <= rank.index(pol.get("external_model_max", "confidential")) if lvl in rank else True
    exp = rank.index(lvl) <= rank.index(pol.get("export_max", "confidential")) if lvl in rank else True
    kit.label("Sensitivity")
    st.caption(md(f"{LEVELS.get(lvl, lvl)}: readable by {who.lower()}; "
                  + ("may" if ext else "may not") + " be sent to an external model; "
                  + ("may" if exp else "may not") + " be exported."))
    if admin:
        with st.popover("Change sensitivity", icon=":material/shield:"):
            new = st.selectbox("Level", list(LEVELS), index=rank.index(lvl) if lvl in rank else 1, format_func=LEVELS.get,
                               key=f"cl_{name}")
            why = st.text_input("Reason", placeholder="Contains salary data", key=f"clr_{name}")
            if st.button("Save", type="primary", key=f"cls_{name}", disabled=not why.strip() or new == lvl):
                kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/datasets/{name}/classification",
                                            {"level": new, "tags": [], "reason": why.strip()}), "Saved")


def policy_summary() -> str:
    """The data policy in one sentence: who reads restricted and confidential data, and what may leave."""
    api, ws, _admin = _ctx()
    try:
        spec = (api.get(f"/v1/workspaces/{ws}/data-policy").get("spec") or {})
    except APIError:
        return ""
    reads = spec.get("read_min_role") or {}
    return (f"Restricted data: {ROLES.get(reads.get('restricted', 'admin'), '').lower()}. Confidential data: "
            f"{ROLES.get(reads.get('confidential', 'viewer'), '').lower()}. Models receive data up to "
            f"{LEVELS.get(spec.get('external_model_max', 'confidential'), '').lower()}; exports up to "
            f"{LEVELS.get(spec.get('export_max', 'confidential'), '').lower()}.")


def policy_section() -> None:
    """Settings › Workspace: who may read each sensitivity level, and what may leave the workspace."""
    api, ws, admin = _ctx()
    try:
        p = api.get(f"/v1/workspaces/{ws}/data-policy")
    except APIError:
        return
    spec = p.get("spec") or {}
    kit.label("Data policy")
    if not admin:
        st.caption(md("; ".join(f"{LEVELS[k]} data: {ROLES.get(v, v).lower()}" for k, v in (spec.get("read_min_role") or {}).items()
                                if k in LEVELS) + "."))
        return
    reads = {}
    cols = st.columns(len(LEVELS))
    for col, (k, label) in zip(cols, LEVELS.items(), strict=True):
        cur = (spec.get("read_min_role") or {}).get(k, "viewer")
        reads[k] = col.selectbox(f"{label} data is read by", list(ROLES), index=list(ROLES).index(cur), format_func=ROLES.get,
                                 key=f"dp_{k}")
    c = st.columns(2)
    ext = c[0].selectbox("Send to an external model data up to", list(LEVELS), format_func=LEVELS.get,
                         index=list(LEVELS).index(spec.get("external_model_max", "confidential")), key="dp_ext")
    exp = c[1].selectbox("Allow exports of data up to", list(LEVELS), format_func=LEVELS.get,
                         index=list(LEVELS).index(spec.get("export_max", "confidential")), key="dp_exp")
    new = {"read_min_role": reads, "external_model_max": ext, "export_max": exp}
    if new != {k: spec.get(k) for k in new} and st.button("Save policy", type="primary", key="dp_save"):
        kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/data-policy", new), "Policy saved")
