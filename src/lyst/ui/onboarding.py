# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The first run: where a workspace stands, the next thing to do, and the definitions Lyst suggests from the data
(each with the reason). Used by Home, Data and Settings; reads the session on every call."""

from __future__ import annotations

import base64
import html

import streamlit as st
from streamlit.errors import StreamlitAPIException

from lyst.ui import kit, tokens
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


def _count(v) -> int:
    """The engine reports new suggestions as a number; accept a list too."""
    return v if isinstance(v, int) else len(v or [])


def discover() -> None:
    api, ws, _ = _ctx()
    out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/onboarding/discover"))
    if out:
        found = [f"{d['title']}" + ("" if d.get("strength") == "strong" else " (possibly)") for d in out.get("domains") or []]
        st.toast((f"Recognised {', '.join(found)}. " if found else "")
                 + f"{_count(out.get('new_proposals'))} new suggestions.", icon=":material/auto_awesome:")


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


def _rerun_here() -> None:
    """Stay in the sheet when it is one (a dialog is a fragment); otherwise rerun the page."""
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()


def _go(key: str) -> None:
    if key == "connect":
        st.switch_page("app_pages/data.py")
    elif key == "discover":
        discover()
        _rerun_here()  # the step ticks over in place and the next one comes up
    elif key == "confirm":
        st.switch_page("app_pages/data.py", query_params={"view": "datasets"})
    elif key in ("people", "objectives"):
        st.switch_page("app_pages/settings.py", query_params={"view": "workspace"})
    elif key == "first_answer":
        st.rerun()  # closes the sheet; Home's composer is the place to ask


ACTION = {"connect": "Add data", "discover": "Study it", "confirm": "Review", "people": "Invite",
          "objectives": "Add them", "first_answer": "Ask a question"}
# One Material Symbols glyph per step, so each reads at a glance and the list is not a column of identical circles.
ICON = {"connect": "database", "discover": "auto_awesome", "confirm": "fact_check", "first_answer": "forum",
        "people": "group_add", "objectives": "flag", "model": "neurology"}
WORDS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]

GUIDE_CSS = """<style>
/* Get started: a ring for the essentials, a rail of steps with one lit "next", optional extras apart, a finish. */
.lyst-gs-sym {font-family: "Material Symbols Rounded"; font-weight: normal; font-style: normal; font-feature-settings: "liga";
  -webkit-font-smoothing: antialiased; line-height: 1; letter-spacing: normal; text-transform: none; white-space: nowrap;
  direction: ltr; user-select: none;}
