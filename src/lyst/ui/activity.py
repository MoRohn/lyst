# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Contextual autonomous activity: what Lyst's event rules noticed and did, shown where it happened.

One small kit every page uses, so activity reads the same everywhere and never becomes a feed:

* :func:`presence` is a quiet chip on an object (a dataset, an asset, a task) when Lyst has activity about it:
  working on it, waiting for you, a suggestion, or done. Nothing when there is none.
* :func:`panel` lists an object's activities as compact cards: why it started (the explanation), where it is
  (the lifecycle stages the engine recorded), what it produced (an outcome preview), and the one or two things a
  person can do (run a suggestion, approve, dismiss, open the result).
* :func:`flow` draws one activity's recorded chain: trigger → rule → decision → task → verification → artifact.
* :func:`live` is a fragment that follows the workspace's event stream with a cursor that only moves forward. When an
  activity or a task the person started changes, it says so once (a toast), drops the cached lists and lets the page
  show the authoritative state on its next read. It never invents a transition: everything shown is re-read from the
  API. Each browser tab keeps its own cursor; after a reload it starts from now and reads the current state.
* :func:`rules` is the rule workspace for Settings: templates to adopt as drafts, a dry run, activation (with the
  approval it needs), and what each rule decided recently.

Every state comes from the engine (``/v1/workspaces/{ws}/activities``); the UI holds no autonomy logic.
"""

from __future__ import annotations

import time
from typing import Any

import streamlit as st

from lyst.ui import kit, visuals
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

TTL = 15  # seconds an activity list is reused within a session; live() drops it when something changed
POLL = 6  # seconds between event checks (a cheap cursor read; nothing is fetched when nothing happened)
# status -> (words, chip tone). Five visual states only: info, suggestion, working, needs you, done, problem.
STATUS = {"observed": ("Noted", "info"), "suggested": ("Suggestion", "info"), "planned": ("Starting", "run"),
          "running": ("Working on it", "run"), "awaiting_approval": ("Needs your approval", "wait"),
          "completed": ("Done", "verified"), "needs_attention": ("Needs a look", "warn"),
          "failed": ("Did not finish", "bad"), "blocked": ("Not allowed", "bad"), "cancelled": ("Dismissed", "off")}
OPEN = ("suggested", "planned", "running", "awaiting_approval", "needs_attention", "failed", "blocked")
STAGE = {"detect": "Noticed", "interpret": "Understood", "qualify": "Worth acting on", "plan": "Planned",
         "authorize": "Allowed", "execute": "Ran", "verify": "Checked", "deliver": "Delivered", "monitor": "Followed up"}
MARK = {"done": "done", "approved": "done", "failed": "failed", "denied": "failed", "rejected": "failed",
        "waiting": "running", "awaiting_approval": "pending", "skipped": "skipped"}
ACTION_WORDS = {"notify": "Tell the owners", "impact_review": "Review what depends on it", "investigate": "Investigate",
                "retry_task": "Retry once", "propose": "Propose a change"}
LEVEL_WORDS = {"observe": "Only watch", "suggest": "Suggest", "prepare": "Prepare", "investigate": "Investigate",
               "automate": "Automate"}


def _ws() -> str:
    return workspace()


def _drop() -> None:
    st.session_state.pop("_activities", None)


def listed(kind: str | None = None, oid: str | None = None, *, status: str | None = None) -> list[dict] | None:
    """Activities (newest first) for the workspace or one object; None when the engine has no activity API."""
    ws = _ws()
    memo = st.session_state.setdefault("_activities", {})
    key = (ws, kind, oid, status)
    hit = memo.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    params: dict[str, Any] = {"limit": 50}
    if kind and oid:
        params |= {"kind": kind, "id": oid}
    if status:
        params["status"] = status
    try:
        out = client().get(f"/v1/workspaces/{ws}/activities", **params)
    except APIError as e:
        if e.status in (404, 405):
            return None
        out = []
    if len(memo) > 60:
        memo.clear()
    memo[key] = (time.monotonic(), out)
    return out


def summary(acts: list[dict]) -> tuple[str, str] | None:
    """The one chip that best says where an object's activity stands, or None."""
    live_ = [a for a in acts if a["status"] in OPEN]
    for status in ("awaiting_approval", "running", "planned", "failed", "blocked", "needs_attention", "suggested"):
        n = sum(a["status"] == status for a in live_)
        if n:
            words, tone = STATUS[status]
            return (words if n == 1 else f"{words} · {n}"), tone
    return None


