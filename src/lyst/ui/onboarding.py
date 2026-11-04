# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The first run: where a workspace stands, the next thing to do, and the definitions Lyst suggests from the data
(each with the reason). Used by Home, Data and Settings; reads the session on every call."""

from __future__ import annotations

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

KIND = {"metric": "Measure", "glossary": "Business term", "policy": "Policy", "contract": "Data contract",
        "question": "Starter question", "source": "Missing data"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("editor", "admin", "owner")


def status() -> dict | None:
    api, ws, _ = _ctx()
    try:
        return api.get(f"/v1/workspaces/{ws}/onboarding")
    except APIError:
        return None


def proposals(kind: str | None = None) -> list[dict]:
    api, ws, _ = _ctx()
    try:
        return api.get(f"/v1/workspaces/{ws}/onboarding/proposals", **({"kind": kind} if kind else {}))
    except APIError:
        return []


def discover() -> None:
    api, ws, _ = _ctx()
    out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/onboarding/discover"))
    if out:
        found = [f"{d['title']}" + ("" if d.get("strength") == "strong" else " (possibly)") for d in out.get("domains") or []]
        st.toast((f"Recognised {', '.join(found)}. " if found else "")
                 + f"{len(out.get('new_proposals') or [])} new suggestions.", icon=":material/auto_awesome:")


def suggestion_rows(items: list[dict], *, key: str, limit: int | None = None) -> None:
    """Each suggestion with its one-line reason, Accept (where it can be accepted) and Dismiss."""
    api, ws, editor = _ctx()
    admin = st.session_state.get("role") in ("admin", "owner")
    shown = items[:limit] if limit else items
    for i, p in enumerate(shown):
        acts = ([("Accept", "yes")] if editor and p["kind"] != "source" else []) + ([("Dismiss", "no")] if admin else [])
        hit = kit.row(p["title"], f"{KIND.get(p['kind'], p['kind'])} · {(p.get('evidence') or {}).get('why') or ''}",
                      actions=acts, key=f"{key}{p['id']}", divider=i < len(shown) - 1)
        if hit == "yes":
            r = kit.guarded(lambda x=p: api.post(f"/v1/workspaces/{ws}/onboarding/proposals/{x['id']}/accept", {}))
            if r and (r.get("result") or {}).get("task_id"):
                st.session_state.task_id = r["result"]["task_id"]
                st.switch_page("app_pages/analyst.py", query_params={"task": r["result"]["task_id"]})
            elif r:
                st.toast("Added", icon=":material/check:")
                st.rerun()
        elif hit == "no":
            if kit.guarded(lambda x=p: api.post(f"/v1/workspaces/{ws}/onboarding/proposals/{x['id']}/dismiss"), "Dismissed"):
                st.rerun()


def _go(key: str) -> None:
    if key == "connect":
        st.switch_page("app_pages/data.py")
    elif key == "discover":
        discover()
        st.rerun()
    elif key == "confirm":
        st.switch_page("app_pages/data.py", query_params={"view": "datasets"})
    elif key in ("people", "objectives"):
        st.switch_page("app_pages/settings.py", query_params={"view": "workspace"})


ACTION = {"connect": "Add data", "discover": "Study it", "confirm": "Review", "people": "Invite",
          "objectives": "Add them"}


def card(*, extra: list[tuple[str, str, bool, str | None]] = ()) -> bool:
    """The Get started card. Returns False when there is no onboarding status (the caller keeps its own)."""
    stat = status()
    if stat is None:
        return False
    if stat["state"] == "complete" or st.session_state.get("hide_get_started"):
        return True
    _, _, editor = _ctx()
    steps = stat["steps"]
    done = sum(s["done"] for s in steps if not s["optional"])
    need = sum(not s["optional"] for s in steps)
    with st.container(border=True):
        top = st.columns([6, 1], vertical_alignment="center")
        top[0].markdown(f"**Get started** · {done} of {need} done")
        if top[1].button("Hide", type="tertiary", key="gs_hide", width="stretch"):
            st.session_state.hide_get_started = True
            st.rerun()
        for s in steps:
            c = st.columns([0.35, 6, 1.4], vertical_alignment="center")
            c[0].markdown(":material/check_circle:" if s["done"] else ":material/radio_button_unchecked:")
            title = s["title"] + (" (optional)" if s["optional"] else "")
            nxt = s["key"] == stat.get("next")
            c[1].markdown(f"~~{md(title)}~~" if s["done"] else f"{'**' if nxt else ''}{md(title)}{'**' if nxt else ''}  \n"
                          + md(s.get("detail") or ""))
            act = ACTION.get(s["key"])
            if not s["done"] and act and (editor or s["key"] in ("connect",)) and \
                    c[2].button(act, key=f"gs_{s['key']}", type="primary" if nxt else "tertiary", width="stretch"):
                _go(s["key"])
        for i, (title, body, ok, page) in enumerate(extra):
            if ok:
                continue
            c = st.columns([0.35, 6, 1.4], vertical_alignment="center")
            c[0].markdown(":material/radio_button_unchecked:")
            c[1].markdown(f"{md(title)}  \n{md(body)}")
            if page and c[2].button("Go", key=f"gsx_{i}", type="tertiary", width="stretch"):
                st.switch_page(page)
        pend = proposals()
        if pend and editor:
            kit.label(f"Suggested from your data · {len(pend)}")
            suggestion_rows([p for p in pend if p["kind"] != "source"], key="gsp", limit=3)
            if len(pend) > 3:
                st.page_link("app_pages/data.py", label=f"See all {len(pend)} suggestions", icon=":material/arrow_forward:")
    return True


def data_suggestions() -> None:
    """Data › Datasets: everything Lyst suggests from the data, and the data it would expect but cannot see."""
    _, _, editor = _ctx()
    items = proposals()
    if not items:
        return
    missing = [p for p in items if p["kind"] == "source"]
    rest = [p for p in items if p["kind"] != "source"]
    top = st.columns([5, 1.6], vertical_alignment="bottom")
    with top[0]:
        kit.label(f"Suggestions · {len(rest)}")
    if editor and top[1].button("Study the data again", type="tertiary", icon=":material/auto_awesome:", key="ob_rediscover",
                                width="stretch"):
        discover()
    suggestion_rows(rest, key="dsp")
    if missing:
        st.caption(":material/info: Lyst would also expect " + ", ".join(md(p["title"]) for p in missing)
                   + ". Add it with Add data when you have it.")


def profile_section() -> None:
    """Settings › Workspace: the business profile (industries, fiscal year, reporting period, objectives) and how fast
    this workspace got to its first answers."""
    api, ws, _ = _ctx()
    admin = st.session_state.get("role") in ("admin", "owner")
    try:
        prof = api.get(f"/v1/workspaces/{ws}/profile")
        packs = {d["id"]: d for d in api.get("/v1/domains")}
    except APIError:
        return
    spec = prof.get("spec") or {}
    kit.label("Business profile" + (f" · version {prof['version']}" if prof.get("version") else ""))
    if not admin:
        st.caption(" · ".join(x for x in (
            ", ".join(packs[d]["title"] for d in spec.get("domains") or [] if d in packs) or "No industry chosen",
            f"fiscal year starts in {MONTHS[(spec.get('fiscal_year_start_month') or 1) - 1]}",
            f"reports by {spec.get('reporting_period') or 'month'}") if x))
    else:
        doms = st.multiselect("Industry", list(packs), default=[d for d in spec.get("domains") or [] if d in packs],
                              format_func=lambda d: packs[d]["title"], key="pf_domains",
                              help="Lyst brings that industry's usual measures, terms and starter questions.")
        c = st.columns(2)
        fy = c[0].selectbox("Fiscal year starts in", list(range(1, 13)), index=(spec.get("fiscal_year_start_month") or 1) - 1,
                            format_func=lambda m: MONTHS[m - 1], key="pf_fy")
        per = c[1].selectbox("We report by", ["week", "month", "quarter"],
                             index=["week", "month", "quarter"].index(spec.get("reporting_period") or "month"),
                             format_func=str.capitalize, key="pf_per")
        obj = st.text_area("What matters this year (one per line)", "\n".join(spec.get("objectives") or []), height=90,
                           key="pf_obj", placeholder="Grow EMEA margin back above 35%\nCut support backlog by half")
        new = {"domains": doms, "fiscal_year_start_month": fy, "reporting_period": per,
               "objectives": [x.strip() for x in obj.splitlines() if x.strip()]}
        if any(new[k] != (spec.get(k) or ([] if k in ("domains", "objectives") else None)) for k in new) and \
                st.button("Save profile", type="primary", key="pf_save"):
            if kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/profile", new), "Profile saved"):
                st.rerun()
        terms = spec.get("terminology") or {}
        if terms:
            st.caption("Our words: " + ", ".join(f"{md(t)} means {md(str(v.get('metric') if isinstance(v, dict) else v))}"
                                                  for t, v in terms.items()))
    stat = status()
    m = (stat or {}).get("milestones") or {}

    def took(sec: float | None) -> str | None:
        if sec is None:
            return None
        return f"{sec / 60:.0f} min" if sec < 5400 else (f"{sec / 3600:.1f} h" if sec < 172800 else f"{sec / 86400:.0f} days")

    parts = [f"first data after {took(m.get('seconds_to_first_dataset'))}" if m.get("first_dataset_at") else None,
             f"first answer after {took(m.get('seconds_to_first_analysis'))}" if m.get("first_analysis_at") else None,
             f"first verified answer after {took(m.get('seconds_to_first_verified'))}" if m.get("first_verified_at") else None]
    if any(parts):
        st.caption("Since this workspace was created: " + ", ".join(p for p in parts if p) + ".")