.lyst-gs-head {display: flex; align-items: center; gap: var(--lyst-space-4); padding: var(--lyst-space-2) 0 var(--lyst-space-3);}
.lyst-gs-ring {position: relative; flex: 0 0 4.25rem; height: 4.25rem; border-radius: 50%;
  background: conic-gradient(from 0deg, var(--lyst-mark-floor-b), var(--lyst-mark-floor-a) var(--p), var(--lyst-surface) 0);
  -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 7px), #000 calc(100% - 6px));
          mask: radial-gradient(farthest-side, transparent calc(100% - 7px), #000 calc(100% - 6px));}
.lyst-gs-ringbox {position: relative; flex: 0 0 4.25rem; height: 4.25rem;}
.lyst-gs-ringbox > span {position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  line-height: 1; font-size: var(--lyst-size-sm); color: var(--lyst-text-muted); font-variant-numeric: tabular-nums;}
.lyst-gs-ringbox > span b {font-size: 1.3rem; color: var(--lyst-text); font-weight: var(--lyst-weight-strong); margin-right: .05rem;}
.lyst-gs-ringbox > span i {font-style: normal; position: relative; top: .22rem;}
.lyst-gs-copy {display: flex; flex-direction: column; gap: .15rem; min-width: 0;}
.lyst-gs-copy b {font-size: var(--lyst-size-lg); font-weight: var(--lyst-weight-strong); letter-spacing: var(--lyst-tracking-title);
  color: var(--lyst-text); line-height: var(--lyst-leading-tight);}
.lyst-gs-copy span {color: var(--lyst-text-muted); font-size: var(--lyst-size-sm); line-height: 1.4;}
.lyst-gs-label {margin: var(--lyst-space-4) 0 var(--lyst-space-1); font-size: var(--lyst-size-xs); font-weight: var(--lyst-weight-medium);
  letter-spacing: var(--lyst-tracking-label); text-transform: uppercase; color: var(--lyst-text-muted);}
.lyst-gs-label em {font-style: normal; text-transform: none; letter-spacing: 0; opacity: .8; margin-left: .35rem;}
div.st-key-lyst_gs_steps {gap: 0 !important;}
div.st-key-lyst_gs_steps [data-testid="stElementContainer"] {margin: 0;}
.lyst-gs-step {position: relative; display: flex; align-items: center; gap: var(--lyst-space-3); padding: .45rem 0; min-height: 3.1rem;}
.lyst-gs-step::before, .lyst-gs-step::after {content: ""; position: absolute; left: calc(1.1rem - 1px); width: 2px;
  background: var(--lyst-border);}
.lyst-gs-step::before {top: 0; height: calc(50% - 1.1rem);}
.lyst-gs-step::after {bottom: 0; height: calc(50% - 1.1rem);}
.lyst-gs-step.first::before, .lyst-gs-step.last::after, .lyst-gs-step.solo::before, .lyst-gs-step.solo::after {display: none;}
.lyst-gs-step.is-done::after, .lyst-gs-step.after-done::before {background: color-mix(in srgb, var(--lyst-good) 55%, var(--lyst-border));}
.lyst-gs-ic {position: relative; z-index: 1; flex: 0 0 2.2rem; height: 2.2rem; display: grid; place-items: center;
  border-radius: var(--lyst-radius-pill); font-size: 1.15rem; color: var(--lyst-text-muted); background: var(--lyst-raised);
  box-shadow: inset 0 0 0 1.5px var(--lyst-border);}
.lyst-gs-step.is-done .lyst-gs-ic {color: #fff; background: var(--lyst-good); box-shadow: none; font-size: 1.2rem;}
.lyst-gs-txt {display: flex; flex-direction: column; min-width: 0; gap: .1rem;}
.lyst-gs-txt b {font-weight: var(--lyst-weight-medium); color: var(--lyst-text); font-size: .95rem; line-height: 1.3;}
.lyst-gs-txt span {color: var(--lyst-text-muted); font-size: var(--lyst-size-sm); line-height: 1.4;}
.lyst-gs-step.is-done .lyst-gs-txt b {color: var(--lyst-text-muted); font-weight: var(--lyst-weight-regular);}
.lyst-gs-step.is-later .lyst-gs-txt b {color: color-mix(in srgb, var(--lyst-text) 78%, transparent);}
.lyst-gs-kicker {font-size: var(--lyst-size-xs) !important; font-weight: var(--lyst-weight-strong); letter-spacing: var(--lyst-tracking-label);
  text-transform: uppercase; color: var(--lyst-link) !important;}
/* the next step: lit, raised, the one primary action in the sheet */
div.st-key-lyst_gs_next {position: relative; margin: .2rem -.75rem; padding: .7rem .75rem; border-radius: var(--lyst-radius-lg);
  background: color-mix(in srgb, var(--lyst-accent) 8%, var(--lyst-raised));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--lyst-link) 38%, transparent), var(--lyst-shadow-1);}
div.st-key-lyst_gs_next .lyst-gs-step {padding: 0;}
div.st-key-lyst_gs_next .lyst-gs-step::before {top: -.9rem; height: calc(50% - 1.35rem + .9rem);}
div.st-key-lyst_gs_next .lyst-gs-step::after {bottom: -.9rem; height: calc(50% - 1.35rem + .9rem);}
div.st-key-lyst_gs_next .lyst-gs-step.after-done::before {background: color-mix(in srgb, var(--lyst-good) 55%, var(--lyst-border));}
div.st-key-lyst_gs_next .lyst-gs-ic {flex-basis: 2.7rem; height: 2.7rem; font-size: 1.4rem; color: #fff; border-radius: var(--lyst-radius-md);
  background: linear-gradient(145deg, var(--lyst-mark-floor-a), var(--lyst-mark-stem-a)); box-shadow: 0 4px 14px color-mix(in srgb, var(--lyst-accent) 40%, transparent);
  margin-left: -.25rem; margin-right: -.25rem;}
