# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Decision records: compare options on weighted criteria, read Lyst's recommendation and its uncertainty, submit a
choice for approval, and review the outcome later. Lyst prepares; a person decides. Reads the session per call."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

STATUS = {"analyzed": ("Ready to decide", "info"), "submitted": ("Awaiting approval", "wait"),
          "approved": ("Approved", "ok"), "rejected": ("Rejected", "bad"), "reviewed": ("Reviewed", "verified")}
REC = {"recommended": "Lyst recommends", "no_clear_winner": "No clear winner", "insufficient_evidence": "Not enough evidence"}
CRIT = {"cost": "Cost", "impact": "Impact", "risk": "Risk", "time_to_value": "Time to value"}


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("editor", "admin", "owner")


def records() -> list[dict]:
    api, ws, _ = _ctx()
    try:
        return api.get(f"/v1/workspaces/{ws}/decision-records")
    except APIError:
        return []


@st.dialog("Compare options", width="large")
def new(task_id: str | None = None, question: str = "") -> None:
    """Frame the decision from a sentence, then fill in the options against weighted criteria."""
    api, ws, _ = _ctx()
    q = st.text_input("The decision", value=question, placeholder="Cut EMEA prices 5%, or hold prices and cut opex 3%?",
                      key="dr_q")
    frame = st.session_state.get("dr_frame")
    if st.button("Set it up", key="dr_frame_go", disabled=not q.strip()):
        frame = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/decision-records/frame", {"question": q.strip()}))
        st.session_state["dr_frame"] = frame
    if not frame:
        return
    if frame.get("note"):
        st.caption(md(frame["note"]))
    objective = st.text_input("What should improve, and how it is measured", key="dr_obj",
                              placeholder="EMEA operating margin, Q1 2027")
    crit = frame.get("suggested_criteria") or [{"name": k, "direction": "max", "weight": 0.25} for k in CRIT]
    kit.label("Criteria and weights")
    cols = st.columns(len(crit))
    weights = {c["name"]: cols[i].number_input(f"{CRIT.get(c['name'], c['name'])} ({'lower' if c['direction'] == 'min' else 'higher'} is better)",
                                                0.0, 1.0, float(c["weight"]), 0.05, key=f"dr_w_{c['name']}")
               for i, c in enumerate(crit)}
    kit.label("Options (one row each; a number per criterion)")
    blank = pd.DataFrame([{"option": "Do nothing", **{c["name"]: 0.0 for c in crit}},
                          {"option": "", **{c["name"]: 0.0 for c in crit}}])
    grid = st.data_editor(blank, num_rows="dynamic", hide_index=True, width="stretch", key="dr_grid",
                          column_config={"option": st.column_config.TextColumn("Option", required=True),
                                         **{c["name"]: st.column_config.NumberColumn(CRIT.get(c["name"], c["name"])) for c in crit}})
    evidence = st.text_area("Evidence (one per line: data, quotes, benchmarks)", height=80, key="dr_ev")
    alts = [{"name": r["option"].strip(), "values": {c["name"]: float(r[c["name"]] or 0) for c in crit}}
            for _, r in grid.iterrows() if str(r["option"] or "").strip()]
    if st.button("Analyze", type="primary", icon=":material/balance:", key="dr_go", disabled=len(alts) < 2 or not objective):
        spec = {"question": q.strip(), "objective": objective, "alternatives": alts,
                "criteria": [{"name": c["name"], "direction": c["direction"], "weight": weights[c["name"]]} for c in crit],
                "evidence": [e.strip() for e in evidence.splitlines() if e.strip()]}
        out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/decision-records",
                                           {"title": q.strip()[:120], "spec": spec, "task_id": task_id}))
        if out:
            st.session_state.pop("dr_frame", None)
            st.session_state["open_decision"] = out["id"]
            st.rerun()


