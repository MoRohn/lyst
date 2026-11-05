# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Contextual actions and truthful progress: the autonomy layer as it appears on every object.

* :func:`menu` shows what can be done with one object (a dataset, metric, task, saved analysis or asset), straight from
  ``GET /v1/workspaces/{ws}/objects/{kind}/{id}/actions``. The server decides what is offered and re-checks it when
  it runs (``POST .../actions/{id}``), so the menu never offers an action the person may not take and the page holds
  no agent logic of its own. Actions that need approval say so before they are clicked; what comes back (a new task,
  an approval request, a schedule, a draft, an artifact, a place to go) is shown as such.
* :func:`state` is a task's lifecycle state from the engine (Preparing, Investigating, Executing, Verifying, Awaiting
  Approval, Completed, Needs Attention) with its detail: what is actually happening, never a percentage.
* :func:`timeline` draws the lifecycle transitions recorded for a task.

Without the actions API (an older engine) the menu simply does not appear.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qsl, urlparse

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client

TTL = 20  # seconds an object's action list is reused within a session; any action drops it
KIND_ICON = {"navigate": ":material/arrow_outward:", "ask": ":material/auto_awesome:", "run": ":material/play_arrow:",
             "approve": ":material/approval:"}
PREFIX_ICON = {"profile": ":material/fact_check:", "impact": ":material/account_tree:", "repair": ":material/build:",
               "contract": ":material/handshake:", "lineage": ":material/route:", "value": ":material/functions:",
               "explain": ":material/psychology_alt:", "trend": ":material/show_chart:", "forecast": ":material/timeline:",
               "what_if": ":material/tune:", "pause": ":material/pause:", "cancel": ":material/block:",
               "resume": ":material/play_arrow:", "retry": ":material/refresh:", "breakdown": ":material/stacked_bar_chart:",
               "compare_year": ":material/compare_arrows:", "why": ":material/psychology_alt:",
               "dashboard": ":material/dashboard_customize:", "schedule": ":material/event_repeat:",
               "briefing": ":material/article:", "versions": ":material/history:", "run": ":material/play_arrow:",
               "open": ":material/open_in_full:"}
STATE_TONE = {"Preparing": "run", "Investigating": "run", "Executing": "run", "Verifying": "run",
              "Awaiting Approval": "wait", "Completed": "verified", "Needs Attention": "warn"}
PAGE = {"": "app_pages/analyst.py", "work": "app_pages/work.py", "data": "app_pages/data.py",
        "brain": "app_pages/brain.py", "settings": "app_pages/settings.py", "artifact": "app_pages/artifact.py"}


def _icon(action: dict) -> str:
    name = action["id"].split(".", 1)[-1]
    return PREFIX_ICON.get(name, PREFIX_ICON.get(name.split(".")[0], KIND_ICON.get(action["kind"], ":material/bolt:")))


def available(kind: str, oid: str) -> list[dict] | None:
    """The actions the engine offers on this object now, or None when the engine has no actions API."""
    ws = st.session_state.workspace
    memo = st.session_state.setdefault("_actions", {})
    hit = memo.get((ws, kind, oid))
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    try:
        acts = client().get(f"/v1/workspaces/{ws}/objects/{kind}/{oid}/actions")
    except APIError as e:
        if e.status in (404, 405):
            return None
        acts = []
    if len(memo) > 100:
        memo.clear()
    memo[(ws, kind, oid)] = (time.monotonic(), acts)
    return acts


def go(href: str) -> None:
    """Follow a server-given in-app link (``/data?dataset=pnl``) with the page's own navigation."""
    u = urlparse(href)
    page = PAGE.get(u.path.strip("/"), "app_pages/analyst.py")
    qp = dict(parse_qsl(u.query))
    if page.endswith("artifact.py") and "asset" in qp:
        qp = {"kind": "asset", "id": qp["asset"]} | ({"view": qp["view"]} if "view" in qp else {})
    if "task" in qp:
        st.session_state.task_id = qp["task"]
    st.switch_page(page, query_params=qp)


def run(kind: str, oid: str, action: dict, *, page: str, objective: str | None = None) -> None:
    """Take an action through the engine (which re-checks it) and show what it produced."""
    if action["kind"] == "navigate" and action.get("href"):
        go(action["href"])
        return
    ws = st.session_state.workspace
    out = kit.guarded(lambda: client().post(f"/v1/workspaces/{ws}/objects/{kind}/{oid}/actions/{action['id']}",
                                            {"page": page, "objective": objective}))
    st.session_state.pop("_actions", None)
    if not out:
        return
    if out.get("approval_id"):
        st.toast("Sent for approval. It runs once an admin approves it.", icon=":material/approval:")
    elif out.get("schedule_id"):
        st.toast("Proposed. It starts once an admin approves it.", icon=":material/event_repeat:")
    elif out.get("draft"):
        st.toast("Drafted. Nothing changes until it is reviewed.", icon=":material/edit_note:")
    if out.get("task_id"):
        st.session_state.task_id = out["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})
    elif out.get("navigate"):
        go(out["navigate"])
    elif out.get("artifact"):
        from lyst.ui import artifacts  # noqa: PLC0415

        art = out["artifact"]
        artifacts.open_artifact({"kind": art.get("kind", "briefing"), "id": art.get("asset_id") or art.get("id")})
    elif out.get("affected") is not None:
        n = len(out["affected"])
        st.toast(f"{n} thing{'s' * (n != 1)} depend on it" if n else "Nothing approved depends on it",
                 icon=":material/account_tree:")


def menu(kind: str, oid: str, *, page: str, label: str = "Actions", objective: str | None = None,
         exclude: tuple[str, ...] = ()) -> None:
    """The object's actions in one popover: things to ask, things to run, things that need approval, places to go."""
    acts = available(kind, oid)
    if not acts:
        return
    acts = [a for a in acts if a["id"] not in exclude]
    if not acts:
        return
    with st.popover(label, icon=":material/more_horiz:", help="What Lyst can do with this"):
        groups = (("Ask Lyst", ("ask",)), ("Do", ("run", "approve")), ("Go to", ("navigate",)))
        for title, kinds in groups:
            items = [a for a in acts if a["kind"] in kinds]
            if not items:
                continue
            kit.label(title)
            for a in items:
                text = a["label"] + (" · needs approval" if a.get("needs_approval") else "")
                if st.button(text, key=f"act_{kind}_{oid}_{a['id']}", icon=_icon(a), type="tertiary", width="stretch",
                             help=a.get("reason")):
                    run(kind, oid, a, page=page, objective=objective)


def state(task: dict, where: Any = st) -> None:
    """A task's lifecycle state chip, with its detail as the tooltip: what the engine says is happening."""
    lc = task.get("lifecycle") or {}
    if not lc.get("state"):
        return
    kit.chip(lc["state"], STATE_TONE.get(lc["state"], "info"), where)


def timeline(task_id: str) -> None:
    """The lifecycle transitions recorded for a task, oldest first."""
    from lyst.ui import visuals  # noqa: PLC0415

    try:
        lc = client().get(f"/v1/tasks/{task_id}/lifecycle")
    except APIError:
        return
    marks = {"Completed": "done", "Needs Attention": "failed", "Awaiting Approval": "pending"}
    items = [{"capability": e["state"], "agent": f"{kit.when(e.get('at'))} · {e.get('detail') or e.get('event', '')}",
              "status": marks.get(e["state"], "running" if i == len(lc.get("timeline", [])) - 1 else "done")}
             for i, e in enumerate(lc.get("timeline") or [])]
    if items:
        st.html(visuals.steps(items, label="How it progressed", status_text=False))
