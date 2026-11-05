# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Work Lyst gets ready before it is needed: what is coming up, what is prepared for you, why, and how much Lyst
may do on its own. Used by Work, Home and Settings; reads the session on every call."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

VERDICT = {"ready": ("Ready", "ok"), "blocked": ("Blocked", "bad"), "warn": ("Check first", "warn"),
           "risk": ("At risk", "bad"), "watch": ("Keep an eye on it", "warn"),
           "warning": ("Check first", "warn"), "stale": ("Out of date", "warn")}
ACTION = {"run_now": "Run now", "dismiss": "Dismiss", "snooze": "Later", "watch": "Watch it", "schedule": "Repeat it",
          "renew": "Renew", "open_source": "Open source", "open_dataset": "Open dataset", "test_connection": "Test it",
          "propose_fix": "Propose a fix", "request_registration": "Ask to add it"}
PRIMARY = ("run_now", "request_registration", "renew", "open_source", "open_dataset", "test_connection", "propose_fix", "watch", "schedule", "snooze")
NEXT = "next_best_action"  # shown under the answer it came from, not in Work
LEVELS = {"off": "Off", "suggest": "Suggest", "prepare": "Prepare", "investigate": "Investigate", "automate": "Automate"}
LEVEL_TEXT = {"off": "Lyst only does what it is asked.",
              "suggest": "Lyst notices what is coming up and suggests it; it prepares nothing.",
              "prepare": "Lyst checks the data and definitions an upcoming run needs and tells you early if one is "
                         "blocked. It runs nothing on its own.",
              "investigate": "Lyst also investigates unusual changes before you ask, within its daily budget.",
              "automate": "Lyst also refreshes an input that is behind before a scheduled run, within its budget."}


def _ctx():
    return client(), workspace()


def items(status: str = "open", **params) -> list[dict]:
    api, ws = _ctx()
    try:
        return api.get(f"/v1/workspaces/{ws}/anticipations", status=status, **params)
    except APIError:
        return []


def _wants_person(a: dict) -> bool:
    if a.get("kind") == NEXT:
        return False
    if a.get("kind") == "early_warning":
        return a.get("verdict") == "verified"
    if a.get("kind") == "health_foresight":
        return a.get("verdict") == "risk"
    if a.get("kind") == "capability_gap":
        return a.get("status") == "surfaced"
    return a.get("status") == "surfaced" or a.get("verdict") not in (None, "ready")


def _act(a: dict, action: str) -> None:
    api, ws = _ctx()
    body: dict = {"action": action}
    if action == "snooze":
        body["until"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/anticipations/{a['id']}/act", body),
                    {"dismiss": "Dismissed", "snooze": "Hidden until tomorrow", "useful": "Thanks, noted",
                     "not_useful": "Thanks, Lyst will suggest less like this", "incorrect": "Thanks, noted as wrong"}
                    .get(action))
    if r and r.get("open") is not None:  # a link: go where the fix is made
        if r.get("credential_id") and action == "renew":
            st.switch_page("app_pages/settings.py", query_params={"view": "connections"})
        elif r.get("dataset"):
            st.switch_page("app_pages/data.py", query_params={"dataset": r["dataset"]})
        elif r.get("connection_id"):
            st.switch_page("app_pages/data.py", query_params={"connection": r["connection_id"]})
    elif r and r.get("needs_approval"):
        st.toast("Proposed. It starts once an admin approves it.", icon=":material/approval:")
        st.rerun()
    elif r and r.get("task_id"):
        st.session_state.task_id = r["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": r["task_id"]})
    elif r:
        st.rerun()


