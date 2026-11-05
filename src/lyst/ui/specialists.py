# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Brain › Specializations: the kinds of analyst work Lyst can take on, what each needs, how mature it is here, and
what it checks. One Lyst assembles the expertise per request; these are profiles to browse, not chatbots to pick."""

from __future__ import annotations

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

TONE = {1: "off", 2: "info", 3: "ok", 4: "verified", 5: "verified"}
CAP = {"available": ("Available", "ok"), "partial": ("Partly", "warn"), "missing": ("Not yet", "off")}


def _ctx():
    return client(), workspace()


def catalog() -> None:
    api, ws = _ctx()
    try:
        ov = api.get("/v1/specializations/overview")
        doms = api.get("/v1/specializations/domains")
    except APIError as e:
        st.caption(str(e))
        return
    lv = ov.get("levels") or {}
    st.caption(md(f"{ov.get('profiles', 0)} kinds of analyst work across {ov.get('domains', 0)} areas. You don't pick one: "
                  "describe the work on Home and Lyst brings the right expertise. "
                  + ", ".join(f"{n} {k.lower()}" for k, n in lv.items() if n) + "."))
    c = st.columns([3, 2])
    q = c[0].text_input("Find", placeholder="Find a specialization (pricing, churn, reconciliation…)",
                        label_visibility="collapsed", key="sp_q")
    dom = c[1].selectbox("Area", [None] + [d["id"] for d in doms], label_visibility="collapsed", key="sp_dom",
                         format_func=lambda i: "All areas" if i is None else next(
                             f"{d['title']} · {d['count']}" for d in doms if d["id"] == i))
    try:
        rows = api.get("/v1/specializations", **{k: v for k, v in (("q", q.strip()), ("domain", dom)) if v})
    except APIError as e:
        st.caption(str(e))
        return
    gaps = ov.get("capability_gaps") or []
    if gaps and not q and not dom:
        with st.expander(f"What Lyst can't do yet · {len(gaps)}", icon=":material/construction:"):
            for i, g in enumerate(gaps[:8]):
                kit.row(g["title"], f"holds back {g['profiles_blocked']} specialization{'s' * (g['profiles_blocked'] != 1)}",
                        status=CAP.get(g.get("status"), (g.get("status"), "info")), key=f"gap{i}", divider=i < min(len(gaps), 8) - 1)
    kit.label(f"{len(rows)} specialization{'s' * (len(rows) != 1)}")
    for i, r in enumerate(rows[:60]):
        if kit.row(r["name"], r.get("description") or "", status=(r.get("level_label") or "", TONE.get(r.get("level"), "info")),
                   actions=[("Open", "open")], key=f"sp{r['id']}", divider=i < min(len(rows), 60) - 1):
            detail(r["id"])
    if len(rows) > 60:
        st.caption(f"Showing 60 of {len(rows)}; narrow the search to see more.")


@st.dialog("Specialization", width="large")
def detail(sid: str) -> None:
    api, ws = _ctx()
    d = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/specializations/{sid}"))
    if not d:
        return
    ident = (d.get("profile") or {}).get("identity") or {}
    m = d.get("maturity") or {}
    top = st.columns([4, 1.6], vertical_alignment="center")
    top[0].markdown(md(f"**{ident.get('name', sid)}** · {ident.get('subdomain') or ''}"))
    kit.chip(m.get("label") or "", TONE.get(m.get("level"), "info"), top[1])
    st.markdown(md(ident.get("description") or ""))
    if m.get("next_step"):
        st.caption(md(f"{m.get('label')}: {m['next_step']}"))
    acc = d.get("access") or {}
    if acc.get("note"):
        st.info(md(acc["note"]), icon=":material/lock:")
    prof = d.get("profile") or {}
    examples = (prof.get("quality") or {}).get("example_requests") or (prof.get("analytics") or {}).get("example_requests") or []
    if examples:
        kit.label("Try asking")
        for i, ex in enumerate(examples[:4]):
            text = ex if isinstance(ex, str) else ex.get("request") or ex.get("text") or str(ex)
            if kit.row(text, "", actions=[("Ask this", "ask")], key=f"spx{sid}{i}", divider=i < min(len(examples), 4) - 1):
                out = kit.guarded(lambda t=text: api.ask(ws, t))
                if out:
                    st.session_state.task_id = out["task_id"]
                    st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})
    caps = d.get("capabilities") or []
    if caps:
        kit.label("What it uses")
        st.markdown(" ".join(f":{ {'available': 'green', 'partial': 'orange'}.get(c.get('status'), 'gray') }-badge[{md(c['title'])}]"
                             for c in caps))
    ver = d.get("verification") or []
    if ver:
        auto = [v for v in ver if v.get("automated")]
        manual = [v for v in ver if not v.get("automated")]
        kit.label(f"What Lyst checks · {len(auto)} automatically")
        st.markdown("\n".join([f"- {md(v['title'])}" for v in auto] + [f"- {md(v['title'])} (not yet automated)" for v in manual]))
    rel = d.get("related") or []
    if rel:
        st.caption("Related: " + md(", ".join(r["name"] for r in rel[:6])))
