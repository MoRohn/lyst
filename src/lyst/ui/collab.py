# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Working together on what Lyst produces: discuss an answer, hand it to someone, ask for a review, and see what
is waiting on you. Imported by pages; reads the session on every call (a module is imported once per process)."""

from __future__ import annotations

from datetime import date

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

COMMENT_KIND = {"comment": "Comment", "change_request": "Ask for a change", "decision": "Record a decision"}
CHIP = {"change_request": ("Change asked", "warn"), "decision": ("Decision", "verified")}
NOTIFY = {"mention": "Someone mentions me", "assignment": "Something is assigned to me",
          "assignment_done": "Something I assigned is done", "review_requested": "My review is requested",
          "review_decided": "A review I asked for is decided", "comment_on_owned": "Someone comments on what I own"}


def _ctx():
    return client(), workspace()


def members() -> dict[str, dict]:
    api, ws = _ctx()
    try:
        return {m["user_id"]: m for m in api.get(f"/v1/workspaces/{ws}/members")}
    except APIError:
        return {}


def _who(m: dict) -> str:
    return m.get("name") or m["email"]


@st.dialog("Discussion", width="large")
def discuss(kind: str, oid: str, title: str = "") -> None:
    """The thread on one object: comments, change requests (resolvable) and decisions, with @email mentions."""
    api, ws = _ctx()
    act = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/objects/{kind}/{oid}/activity"))
    if act is None:
        return
    st.markdown(md(f"**{act.get('title') or title}**"))
    owner = act.get("owner_name")
    open_cr = act.get("open_change_requests") or 0
    people = members()
    assignees = [a.get("assignee_name") or (_who(people[a["assignee"]]) if a["assignee"] in people else "a colleague")
                 for a in act.get("assignments") or [] if a.get("status") == "open"]
    st.caption(" · ".join(x for x in (f"Owned by {owner}" if owner else None,
                                      f"{open_cr} change request{'s' * (open_cr != 1)} open" if open_cr else None,
                                      f"assigned to {', '.join(assignees)}" if assignees else None) if x))
    comments = act.get("comments") or []
    if not comments:
        st.caption("No discussion yet. Mention someone with their email, like @dana@example.com, to bring them in.")
    for c in comments:
        with st.container(border=True):
            top = st.columns([4, 1.4], vertical_alignment="center")
            top[0].markdown(md(f"**{c.get('author_name') or c['author']}** · {kit.when(c['created_at'])}"))
            if c["kind"] in CHIP:
                label, tone = CHIP[c["kind"]]
                kit.chip("Resolved" if c.get("status") == "resolved" else label,
                         "ok" if c.get("status") == "resolved" else tone, top[1])
            st.markdown(md(c["body"]))
            if c["kind"] == "change_request" and c.get("status") == "open" and \
                    st.button("Mark resolved", type="tertiary", icon=":material/check:", key=f"res_{c['id']}"):
                kit.guarded(lambda x=c: api.post(f"/v1/workspaces/{ws}/comments/{x['id']}/resolve", {}), "Resolved")
    with st.form(f"comment_{kind}_{oid}", clear_on_submit=True, border=False):
        body = st.text_area("Add to the discussion", height=80, placeholder="Write a comment; @email mentions someone")
        ck = st.radio("As", list(COMMENT_KIND), format_func=COMMENT_KIND.get, horizontal=True)
        if st.form_submit_button("Post", type="primary", icon=":material/send:") and body.strip():
            kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/objects/{kind}/{oid}/comments",
                                         {"body": body.strip(), "kind": ck}), "Posted")


def assign(kind: str, oid: str, *, key: str) -> None:
    """Hand it to a colleague with a note and an optional due date; they see it in Work › Needs you."""
    api, ws = _ctx()
    people = members()
    me = st.session_state.get("me_id")
    choices = [u for u in people if u != me] or list(people)
    if not choices:
        st.caption("No one else is in this workspace yet.")
        return
    who = st.selectbox("Who", choices, format_func=lambda u: _who(people[u]), key=f"as_who_{key}")
    note = st.text_input("What you need", placeholder="Check the EMEA numbers before Friday", key=f"as_note_{key}")
    due = st.date_input("By (optional)", value=None, min_value=date.today(), key=f"as_due_{key}")
    if st.button("Assign", type="primary", key=f"as_go_{key}", width="stretch"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/assignments", {
            "object_kind": kind, "object_id": oid, "assignee": who, "note": note,
            "due": due.isoformat() if due else None}), f"Assigned to {_who(people[who])}", icon=":material/assignment_ind:")


def request_review(kind: str, oid: str, summary: str, *, key: str) -> None:
    """Ask someone to look before this is relied on; anyone suitable can decide unless a reviewer is named."""
    api, ws = _ctx()
    people = members()
    me = st.session_state.get("me_id")
    who = st.selectbox("Reviewer", [None] + [u for u in people if u != me],
                       format_func=lambda u: "Anyone who can decide" if u is None else _who(people[u]), key=f"rv_who_{key}")
    note = st.text_input("What to check", value=summary[:200], key=f"rv_note_{key}")
    if st.button("Ask for a review", type="primary", key=f"rv_go_{key}", width="stretch"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/reviews", {
            "action": "look", "object_kind": kind, "object_id": oid, "payload": {}, "summary": note or summary,
            "reviewer": who}), "Review requested", icon=":material/rate_review:")


def waiting() -> tuple[list[dict], list[dict]]:
    """Open assignments for me, and reviews I can decide."""
    api, ws = _ctx()
    try:
        mine = api.get(f"/v1/workspaces/{ws}/assignments", mine=True, status="open")
        revs = api.get(f"/v1/workspaces/{ws}/reviews", status="pending", for_me=True)
    except APIError:
        return [], []
    return mine, revs


def open_object(kind: str, oid: str) -> None:
    if kind == "task":
        st.session_state.task_id = oid
        st.switch_page("app_pages/analyst.py", query_params={"task": oid})
    elif kind == "metric":
        st.switch_page("app_pages/brain.py", query_params={"metric": oid})
    elif kind == "dataset":
        st.switch_page("app_pages/data.py", query_params={"dataset": oid})
    else:
        st.switch_page("app_pages/data.py", query_params={"view": "dashboards"})


def needs_you_lists() -> None:
    """Assigned to you and Reviews waiting, for Work › Needs you."""
    api, ws = _ctx()
    mine, revs = waiting()
    people = members()
    if mine:
        kit.label(f"Assigned to you · {len(mine)}")
        for i, a in enumerate(mine):
            by = people.get(a.get("assigned_by"), {})
            hit = kit.row(a.get("note") or f"Look at this {a['object_kind']}",
                          " · ".join(x for x in (f"from {_who(by)}" if by else None,
                                                 f"due {a['due']}" if a.get("due") else None,
                                                 kit.when(a["created_at"])) if x),
                          actions=[("Open", "open"), ("Done", "done")], key=f"asg{a['id']}", divider=i < len(mine) - 1)
            if hit == "open":
                open_object(a["object_kind"], a["object_id"])
            elif hit == "done":
                if kit.guarded(lambda x=a: api.post(f"/v1/workspaces/{ws}/assignments/{x['id']}/complete", {}), "Marked done"):
                    st.rerun()
    if revs:
        kit.label(f"Reviews waiting · {len(revs)}")
        for i, r in enumerate(revs):
            by = people.get(r.get("requested_by"), {})
            hit = kit.row(r["summary"], " · ".join(x for x in (f"asked by {_who(by)}" if by else None,
                                                               kit.when(r["created_at"])) if x),
                          actions=[("Open", "open"), ("Approve", "yes"), ("Reject", "no")], key=f"rev{r['id']}",
                          divider=i < len(revs) - 1)
            if hit == "open":
                open_object(r["object_kind"], r["object_id"])
            elif hit in ("yes", "no"):
                if kit.guarded(lambda x=r, h=hit: api.post(f"/v1/workspaces/{ws}/reviews/{x['id']}/decide",
                                                           {"approve": h == "yes", "note": ""}),
                               "Approved" if hit == "yes" else "Rejected"):
                    st.rerun()


def bell() -> None:
    """Notifications in the sidebar: what was said to or about me, with preferences for what to hear about."""
    api, ws = _ctx()
    try:
        unread = api.get(f"/v1/workspaces/{ws}/notifications", unread=True)
    except APIError:
        return
    label = f"Notifications · {len(unread)}" if unread else "Notifications"
    with st.popover(label, icon=":material/notifications:", width="stretch"):
        if not unread:
            st.caption("Nothing new.")
        for n in unread[:12]:
            c = st.columns([5, 1.2], vertical_alignment="center")
            c[0].markdown(md(f"{n['text']}  \n") + f":gray[{kit.when(n['created_at'])}]")
            if c[1].button("Open", key=f"nt_{n['id']}", type="tertiary"):
                kit.guarded(lambda x=n: api.post(f"/v1/workspaces/{ws}/notifications/{x['id']}/read", {}))
                if n.get("object_kind") in ("task", "metric", "dataset", "asset"):
                    open_object(n["object_kind"], n["object_id"])
                elif n.get("object_kind") == "finding":
                    st.switch_page("app_pages/work.py", query_params={"view": "recurring"})
                st.rerun()
        if unread and st.button("Mark all read", type="tertiary", key="nt_all"):
            for n in unread:
                kit.guarded(lambda x=n: api.post(f"/v1/workspaces/{ws}/notifications/{x['id']}/read", {}))
            st.rerun()
        with st.expander("What to tell me about", icon=":material/tune:"):
            try:
                prefs = api.get(f"/v1/workspaces/{ws}/notification-preferences")
            except APIError:
                prefs = {}
            new = {k: st.toggle(NOTIFY.get(k, k.replace("_", " ").capitalize()), value=bool(v), key=f"np_{k}")
                   for k, v in prefs.items()}
            if new != prefs and st.button("Save", key="np_save", type="primary"):
                kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/notification-preferences", new), "Saved")