def presence(kind: str, oid: str, where: Any = st) -> None:
    """A quiet chip when Lyst has open activity about this object; nothing otherwise. Reads the workspace's list
    (one cached call however many objects a page shows), so a long list of rows costs no extra requests."""
    acts = [a for a in (listed() or []) if a["subject_kind"] == kind and a["subject_id"] == oid]
    if not acts:
        return
    s = summary(acts)
    if s:
        kit.chip(s[0], s[1], where)


def stages_html(a: dict) -> str:
    items = [{"capability": STAGE.get(s["stage"], s["stage"]), "agent": s.get("detail") or "",
              "status": MARK.get(s.get("status", ""), "done" if s.get("status") == "done" else "queued")}
             for s in a.get("stages") or []]
    return visuals.steps(items, label="How it went", status_text=False)


def flow(a: dict) -> None:
    """The recorded chain from the event that started it to what it produced."""
    nodes = a.get("flow")
    if not nodes:
        try:
            nodes = client().get(f"/v1/workspaces/{_ws()}/activities/{a['id']}")["flow"]
        except APIError:
            return
    names = {"trigger": "Event", "rule": "Rule", "decision": "Decision", "task": "Work", "verification": "Check",
             "artifact": "Result"}
    steps = [(names.get(n["node"], n["node"]), str(n.get("label") or "—").replace("_", " ")[:40],
              f"v{n['ref']}" if n["node"] == "rule" and n.get("ref") else "") for n in nodes]
    st.html(visuals.flow(steps, label="Why this happened"))


def outcome(a: dict, key: str = "") -> None:
    """A light preview of what the activity produced, with the way to open it."""
    out = a.get("outcome") or {}
    if out.get("affected"):
        n = len(out["affected"])
        st.markdown(f"**{n} thing{'s' * (n != 1)} depend on it**")
        for x in out["affected"][:5]:
            st.markdown(f"- {md(x.get('title') or x.get('name'))} · {md(x.get('why') or x.get('kind'))}")
        fixable = [r for r in out.get("repairs") or [] if r.get("automatic")]
        if fixable:
            st.caption(f"{len(fixable)} can be repaired automatically; each repair keeps the current version.")
    if out.get("asset_id") and st.button("Open the result", key=f"actart_{key}_{a['id']}", icon=":material/article:",
                                         type="tertiary"):
        from lyst.ui import artifacts  # noqa: PLC0415

        artifacts.open_artifact({"kind": "asset", "id": out["asset_id"]})
    if a.get("task_id") and st.button("Open the work", key=f"acttask_{key}_{a['id']}", icon=":material/arrow_outward:",
                                      type="tertiary"):
        st.session_state.task_id = a["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": a["task_id"]})


def _post(path: str, ok: str) -> None:
    if kit.guarded(lambda: client().post(path), ok):
        _drop()
        st.rerun()


def card(a: dict, *, key: str, admin: bool = False) -> None:
    """One activity: what and why, where it is, what it made, and what you can do."""
    words, tone = STATUS.get(a["status"], (a["status"], "info"))
    ws = _ws()
    with kit.card(f"act_{key}_{a['id']}"):
        top = st.columns([5, 1.6], vertical_alignment="center")
        top[0].markdown(f"**{md(ACTION_WORDS.get(a['action'], a['action']))}** · "
                        f"{md(a.get('subject_title') or a['subject_id'])}")
        kit.chip(words, tone, top[1])
        st.caption(f"{a['explanation']} · {kit.when(a.get('created_at'))}")
        if a["status"] == "awaiting_approval":
            st.info("Lyst prepared this but will not run it until an admin approves it.", icon=":material/approval:")
            if admin and a.get("approval_id"):
                c = st.columns([1.2, 1.2, 4])
                if c[0].button("Approve", key=f"actok_{key}_{a['id']}", type="primary", icon=":material/check:"):
                    if kit.guarded(lambda: client().post(f"/v1/approvals/{a['approval_id']}", {"approve": True}), "Approved"):
                        _drop()
                        st.rerun()
                if c[1].button("Reject", key=f"actno_{key}_{a['id']}", icon=":material/close:"):
                    if kit.guarded(lambda: client().post(f"/v1/approvals/{a['approval_id']}", {"approve": False}), "Rejected"):
                        _drop()
                        st.rerun()
        with st.expander("How it went", icon=":material/timeline:", expanded=a["status"] in ("failed", "blocked")):
            flow(a)
            st.html(stages_html(a))
        outcome(a, key)
        c = st.columns([1.2, 1.2, 4])
        if a["status"] == "suggested" and c[0].button("Do it", key=f"actrun_{key}_{a['id']}", type="primary",
                                                      icon=":material/play_arrow:"):
            _post(f"/v1/workspaces/{ws}/activities/{a['id']}/run", "Started")
        if a["status"] in ("failed", "blocked") and c[0].button("Try again", key=f"actre_{key}_{a['id']}",
                                                                icon=":material/refresh:"):
            _post(f"/v1/workspaces/{ws}/activities/{a['id']}/run", "Running it again")
        if a["status"] in OPEN and a["status"] != "awaiting_approval" and c[1].button(
                "Dismiss", key=f"actx_{key}_{a['id']}", icon=":material/close:", type="tertiary"):
            _post(f"/v1/workspaces/{ws}/activities/{a['id']}/dismiss", "Dismissed")