div.st-key-lyst_gs_next .lyst-gs-ic::after {content: ""; position: absolute; inset: -4px; border-radius: inherit;
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--lyst-link) 45%, transparent); animation: lyst-gs-pulse 2.4s var(--lyst-motion-ease) infinite;}
@keyframes lyst-gs-pulse {0% {opacity: .9; transform: scale(.94);} 70%, 100% {opacity: 0; transform: scale(1.18);}}
@media (prefers-reduced-motion: reduce) {div.st-key-lyst_gs_next .lyst-gs-ic::after {animation: none; opacity: .5;}}
div.st-key-lyst_gs_next .lyst-gs-txt b {font-size: 1.02rem; font-weight: var(--lyst-weight-strong);}
div.st-key-lyst_gs_next [data-testid="stButton"] button {min-height: 2.5rem; border-radius: var(--lyst-radius-pill); font-weight: var(--lyst-weight-medium);
  box-shadow: 0 2px 10px color-mix(in srgb, var(--lyst-accent) 35%, transparent);}
/* optional extras: the same row, quieter, each with its own small action */
div[class*="st-key-lyst_gs_opt_"] {padding: .1rem .5rem; margin: 0 -.5rem; border-radius: var(--lyst-radius-md);
  transition: background var(--lyst-motion-fast) var(--lyst-motion-ease);}
@media (hover: hover) {div[class*="st-key-lyst_gs_opt_"]:hover {background: var(--lyst-surface);}}
div[class*="st-key-lyst_gs_opt_"] .lyst-gs-step {padding: .3rem 0; min-height: 2.6rem;}
div[class*="st-key-lyst_gs_opt_"] .lyst-gs-ic {flex-basis: 2rem; height: 2rem; font-size: 1.05rem; border-radius: var(--lyst-radius-sm);
  color: var(--lyst-link); background: color-mix(in srgb, var(--lyst-link) 12%, transparent); box-shadow: none;}