@st.dialog("Decision", width="large")
def detail(did: str) -> None:
    api, ws, editor = _ctx()
    d = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/decision-records/{did}"))
    if not d:
        return
    text, tone = STATUS.get(d["status"], (d["status"], "info"))
    top = st.columns([4, 1.4], vertical_alignment="center")
    top[0].markdown(md(f"**{d['title']}**"))
    kit.chip(text, tone, top[1])
    rec = d.get("recommendation") or {}
    if rec.get("statement"):
        st.markdown(md(f"{REC.get(rec.get('status'), '')}: {rec['statement']}" if rec.get("status") in REC else rec["statement"]))
    ana = d.get("analysis") or {}
    rank = ana.get("ranking") or []
    if rank:
        kit.label("How the options compare")
        for i, r in enumerate(rank):
            name = r.get("name") if isinstance(r, dict) else str(r)
            score = r.get("score") if isinstance(r, dict) else None
            p = ((ana.get("uncertainty") or {}).get("p_best") or {}).get(name)
            kit.row(name, " · ".join(x for x in (f"score {score:.2f}" if isinstance(score, (int, float)) else None,
                                                  f"best in {p:.0%} of simulations" if isinstance(p, (int, float)) else None) if x),
                    key=f"drr{i}", divider=i < len(rank) - 1)
    for k, label in (("evidence", "Evidence"), ("risks", "Risks"), ("assumptions", "Assumptions"),
                     ("implementation", "To do it"), ("alternatives", "Other options")):
        items = rec.get(k) or []
        if items:
            with st.expander(label, icon=":material/list:"):
                st.markdown("\n".join(f"- {md(x if isinstance(x, str) else x.get('text') or x.get('name') or x)}" for x in items))
    opts = [a["name"] for a in (d.get("spec") or {}).get("alternatives") or []]
    if editor and d["status"] == "analyzed" and opts:
        kit.label("Decide")
        st.caption("Lyst prepares the comparison; a person decides. Choosing sends it to an approver.")
        pick = st.selectbox("Choose", opts, index=opts.index(rec["choice"]) if rec.get("choice") in opts else 0, key=f"dr_pick_{did}")
        why = st.text_input("Why", key=f"dr_why_{did}")
        if st.button("Submit for approval", type="primary", key=f"dr_sub_{did}"):
            if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/decision-records/{did}/submit",
                                            {"chosen": pick, "rationale": why}), "Submitted for approval"):
                st.rerun()
    if editor and d["status"] == "approved":
        kit.label("How did it turn out?")
        obs = st.number_input("Observed result", key=f"dr_obs_{did}", value=None, placeholder="the outcome metric now")
        note = st.text_input("Note", key=f"dr_note_{did}")
        if st.button("Record the outcome", type="primary", key=f"dr_rev_{did}", disabled=obs is None):
            if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/decision-records/{did}/review",
                                            {"observed": obs, "note": note}), "Recorded"):
                st.rerun()
    if d.get("outcome"):
        st.caption(md(f"Outcome: {d['outcome']}"))


def activity_rows() -> None:
    """Work › Activity › Decisions."""
    _, _, editor = _ctx()
    found = records()
    if editor and st.button("Compare options", icon=":material/balance:", type="tertiary", key="dr_new"):
        new()
    if (did := st.session_state.pop("open_decision", None)):
        detail(did)
    if not found:
        st.caption("No decisions recorded yet. Compare options to have Lyst weigh them, then submit a choice for approval.")
        return
    for i, d in enumerate(found):
        text, tone = STATUS.get(d["status"], (d["status"], "info"))
        meta = " · ".join(x for x in (REC.get(d.get("recommendation")), f"chosen: {d['chosen']}" if d.get("chosen") else None,
                                      kit.when(d.get("created_at"))) if x)
        if kit.row(d["title"], meta, status=(text, tone), actions=[("Open", "open")], key=f"dr{d['id']}",
                   divider=i < len(found) - 1):
            detail(d["id"])