def panel(kind: str, oid: str, *, key: str, admin: bool = False, limit: int = 3) -> None:
    """An object's activity, open first; nothing at all when there is none."""
    acts = listed(kind, oid)
    if not acts:
        return
    acts = sorted(acts, key=lambda a: (a["status"] not in OPEN, a.get("created_at") or ""), reverse=False)[:limit]
    kit.label("What Lyst is doing about it")
    for a in acts:
        card(a, key=key, admin=admin)


def workspace_list(*, admin: bool = False) -> None:
    """Every activity in the workspace for Work: open first, then the last finished ones, grouped by state."""
    acts = listed()
    if acts is None:
        return
    if not acts:
        kit.empty(":material/bolt:", "No automatic activity yet",
                  "When a rule you turn on in Settings notices something (a schema change, a failed check, a broken "
                  "report), what Lyst does about it appears here, with why it started.")
        return
    open_ = [a for a in acts if a["status"] in OPEN]
    done = [a for a in acts if a["status"] not in OPEN][:10]
    if open_:
        kit.label(f"Open · {len(open_)}")
        for a in open_:
            card(a, key="ws", admin=admin)
    if done:
        kit.label("Finished")
        for i, a in enumerate(done):
            words, tone = STATUS.get(a["status"], (a["status"], "info"))
            if kit.row(f"{ACTION_WORDS.get(a['action'], a['action'])} · {a.get('subject_title') or a['subject_id']}",
                       f"{a['explanation'][:140]} · {kit.when(a.get('created_at'))}", status=(words, tone),
                       actions=[("Details", "d")], key=f"actdone{a['id']}", divider=i < len(done) - 1):
                details(a["id"])


@st.dialog("Activity", width="large")
def details(aid: str) -> None:
    try:
        a = client().get(f"/v1/workspaces/{_ws()}/activities/{aid}")
    except APIError as e:
        st.error(kit.reason(e))
        return
    card(a, key="dlg", admin=st.session_state.get("role") in ("admin", "owner"))


# -- live updates ------------------------------------------------------------------------------------

def notable(ev: dict, me: str | None) -> str | None:
    """The one line worth interrupting for, or None. Only activity changes and the person's own tasks qualify."""
    t, d = ev.get("type", ""), ev.get("data") or {}
    if t == "activity.changed" and d.get("to") == "awaiting_approval":
        return "Lyst prepared something that needs approval"
    if t == "activity.changed" and d.get("to") == "completed":
        return "An automatic activity finished"
    if t == "activity.changed" and d.get("to") in ("failed", "blocked"):
        return "An automatic activity did not finish"
    if t == "task.completed" and me and d.get("created_by") == me:
        return "Your analysis finished"
    if t == "task.failed" and me and d.get("created_by") == me:
        return "Your analysis stopped"
    return None


def advance(cursor: int, events: list[dict]) -> tuple[int, list[dict]]:
    """Events strictly after the cursor, in order, and the new cursor. A stale or repeated event is dropped, so an
    older event can never overwrite what a newer one already showed."""
    fresh = sorted((e for e in events if int(e.get("seq", 0)) > cursor), key=lambda e: e["seq"])
    seen: set[int] = set()
    out = [e for e in fresh if not (e["seq"] in seen or seen.add(e["seq"]))]
    return (out[-1]["seq"] if out else cursor), out


@st.fragment(run_every=POLL)
def live() -> None:
    """Follow the workspace's events; announce what matters once and let the page re-read the rest."""
    ws = _ws()
    if not ws:
        return
    key = f"_evcursor_{ws}"
    api = client()
    try:
        if key not in st.session_state:  # a new tab or a reload: start from now, the page shows current state
            st.session_state[key] = int(api.get(f"/v1/workspaces/{ws}/events", after=-1)["next"])
            return
        page = api.get(f"/v1/workspaces/{ws}/events", after=st.session_state[key], limit=100)
    except APIError:
        return
    cursor, fresh = advance(st.session_state[key], page.get("events") or [])
    st.session_state[key] = max(cursor, int(page.get("next") or 0), st.session_state[key])
    if not fresh:
        return
    if any(e["type"].startswith(("activity.", "task.", "asset.", "dataset.")) for e in fresh):
        _drop()
        st.session_state.pop("_actions", None)
        st.session_state.pop("_zone", None)  # the activity zone re-reads on its next refresh
    me = st.session_state.get("me_id")
    said: set[str] = set()
    for e in fresh:
        line = notable(e, me)
        if line and line not in said:
            said.add(line)
            st.toast(line, icon=":material/bolt:")