@st.dialog("Why Lyst prepared this", width="large")
def why(a: dict) -> None:
    api, ws = _ctx()
    w = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/anticipations/{a['id']}/why")) or {}
    full = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/anticipations/{a['id']}")) or a
    st.markdown(md(f"**{a.get('objective')}**"))
    if w.get("short"):
        st.markdown(md(w["short"]))
    detail = w.get("detail") or []
    rest = detail[1:] if detail and detail[0] == w.get("short") else detail
    checks = ((full.get("verification") or {}).get("checks")) or []
    if rest and not checks:  # the checks below say the same thing, with a status each
        st.markdown("\n".join(f"- {md(x)}" for x in (rest if isinstance(rest, list) else [rest])))
    if checks:
        kit.label("What Lyst checked")
        for i, c in enumerate(checks):
            tone = {"ok": ("OK", "ok"), "warn": ("Check", "warn"), "fail": ("Blocked", "bad")}.get(c.get("status"), (c.get("status"), "info"))
            kit.row(c["check"].replace(":", " · ").replace("_", " ").capitalize(), c.get("detail") or "", status=tone,
                    key=f"awc{i}", divider=i < len(checks) - 1)
    if w.get("methods"):
        st.caption(md(f"How: {w['methods']}."))
    if st.button("Dismiss it", type="tertiary", icon=":material/close:", key=f"aw_dismiss_{a['id']}"):
        _act(a, "dismiss")
    kit.label("Was this worth preparing?")
    b = st.columns(3)
    for col, (aid, text) in zip(b, (("useful", "Useful"), ("not_useful", "Not useful"), ("incorrect", "Wrong")), strict=True):
        if col.button(text, key=f"fb_{aid}_{a['id']}", type="tertiary", width="stretch"):
            _act(a, aid)


def rows(found: list[dict], *, key: str) -> None:
    for i, a in enumerate(found):
        status = VERDICT.get(a.get("verdict"))
        meta = " · ".join(x for x in ((a.get("reasons") or [None])[0],
                                      f"needed {kit.when(a['needed_at'])}" if a.get("needed_at") else None) if x)
        ids = [x["id"] if isinstance(x, dict) else x for x in a.get("actions") or []]
        acts = [(ACTION[x], x) for x in PRIMARY if x in ids][:2] + [("Why", "why")]
        hit = kit.row(a.get("objective") or a.get("kind", "").replace("_", " "), meta, status=status, actions=acts,
                      key=f"{key}{a['id']}", divider=i < len(found) - 1)
        if hit == "why":
            why(a)
        elif hit:
            _act(a, hit)


def about(subject_kind: str, subject_id: str, *, key: str) -> None:
    """Inside a source or dataset: what Lyst foresees for it (an expiring credential, a schema change, a source
    that keeps failing), with the fix one click away."""
    found = [a for a in items() if a.get("kind") == "health_foresight" and a.get("subject_kind") == subject_kind
             and str(a.get("subject_id")) == str(subject_id)]
    if found:
        kit.label("Lyst sees ahead")
        rows(found, key=key)


def health_by_subject() -> dict[tuple[str, str], dict]:
    return {(a.get("subject_kind"), str(a.get("subject_id"))): a for a in items() if a.get("kind") == "health_foresight"}


def coming_up() -> None:
    """Work › Recurring: what Lyst sees coming and has checked or prepared."""
    found = [a for a in items() if a.get("kind") not in (NEXT, "capability_gap") and not _wants_person(a)]
    if not found:
        return
    kit.label(f"Coming up · {len(found)}")
    rows(found, key="cu")


def prepared_for_you() -> list[dict]:
    """Work › Needs you: only what wants a person (blocked, needing a look, a verified early warning); ready work and
    unverified warnings wait in Coming up."""
    return [a for a in items() if _wants_person(a)]


def next_steps(task_id: str) -> None:
    """Under an answer: what Lyst suggests doing next with it (a follow-up question, watching, repeating)."""
    found = items(subject=task_id)
    found = [a for a in found if a.get("kind") == NEXT]
    if not found:
        return
    kit.label("Next steps")
    rows(found[:3], key=f"nx{task_id}")


def home_line() -> None:
    blocked = [a for a in items() if a.get("verdict") == "blocked" and a.get("kind") != NEXT]
    if blocked:
        a = blocked[0]
        c = st.columns([6, 1.2], vertical_alignment="center")
        c[0].warning(md(f"{a.get('objective')}: {(a.get('reasons') or ['it cannot run as things stand'])[0]}"),
                     icon=":material/event_busy:")
        if c[1].button("See why", type="tertiary", key="home_blocked", width="stretch"):
            why(a)