div[class*="st-key-lyst_gs_opt_"] .lyst-gs-step.is-done .lyst-gs-ic {color: #fff; background: var(--lyst-good);}
div[class*="st-key-lyst_gs_opt_"] .lyst-gs-step::before, div[class*="st-key-lyst_gs_opt_"] .lyst-gs-step::after {display: none;}
div.st-key-lyst_gs_foot {margin-top: var(--lyst-space-3); padding: var(--lyst-space-2) 0 0 !important; border: 0 !important;
  border-top: 1px solid var(--lyst-border) !important; border-radius: 0 !important; background: none !important; box-shadow: none !important;}
div.st-key-lyst_gs_foot p {color: var(--lyst-text-muted); font-size: var(--lyst-size-sm);}
/* the finish: Lyst's mark at the centre of widening rings, what it took, and one way forward */
.lyst-gs-finish {display: flex; flex-direction: column; align-items: center; text-align: center; gap: var(--lyst-space-2);
  padding: var(--lyst-space-2) 0 var(--lyst-space-3);}
.lyst-gs-art {width: 9.5rem; height: 9.5rem; display: block; animation: lyst-gs-arrive var(--lyst-motion-slow) var(--lyst-motion-spring) both;}
@keyframes lyst-gs-arrive {from {opacity: 0; transform: scale(.86);} to {opacity: 1; transform: none;}}
.lyst-gs-finish > b {font-size: 1.45rem; font-weight: var(--lyst-weight-strong); letter-spacing: var(--lyst-tracking-title); color: var(--lyst-text);}
.lyst-gs-finish > span {color: var(--lyst-text-muted); font-size: .95rem; max-width: 30rem; line-height: 1.45;}
.lyst-gs-stats {display: grid; grid-template-columns: repeat(auto-fit, minmax(5.5rem, 1fr)); gap: var(--lyst-space-2); width: 100%;
  margin-top: var(--lyst-space-3);}
.lyst-gs-stats div {display: flex; flex-direction: column; align-items: center; gap: .1rem; padding: .7rem .5rem;
  border-radius: var(--lyst-radius-md); background: var(--lyst-surface); box-shadow: inset 0 0 0 1px var(--lyst-edge);}
.lyst-gs-stats b {font-size: 1.15rem; font-weight: var(--lyst-weight-strong); font-variant-numeric: tabular-nums; color: var(--lyst-text);}
.lyst-gs-stats span {font-size: var(--lyst-size-xs); color: var(--lyst-text-muted);}
div.st-key-lyst_gs_go [data-testid="stButton"] button {min-height: 2.75rem; border-radius: var(--lyst-radius-pill); font-size: 1rem;
  font-weight: var(--lyst-weight-medium); box-shadow: 0 2px 12px color-mix(in srgb, var(--lyst-accent) 35%, transparent);}
@media (max-width: 640px) {
  .lyst-gs-ringbox, .lyst-gs-ring {flex-basis: 3.75rem; height: 3.75rem;}
  div.st-key-lyst_gs_next .lyst-gs-step::before, div.st-key-lyst_gs_next .lyst-gs-step::after {display: none;}
  div.st-key-lyst_gs_next [data-testid="stHorizontalBlock"] {gap: var(--lyst-space-3);}
  div.st-key-lyst_gs_next [data-testid="stButton"] button {width: 100%;}
  .lyst-gs-art {width: 7.5rem; height: 7.5rem;}
  .lyst-gs-stats div {padding: .6rem .3rem;}
  .lyst-gs-stats b {font-size: 1rem;}
  .lyst-gs-stats span {text-align: center; line-height: 1.25;}
}
</style>"""

def _e(text: object) -> str:
    return html.escape(str(text or ""), quote=True)


def ring(done: int, need: int) -> str:
    """The essentials as a ring (a conic gradient, so it themes with the tokens): how far, with the count inside."""
    pct = round(100 * done / need) if need else 100
    return (f'<div class="lyst-gs-ringbox" role="img" aria-label="{done} of {need} essential steps done">'
            f'<div class="lyst-gs-ring" style="--p: {pct}%"></div><span><b>{done}</b><i>/{need}</i></span></div>')


def head(done: int, need: int, nxt: dict | None, *, extras: bool = False) -> str:
    left = need - done
    title = f"{WORDS[left] if left < len(WORDS) else left} essential{'s' if left != 1 else ''} left"
    sub = (f"Next: {_e(nxt['title'])}." if nxt else "") + (" The optional extras below don't count toward this." if extras else "")
    return f'<div class="lyst-gs-head">{ring(done, need)}<div class="lyst-gs-copy"><b>{_e(title)}</b><span>{sub.strip()}</span></div></div>'


def step_html(s: dict, *, pos: str = "", prev_done: bool = False, kicker: str | None = None, detail: bool | None = None) -> str:
    """One step: its glyph (a check once done), its title, and its line. pos: first, last, solo or ''."""
    state = "is-done" if s["done"] else ("is-next" if kicker else "is-later")
    cls = " ".join(x for x in ("lyst-gs-step", state, pos, "after-done" if prev_done else "") if x)
    glyph = "check" if s["done"] else ICON.get(s["key"], "radio_button_unchecked")
    show = (not s["done"]) if detail is None else detail
    line = "Done" if s["done"] else (s.get("detail") or "")
    return (f'<div class="{cls}"><span class="lyst-gs-ic lyst-gs-sym" aria-hidden="true">{glyph}</span>'
            f'<div class="lyst-gs-txt">' + (f'<span class="lyst-gs-kicker">{_e(kicker)}</span>' if kicker else "")
            + f'<b>{_e(s["title"])}</b>' + (f'<span>{_e(line)}</span>' if show and line else "")
            + "</div></div>")


def _took(sec: float | None) -> str | None:
    if sec is None:
        return None
    if sec < 60:
        return "< 1 min"
    return f"{sec / 60:.0f} min" if sec < 5400 else (f"{sec / 3600:.1f} h" if sec < 172800 else f"{sec / 86400:.0f} days")


def art(mode: str = "light") -> str:
    """The finish's picture: Lyst's mark at the centre of widening rings, a check, a few sparks. An image (st.html
    drops inline SVG), so its colours come from the tokens for the active theme."""
    c = tokens.colors(mode)
    ring, spark, good = c["link"], c["mark-floor-b"], c["good"]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160">'
           '<defs><radialGradient id="g" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="' + c["mark-floor-a"]
           + '" stop-opacity=".32"/><stop offset="1" stop-color="' + c["mark-floor-a"] + '" stop-opacity="0"/></radialGradient></defs>'
           '<circle cx="80" cy="80" r="56" fill="url(#g)"/>'
           f'<circle cx="80" cy="80" r="76" fill="none" stroke="{ring}" stroke-width="1" opacity=".14"/>'
           f'<circle cx="80" cy="80" r="62" fill="none" stroke="{ring}" stroke-width="1.2" opacity=".26" stroke-dasharray="3 5"/>'
           f'<circle cx="80" cy="80" r="46" fill="none" stroke="{ring}" stroke-width="1.6" opacity=".5"/>'
           f'<circle cx="80" cy="80" r="34" fill="{c["raised"]}" stroke="{ring}" stroke-opacity=".35"/>'
           '<g transform="translate(61.6 60.5) scale(1.15)"><path d="M3.4 31.8 15.2 21.4H28.5Q29.6 21.4 29 22.3L23.6 31Q23 31.8 22 31.8Z" '
           f'fill="{c["mark-floor-a"]}"/><path d="M2.8 10.6Q2.8 9.4 3.8 8.7L14.8.7Q16.2-.2 16.2 1.5V19.3Q16.2 20.6 15.2 21.4L3.6 '
           f'31.3Q2.8 32 2.8 30.9Z" fill="{c["mark-stem-a"]}"/></g>'
           f'<circle cx="111" cy="52" r="12" fill="{good}" stroke="{c["raised"]}" stroke-width="3"/>'
           '<path d="M105.5 52.5l3.8 3.8 7.2-7.6" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>'
           f'<path d="M28 36l2 5 5 2-5 2-2 5-2-5-5-2 5-2z" fill="{spark}"/><circle cx="140" cy="98" r="3" fill="{ring}"/>'
           f'<path d="M124 126l1.5 3.5 3.5 1.5-3.5 1.5-1.5 3.5-1.5-3.5-3.5-1.5 3.5-1.5z" fill="{ring}"/>'
           f'<circle cx="22" cy="112" r="2.5" fill="{spark}"/><circle cx="52" cy="16" r="2" fill="{ring}"/>'
           f'<path d="M104 6l1.2 3 3 1.2-3 1.2-1.2 3-1.2-3-3-1.2 3-1.2z" fill="{spark}"/></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _mode() -> str:
    try:
        return st.context.theme.type or "light"
    except Exception:  # noqa: BLE001 - outside a browser session
        return "light"


def finish_html(stat: dict, mode: str = "light") -> str:
    """The finished state: what is now true, and what it took."""
    m = stat.get("milestones") or {}
    stats = [(_took(m.get("seconds_to_first_dataset")), "to first data") if m.get("first_dataset_at") else None,
             (_took(m.get("seconds_to_first_analysis")), "to first answer") if m.get("first_analysis_at") else None,
             (_took(m.get("seconds_to_first_verified")), "to a verified answer") if m.get("first_verified_at") else None]
    tiles = "".join(f"<div><b>{_e(v)}</b><span>{_e(k)}</span></div>" for v, k in (x for x in stats if x) if v)
    return (f'<div class="lyst-gs-finish" role="status"><img class="lyst-gs-art" src="{art(mode)}" alt=""><b>Lyst is ready</b>'
            '<span>Data studied, definitions confirmed, first answer verified. Every answer from here builds on them.</span>'
            + (f'<div class="lyst-gs-stats">{tiles}</div>' if tiles else "") + "</div>")


def guide(steps: list[dict], *, nxt: str | None, editor: bool, finished: dict | None = None) -> None:
    """Draw the guide. Each step: key, title, detail, done, optional, and optionally go (a callable) and action."""
    st.html(GUIDE_CSS)
    must = [s for s in steps if not s["optional"]]
    extra = [s for s in steps if s["optional"]]
    if finished is not None:
        st.html(finish_html(finished, _mode()))
        with st.container(key="lyst_gs_go"):
            if st.button("Ask Lyst a question", type="primary", icon=":material/arrow_forward:", key="gs_go", width="stretch"):
                st.rerun()
    else:
        done = sum(s["done"] for s in must)
        nstep = next((s for s in must if s["key"] == nxt), None)
        st.html(head(done, len(must), nstep, extras=bool(extra)))
        st.html('<p class="lyst-gs-label">Essentials</p>')
        with st.container(key="lyst_gs_steps"):
            buf: list[str] = []
            for i, s in enumerate(must):
                pos = "solo" if len(must) == 1 else ("first" if i == 0 else ("last" if i == len(must) - 1 else ""))
                prev = i > 0 and must[i - 1]["done"]
                if s["key"] != nxt:
                    buf.append(step_html(s, pos=pos, prev_done=prev))
                    continue
                if buf:
                    st.html("".join(buf))
                    buf = []
                with st.container(key="lyst_gs_next"):
                    act = _action(s, editor)
                    c = st.columns([5, 1.7] if act else [1], vertical_alignment="center")
                    c[0].html(step_html(s, pos=pos, prev_done=prev, kicker="Next up"))
                    if act and c[1].button(act, key=f"gs_{s['key']}", type="primary", width="stretch"):
                        _run(s)
            if buf:
                st.html("".join(buf))
    left = [s for s in extra if not s["done"]] if finished is not None else extra
    if left:
        st.html('<p class="lyst-gs-label">Make it yours<em>· optional</em></p>')
        for s in left:
            with st.container(key=f"lyst_gs_opt_{s['key']}"):
                act = None if s["done"] else _action(s, editor)
                c = st.columns([5, 1.7] if act else [1], vertical_alignment="center")
                c[0].html(step_html(s, pos="solo", detail=not s["done"]))
                if act and c[1].button(act, key=f"gs_{s['key']}", type="secondary", width="stretch"):
                    _run(s)


def _action(s: dict, editor: bool) -> str | None:
    if s["done"]:
        return None
    if s.get("go"):
        return s.get("action") or "Open"
    act = ACTION.get(s["key"])
    return act if act and (editor or s["key"] in ("connect", "first_answer")) else None


def _run(s: dict) -> None:
    if s.get("go"):
        s["go"]()
    else:
        _go(s["key"])


def footer() -> None:
    """Put the guide away; Settings brings it back."""
    with st.container(key="lyst_gs_foot"):
        c = st.columns([5, 1.7], vertical_alignment="center")
        c[0].markdown("Bring it back any time from Settings › Preferences.")
        if c[1].button("Hide guide", type="tertiary", icon=":material/visibility_off:", key="gs_hide", width="stretch"):
            st.session_state.hide_get_started = True
            st.session_state.pop("_home", None)  # Home's dock drops its Get started item at once
            st.rerun()


def card(*, extra: list[dict] = ()) -> bool:
    """The Get started guide. Returns False when there is no onboarding status (the caller keeps its own).

    The count is the essentials only (optional steps sit apart and never move it). Once the essentials are done the
    guide shows its finished state instead of disappearing, so the last step has an ending."""
    stat = status()
    if stat is None:
        return False
    if st.session_state.get("hide_get_started"):
        return True
    _, _, editor = _ctx()
    steps = [dict(s) for s in stat["steps"]] + [dict(x, optional=True) for x in extra]
    complete = stat["state"] == "complete"
    st.session_state["_gs_seen"] = st.session_state.get("_gs_seen") or not complete
    guide(steps, nxt=stat.get("next"), editor=editor, finished=stat if complete else None)
    pend = proposals() if not complete else []
    if pend and editor:
        rest = [p for p in pend if p["kind"] != "source"]
        if rest:
            st.html(f'<p class="lyst-gs-label">Suggested from your data<em>· {len(rest)}</em></p>')
            suggestion_rows(rest, key="gsp", limit=3)
        if len(pend) > 3:
            st.page_link("app_pages/data.py", label=f"See all {len(pend)} suggestions", icon=":material/arrow_forward:")
    if not complete:
        footer()
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

    parts = [f"first data after {_took(m.get('seconds_to_first_dataset'))}" if m.get("first_dataset_at") else None,
             f"first answer after {_took(m.get('seconds_to_first_analysis'))}" if m.get("first_analysis_at") else None,
             f"first verified answer after {_took(m.get('seconds_to_first_verified'))}" if m.get("first_verified_at") else None]
    if any(parts):
        st.caption("Since this workspace was created: " + ", ".join(p for p in parts if p) + ".")