# -- rules (Settings) ----------------------------------------------------------------------------------

RULE_STATUS = {"active": ("On", "ok"), "draft": ("Off", "off"), "pending_approval": ("Waiting for approval", "wait"),
               "retired": ("Retired", "off")}


def rules(*, admin: bool) -> None:
    api, ws = client(), _ws()
    try:
        have = api.get(f"/v1/workspaces/{ws}/rules")
        cat = api.get("/v1/rule-templates")
    except APIError as e:
        if e.status in (404, 405):
            return
        st.error(kit.reason(e))
        return
    st.caption("Rules decide what Lyst does when something happens: a schema change, a failed check, a report that "
               "breaks. Each one has a ceiling on how far it may go on its own; anything that changes something "
               "waits for an approval.")
    if have:
        for i, r in enumerate(have):
            words, tone = RULE_STATUS.get(r["status"], (r["status"], "info"))
            spec = r["spec"]
            meta = (f"{ACTION_WORDS.get(spec['action']['kind'], spec['action']['kind'])} · "
                    f"on its own up to {LEVEL_WORDS.get(spec['autonomy'], spec['autonomy']).lower()} · {r['risk']} risk · v{r['current_version']}"
                    + (f" · matched {r['hits']} time{'s' * (r['hits'] != 1)}" if r.get("hits") else ""))
            acts = ([("Turn off", "off")] if r["status"] == "active" else [("Turn on", "on")]
                    if r["status"] == "draft" else []) if admin else []
            hit = kit.row(spec["name"], meta, status=(words, tone), actions=acts + [("Check", "sim")],
                          key=f"rule{r['id']}", divider=i < len(have) - 1)
            if hit == "on":
                out = kit.guarded(lambda x=r: api.post(f"/v1/workspaces/{ws}/rules/{x['id']}/activate"))
                if out:
                    st.toast("On" if out["status"] == "active" else "Sent for approval; it turns on once approved",
                             icon=":material/bolt:")
                    st.rerun()
            elif hit == "off":
                if kit.guarded(lambda x=r: api.post(f"/v1/workspaces/{ws}/rules/{x['id']}/deactivate"), "Turned off"):
                    st.rerun()
            elif hit == "sim":
                dry_run(spec)
    adopted = {r["spec"]["name"] for r in have}
    offer = {k: t for k, t in cat["templates"].items() if t["name"] not in adopted}
    if admin and offer:
        kit.label("Start from a rule")
        for i, (k, t) in enumerate(offer.items()):
            hit = kit.row(t["name"], t.get("description", ""), actions=[("Check", "sim"), ("Add", "add")],
                          key=f"tpl{k}", divider=i < len(offer) - 1)
            if hit == "add":
                if kit.guarded(lambda x=t: api.post(f"/v1/workspaces/{ws}/rules", {"spec": x}),
                               "Added, off until you turn it on"):
                    st.rerun()
            elif hit == "sim":
                dry_run(t)


@st.dialog("What this rule would have done", width="large")
def dry_run(spec: dict) -> None:
    try:
        out = client().post(f"/v1/workspaces/{_ws()}/rules/simulate", {"spec": spec, "last": 500})
    except APIError as e:
        st.error(kit.reason(e))
        return
    c = out.get("counts") or {}
    st.html(visuals.stats([("Events checked", str(out["evaluated"]), None),
                           ("Would act", str(c.get("act", 0) + c.get("proposed", 0)), None),
                           ("Would suggest", str(c.get("suggested", 0)), None),
                           ("Held back", str(c.get("suppressed", 0) + c.get("capped", 0) + c.get("chain_limit", 0)), None)],
                          compact=True))
    st.caption(f"{out['risk'].capitalize()} risk" + (" · turning it on needs an approval" if out["needs_approval"] else "")
               + " · nothing was run.")
    for r in [x for x in out["results"] if x["decision"] != "no_match"][:12]:
        st.markdown(f"- **{md(r['decision'].replace('_', ' '))}** · {md(r['type'])} on {md(r['subject']['id'])}: "
                    f"{md(r['why'])}")


__all__ = ["advance", "card", "details", "flow", "listed", "live", "notable", "panel", "presence", "rules", "summary",
           "workspace_list"]
