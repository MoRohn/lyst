# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Needs you, answered by picking: an answer that needs someone (needs input, or not verified) shows the one question
Lyst has, its choices as a multiple-choice list, and "Something else" for your own words. Lyst carries out the pick.

:func:`panel` draws it under the answer; :func:`sheet` opens the same thing as a dialog (Work › Needs you). The choices
come from the API (``/v1/tasks/{id}/needs``), worked out from the workspace by ``lyst.needs``; a choice that changes
something for everyone says so beside the button, which then reads "Confirm and ...". Nothing here decides who may
do what: the API checks every pick again."""

from __future__ import annotations

from typing import Any

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client

OTHER = "__other__"
# the button reads as what will happen
VERB = {"ask": ("Ask Lyst", ":material/arrow_forward:"), "map": ("Confirm and ask again", ":material/check:"),
        "request": ("Ask an admin", ":material/send:"), "open": ("Open it", ":material/arrow_outward:"),
        "dismiss": ("Clear it", ":material/done:"), "other": ("Ask Lyst", ":material/arrow_forward:")}

CSS = """<style>
div[class*="st-key-lyst_needs_"] {border: 1px solid color-mix(in srgb, var(--lyst-warn) 50%, transparent) !important;
  border-radius: 16px; padding: 1rem 1.15rem .95rem;}
div[class*="st-key-lyst_needs_"] .lyst-needs-kicker {font-size: .76rem; font-weight: 600; letter-spacing: .05em;
  text-transform: uppercase; color: var(--lyst-warn); margin: 0 0 .15rem;}
div[class*="st-key-lyst_needs_"] h3.lyst-needs-q {font-size: 1.3rem; line-height: 1.35; margin: 0 0 .5rem; font-weight: 650;
  padding: 0;}
div[class*="st-key-lyst_needs_"] [data-testid="stRadio"], div[class*="st-key-lyst_needs_"] [role="radiogroup"] {width: 100%;}
div[class*="st-key-lyst_needs_"] [role="radiogroup"] {gap: .45rem;}
div[class*="st-key-lyst_needs_"] [role="radiogroup"] > div {width: 100%; box-sizing: border-box; padding: .6rem .85rem;
  border: 1px solid var(--lyst-border); border-radius: 12px; background: var(--lyst-bg);
  transition: border-color var(--lyst-motion-fast) ease;}
div[class*="st-key-lyst_needs_"] [role="radiogroup"] > div:hover {border-color: var(--lyst-accent);}
div[class*="st-key-lyst_needs_"] [role="radiogroup"] > div:has(input:checked) {border-color: var(--lyst-accent);
  box-shadow: inset 0 0 0 1px var(--lyst-accent);}
div[class*="st-key-lyst_needs_"] [data-testid="stRadioOption"] {width: 100%;}
div[class*="st-key-lyst_needs_"] [data-testid="stRadioOption"] p {font-weight: 560;}
</style>"""


def _get(task_id: str) -> dict[str, Any] | None:
    try:
        return client().get(f"/v1/tasks/{task_id}/needs")
    except APIError:
        return None


def _open_task(task_id: str) -> None:
    st.switch_page("app_pages/analyst.py", query_params={"task": task_id})


def _resolved(v: dict[str, Any], key: str) -> None:
    res = v["resolution"]
    st.html(f'<p class="lyst-needs-kicker">Resolved</p><h3 class="lyst-needs-q">{kit._esc(res["label"])}</h3>')
    st.caption(" · ".join(x for x in (
        "Lyst asked again" if res.get("follow_task_id") else "An admin was asked" if res.get("review_id") else "Cleared",
        kit.when(res.get("resolved_at"))) if x))
    if res.get("follow_task_id") and st.button("Open the new answer", type="primary", icon=":material/arrow_outward:",
                                               key=f"needs_new_{key}"):
        _open_task(res["follow_task_id"])


def _label(c: dict[str, Any]) -> str:
    return c["label"] + ("  ·  recommended" if c.get("recommended") else "")


def panel(task_id: str, *, key: str | None = None, data: dict[str, Any] | None = None) -> bool:
    """The question and its choices for one answer. False (and nothing drawn) when the answer needs no one."""
    v = data or _get(task_id)
    if not v or not (v.get("needs") or v.get("resolution")):
        return False
    key = key or task_id
    st.html(CSS)
    with st.container(key=f"lyst_needs_{key}"):
        if v.get("resolution"):
            _resolved(v, key)
            return True
        st.html('<p class="lyst-needs-kicker">Lyst needs you</p>'
                f'<h3 class="lyst-needs-q">{kit._esc(v["issue"])}</h3>')
        choices = v["choices"]
        opts = [c["id"] for c in choices] + [OTHER]
        by = {c["id"]: c for c in choices}
        start = next((i for i, c in enumerate(choices) if c.get("recommended")), None)
        pick = st.radio("Choose", opts, index=start, key=f"needs_pick_{key}", label_visibility="collapsed",
                        format_func=lambda i: "Something else" if i == OTHER else _label(by[i]),
                        captions=[c.get("detail") or "" for c in choices] + ["Tell Lyst in your own words."],
                        disabled=not v["can_resolve"])
        if not v["can_resolve"]:
            st.caption("Only the person who asked, or an editor, can resolve this.")
            return True
        text = ""
        if pick == OTHER:
            text = st.text_input("Your words", placeholder=v["other"]["placeholder"], key=f"needs_text_{key}",
                                 label_visibility="collapsed")
        c = by.get(pick) or {"kind": "other"}
        if c.get("confirm"):
            st.info(c["confirm"], icon=":material/policy:")
        verb, icon = VERB.get(c["kind"], VERB["ask"])
        cols = st.columns([1.6, 4])
        go = cols[0].button(verb, type="primary", icon=icon, key=f"needs_go_{key}", width="stretch",
                            disabled=pick is None or (pick == OTHER and not text.strip()))
        if c["kind"] == "ask" and c.get("question"):
            cols[1].caption(f"Lyst asks: “{c['question']}”")
        if go:
            body = {"text": text} if pick == OTHER else {"choice": pick, "confirm": bool(c.get("confirm"))}
            out = kit.guarded(lambda: client().post(f"/v1/tasks/{task_id}/needs/resolve", body))
            if out:
                st.toast(out.get("done") or "Done", icon=":material/check:")
                if out.get("task_id"):
                    _open_task(out["task_id"])
                elif out.get("go"):
                    st.switch_page(out["go"]["page"], query_params=out["go"].get("params") or {})
                else:
                    st.rerun()
    return True


@st.dialog("Needs you", width="large")
def sheet(task_id: str, title: str = "") -> None:
    """The same question and choices as a dialog, from a list of what needs you."""
    v = _get(task_id)
    if title:
        st.caption(title)
    if not panel(task_id, key=f"dlg_{task_id}", data=v):
        st.caption("This no longer needs anyone.")
    if st.button("Open the answer", type="tertiary", icon=":material/arrow_outward:", key=f"needs_open_{task_id}"):
        _open_task(task_id)


__all__ = ["panel", "sheet"]
