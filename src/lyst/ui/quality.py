# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data quality review, anywhere a dataset is: its flagged rows in one styled table (the flagged cell lit in its row,
what Lyst would do beside it), a person's call per row (accept as valid, fix, exclude), "Let Lyst resolve" to stage
Lyst's proposed fixes, and the staged changes as a diff that is applied only after an approval.

:func:`review` draws it in place (Data's Quality view, an answer that profiled a dataset); :func:`sheet` opens it as a
dialog (Data's More menu, the dataset actions anywhere). The drawing is a ``st.components.v2`` component
(``quality_component``); every change goes through the API (``/datasets/{name}/quality``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from lyst.ui.client import APIError, client, workspace

_COMPONENT = None


def _component():
    global _COMPONENT
    if _COMPONENT is None:
        from streamlit.components.v2 import component  # noqa: PLC0415

        here = Path(__file__).resolve().parent / "quality_component"
        _COMPONENT = component("lyst_quality", css=(here / "quality.css").read_text(encoding="utf-8"),
                               js=(here / "quality.js").read_text(encoding="utf-8"))
    return _COMPONENT


def _role() -> str:
    return str(st.session_state.get("role") or "")


def can_edit() -> bool:
    return _role() in ("editor", "admin", "owner")


def can_approve() -> bool:
    return _role() in ("admin", "owner")


def report(dataset: str) -> dict[str, Any] | None:
    try:
        return client().get(f"/v1/workspaces/{workspace()}/datasets/{dataset}/quality")
    except APIError as e:
        st.caption(f":material/info: {e}")
        return None


def summary(r: dict[str, Any]) -> str:
    """One sentence for a screen reader and a caption: what is flagged and what is waiting."""
    if not r.get("total"):
        return f"No flagged rows in {r['title']}."
    parts = [f"{r['open']} of {r['total']} flagged rows need review"]
    if r.get("staged"):
        parts.append(f"{len(r['staged'])} change{'s' * (len(r['staged']) != 1)} staged")
    if r.get("pending"):
        parts.append("a resolution is awaiting approval")
    return "; ".join(parts) + "."


SAY = {"accept": "Accepted as valid", "exclude": "Staged: exclude", "fix": "Staged: fix", "reopen": "Back to review"}


def _handle(dataset: str, ev: dict[str, Any], *, in_dialog: bool = False) -> None:
    api, ws = client(), workspace()
    base = f"/v1/workspaces/{ws}/datasets/{dataset}/quality"
    kind = ev.get("type")
    try:
        if kind == "review":
            api.post(f"{base}/review", {"issue_ids": list(ev.get("ids") or []), "decision": ev.get("decision"),
                                        "value": ev.get("value")})
            n = len(ev.get("ids") or [])
            st.toast(f"{SAY.get(str(ev.get('decision')), 'Saved')}{f' ({n} rows)' if n > 1 else ''}", icon=":material/check:")
        elif kind == "propose":
            r = api.post(f"{base}/propose", {"issue_ids": ev.get("ids")})
            n = r.get("proposed", 0)
            st.toast(f"Lyst proposed {n} fix{'es' * (n != 1)}. Review them, then apply." if n else
                     "Lyst has no fix to propose for these; they need a person.", icon=":material/auto_awesome:")
        elif kind == "apply":
            res = api.post(f"{base}/apply", {})
            if ev.get("approve") and res.get("approval_id"):
                api.post(f"/v1/approvals/{res['approval_id']}", {"approve": True})
                st.toast("Approved and applied as a new version. The earlier version is kept.", icon=":material/verified:")
            else:
                st.toast("Sent for approval. Nothing changes until an admin approves it.", icon=":material/schedule:")
        elif kind == "decide":
            api.post(f"/v1/approvals/{ev.get('approval')}", {"approve": bool(ev.get("approve"))})
            st.toast("Approved and applied." if ev.get("approve") else "Rejected; the changes are staged again.",
                     icon=":material/verified:" if ev.get("approve") else ":material/close:")
        elif kind == "undo":
            r = api.post(f"/v1/workspaces/{ws}/quality/resolutions/{ev.get('id')}/undo", {})
            st.toast(f"Undone: the earlier data is back as version {r.get('version')}.", icon=":material/undo:")
        else:
            return
    except APIError as e:
        st.session_state[f"_q_err_{dataset}"] = str(e)
    for k in ("_waiting", "_home", "_agent", "_zone"):  # what waits on this person may have changed
        st.session_state.pop(k, None)
    if in_dialog:
        st.rerun(scope="fragment")
    st.rerun()


def review(dataset: str, *, key: str, compact: bool = False, in_dialog: bool = False) -> dict[str, Any] | None:
    """The flagged rows of ``dataset`` for a person to review and resolve, drawn in place. Returns the report."""
    r = report(dataset)
    if r is None:
        return None
    if err := st.session_state.pop(f"_q_err_{dataset}", None):
        st.error(err, icon=":material/error:")
    data = {**r, "canEdit": can_edit(), "canApprove": can_approve(), "compact": compact, "say": summary(r)}
    res = _component()(data=data, key=f"lyst_quality_{key}", on_action_change=lambda: None)
    ev = getattr(res, "action", None)
    seen = f"_q_nonce_{key}"
    if isinstance(ev, dict) and ev.get("nonce") != st.session_state.get(seen):
        st.session_state[seen] = ev.get("nonce")
        _handle(dataset, ev, in_dialog=in_dialog)
    return r


@st.dialog("Review data quality", width="large")
def sheet(dataset: str) -> None:
    """The review as a sheet, for a dataset picked from anywhere (Data's More menu, a dataset's actions)."""
    review(dataset, key=f"sheet_{dataset}", in_dialog=True)


def badge(r: dict[str, Any]) -> str:
    """A short status for a button or a chip: '3 to review', '2 staged', 'clean'."""
    if r.get("open"):
        return f"{r['open']} to review"
    if r.get("staged"):
        return f"{len(r['staged'])} staged"
    if r.get("pending"):
        return "awaiting approval"
    return "clean"