def level_control() -> None:
    """Settings › Workspace: how much Lyst may do before it is asked, in plain words, and today's use."""
    api, ws = _ctx()
    try:
        s = api.get(f"/v1/workspaces/{ws}/anticipation/settings")
    except APIError:
        return
    kit.label("Working ahead")
    cur = s.get("level_name") or "suggest"
    pick = st.segmented_control("Working ahead", list(LEVELS), default=cur, format_func=LEVELS.get,
                                label_visibility="collapsed", key="anticipation_level")
    st.caption(LEVEL_TEXT.get(pick or cur, "") + " " + md(s.get("methods") or "").capitalize()
               + ("" if s.get("external_services") else ". Nothing leaves this installation."))
    if pick and pick != cur:
        kit.guarded(lambda: api.patch(f"/v1/workspaces/{ws}/anticipation/settings", {"level": pick}), "Saved")
        st.rerun()
    use = s.get("usage_today") or {}
    if use:
        def one(kind: str, v) -> str:
            if not isinstance(v, dict):
                return f"{kind.replace('_', ' ')} {v}"
            limit = s.get(f"daily_{kind}_limit")
            return (f"{v.get('count', 0)} {kind.replace('_', ' ')}{'d' if kind == 'prepare' else ''}"
                    + (f" of {limit} a day" if limit else "") + (f", {v['refused']} refused" if v.get("refused") else ""))
        st.caption("Today: " + "; ".join(one(k, v) for k, v in use.items()) + ".")


def _pct(v) -> str:
    return "not enough yet" if v is None else f"{v * 100:.0f}%"


def quality_block() -> None:
    """Settings › Advanced: how well working ahead has served this workspace, from what actually happened."""
    api, ws = _ctx()
    try:
        q = api.get(f"/v1/workspaces/{ws}/anticipation/quality", days=30)
    except APIError:
        return
    rel, tim, acc = q.get("relevance") or {}, q.get("timeliness") or {}, q.get("accuracy") or {}
    eff, att, gov = q.get("efficiency") or {}, q.get("attention") or {}, q.get("governance") or {}
    kit.label(f"Working ahead, last {q.get('window_days', 30)} days")
    rows = [
        ("Useful", f"{_pct(rel.get('useful_rate'))} of what people judged ({rel.get('judged', 0)} judged)"),
        ("On time", f"{_pct(tim.get('prepared_before_needed'))} prepared before it was needed"
         + (f", {tim['median_lead_hours']:.0f} h ahead in the middle" if tim.get("median_lead_hours") is not None else "")),
        ("Early warnings", f"{acc.get('confirmed', 0)} of {acc.get('early_warnings', 0)} confirmed; "
         f"false alarms {_pct(acc.get('false_positive_rate'))}"),
        ("Saved", f"{eff.get('runs_blocked_before_they_failed', 0)} runs fixed before they failed; "
         f"{eff.get('queries', 0)} queries and {eff.get('model_calls', 0)} model calls spent"),
        ("Interruptions", f"{att.get('pushed', 0)} brought to people ({att.get('pushes_per_active_day', 0):.1f} a day); "
         f"{att.get('held', 0)} held for quiet hours or daily limits"),
        ("Governance", f"{gov.get('accepted_without_audit', 0)} accepted without an audit record; "
         f"{gov.get('tasks_run_as_someone_else', 0)} run as someone else; {gov.get('denied_attempts', 0)} refused"),
    ]
    for i, (label, text) in enumerate(rows):
        kit.row(label, text, key=f"aq{i}", divider=i < len(rows) - 1)
    st.caption(md(f"From {q.get('basis') or 'recorded events'}."))
    gaps = [a for a in items() if a.get("kind") == "capability_gap"]
    if gaps:
        kit.label(f"Methods Lyst could add · {len(gaps)}")
        rows(gaps, key="cg")
