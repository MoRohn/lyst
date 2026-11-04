# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's design kit: the few building blocks every page is made of, so the product reads as one thing.

* ``style()``: one stylesheet (spacing, type scale, list rows, focus, quiet chrome), written in design tokens
  (``lyst.ui.tokens``). Colours come from the Streamlit theme in ``.streamlit/config.toml``, never from here.
* ``header()``: the page title, one line of purpose, and at most one primary action.
* ``sections()``: at most three sections per page, as a segmented control kept in the URL.
* ``row()``: a list item (title, quiet metadata, a status chip, actions), the replacement for raw tables.
* ``empty()``: what an empty list says and the one thing to do about it.
* ``chip()``: status with the same colours and icons everywhere.

Rules the pages follow: lists before tables; details in a dialog, not another tab; one primary button per view;
every action answers with a toast or an inline message; nothing technical unless the person asks for it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import streamlit as st

from lyst.ui.components import md

_CSS = """
<style>
/* layout: a calm, centred reading column; denser on small screens. Values come from tokens (--lyst-*). */
.block-container {max-width: var(--lyst-content-max); padding-top: 2.2rem; padding-bottom: 4rem;}
@media (max-width: 640px) {
  .block-container {padding-left: var(--lyst-gutter-mobile); padding-right: var(--lyst-gutter-mobile); padding-top: 4rem;}
  /* list rows stay one line on a phone: title, status and action side by side, not stacked */
  div[data-testid="stHorizontalBlock"]:has(div.lyst-row-title) {flex-wrap: nowrap !important; gap: .4rem;}
  div[data-testid="stHorizontalBlock"]:has(div.lyst-row-title) > div[data-testid="stColumn"] {
    min-width: 0 !important; width: auto !important; flex: 1 1 auto !important;}
  div[data-testid="stHorizontalBlock"]:has(div.lyst-row-title) > div[data-testid="stColumn"]:first-child {flex: 4 1 0 !important;}
  /* an empty status slot takes no room on a phone */
  div[data-testid="stColumn"]:has(div.lyst-slot) {display: none;}
}
/* type scale: one title size, one section label, quiet captions */
h1.lyst-title {font-size: var(--lyst-size-xl); font-weight: var(--lyst-weight-strong); letter-spacing: var(--lyst-tracking-title);
               line-height: var(--lyst-leading-tight); margin: 0 0 .15rem 0; padding: 0;}
p.lyst-sub {color: var(--lyst-text-muted); margin: 0 0 .2rem 0; font-size: .95rem;}
p.lyst-label {text-transform: uppercase; letter-spacing: var(--lyst-tracking-label); font-size: var(--lyst-size-xs);
              font-weight: 600; color: var(--lyst-text-muted); margin: 1.4rem 0 .35rem 0;}
/* numbers line up in columns and metrics: tabular figures everywhere data is shown */
[data-testid="stMetricValue"], [data-testid="stDataFrame"], [data-testid="stTable"], .lyst-num {font-variant-numeric: tabular-nums;}
/* list rows: a hairline separator instead of a box per item */
div.lyst-row-title p {font-weight: var(--lyst-weight-medium); margin: 0;}
div.lyst-row-meta p {color: var(--lyst-text-muted); font-size: var(--lyst-size-sm); margin: 0;}
div[data-testid="stElementContainer"]:has(> div > hr) {margin: -0.55rem 0 -0.55rem 0;}
hr {margin: 0.2rem 0 !important; border-color: var(--lyst-border) !important;}
/* empty states */
div.lyst-empty {text-align: center; padding: 2.2rem 1rem;}
div.lyst-empty .icon {font-size: var(--lyst-icon-lg); color: var(--lyst-text-muted);}
div.lyst-empty h4 {margin: .4rem 0 .2rem 0; font-weight: 600;}
/* Streamlit gives every heading a hover link that takes space after the text, pulling a centred title off-centre */
div.lyst-empty [data-testid="stHeaderActionElements"] {display: none;}
div.lyst-empty p {color: var(--lyst-text-muted); margin: 0 auto; max-width: 30rem;}
/* focus: one visible ring for every control reached by keyboard (WCAG 2.4.7 / 2.4.11) */
button:focus-visible, a:focus-visible, [role="radio"]:focus-visible, [role="tab"]:focus-visible,
[data-baseweb="select"] div:focus-visible, summary:focus-visible {
  outline: 2px solid var(--lyst-focus) !important; outline-offset: 2px !important; border-radius: var(--lyst-radius-sm);}
/* the selected section reads at 4.5:1 on both themes (the accent alone is 3.7:1 on dark) */
button[data-variant="segmented_control"][aria-checked="true"] {color: var(--lyst-link) !important;}
/* code: syntax colours from the tokens, every one 4.5:1 on the code background */
[data-testid="stCode"] .token.keyword, [data-testid="stCode"] .token.function {color: var(--lyst-info) !important;}
[data-testid="stCode"] .token.string {color: var(--lyst-good) !important;}
[data-testid="stCode"] .token.number, [data-testid="stCode"] .token.boolean {color: var(--lyst-warn) !important;}
[data-testid="stCode"] .token.comment {color: var(--lyst-text-muted) !important;}
/* long queries wrap instead of hiding behind a horizontal scroll (also keeps the block keyboard-reachable) */
[data-testid="stCode"] pre, [data-testid="stCode"] code {white-space: pre-wrap !important; overflow-wrap: anywhere;}
[data-testid="stCode"] .token.operator, [data-testid="stCode"] .token.punctuation {color: var(--lyst-text) !important;}
/* the command palette's trigger: a search field look, with the shortcut on the right */
div.st-key-lyst_palette button {justify-content: flex-start; color: var(--lyst-text-muted);}
/* quiet chrome */
[data-testid="stToolbar"] {opacity: .55;}
footer {visibility: hidden;}
</style>
"""


def style() -> None:
    """The stylesheet, with the token block for the active theme (light or dark)."""
    from lyst.ui import tokens  # noqa: PLC0415

    try:
        mode = st.context.theme.type or "light"
    except Exception:  # noqa: BLE001 - outside a browser session
        mode = "light"
    st.html(_CSS.replace("<style>", "<style>\n" + tokens.css(mode) + "\n", 1))


def header(title: str, subtitle: str | None = None, *, action: str | None = None, icon: str | None = None,
           key: str | None = None) -> bool:
    """Title, purpose, and the page's one primary action. Returns True when the action was clicked."""
    cols = st.columns([5, 1.6] if action else [1], vertical_alignment="bottom")
    with cols[0]:
        st.html(f'<h1 class="lyst-title">{_esc(title)}</h1>' + (f'<p class="lyst-sub">{_esc(subtitle)}</p>' if subtitle else ""))
    if action:
        return cols[1].button(action, type="primary", icon=icon, key=key or f"hdr_{title}", width="stretch")
    return False


def label(text: str) -> None:
    st.html(f'<p class="lyst-label">{_esc(text)}</p>')


def sections(names: Sequence[str], *, key: str, counts: dict[str, int] | None = None,
             default: str | None = None) -> str:
    """At most three sections, picked with one control and kept in the URL so links and reloads land on them."""
    counts = counts or {}
    asked = (st.query_params.get(key) or "").lower()
    default = next((n for n in names if n.lower() == asked), default or names[0])
    pick = st.segmented_control(key, list(names), default=default, key=f"sec_{key}", label_visibility="collapsed",
                                format_func=lambda n: f"{n} · {counts[n]}" if counts.get(n) else n) or default
    if st.query_params.get(key) != pick.lower():
        st.query_params[key] = pick.lower()
    return pick


CHIPS = {
    "ok": ("green", ":material/check_circle:"), "verified": ("green", ":material/verified:"),
    "warn": ("orange", ":material/error:"), "wait": ("orange", ":material/schedule:"),
    "run": ("blue", ":material/progress_activity:"), "bad": ("red", ":material/cancel:"),
    "off": ("gray", ":material/radio_button_unchecked:"), "info": ("gray", None),
}


def chip(text: str, tone: str = "info", where: Any = st) -> None:
    color, icon = CHIPS.get(tone, CHIPS["info"])
    where.badge(text, color=color, icon=icon)


def row(title: str, meta: str = "", *, status: tuple[str, str] | None = None,
        actions: Sequence[tuple[str, str]] = (), key: str, divider: bool = True, slot: bool = True) -> str | None:
    """One list item. ``actions`` are (label, id) pairs shown as quiet buttons; returns the id clicked, if any.

    Rows with actions keep a status slot even when this row has no status, so the action buttons of every row in
    a list sit in the same column instead of jumping right when a neighbour has a chip. ``slot=False`` opts out."""
    reserve = bool(status) or (slot and bool(actions))
    widths = [6] + ([1.4] if reserve else []) + [1.1] * len(actions)
    cols = st.columns(widths, vertical_alignment="center")
    with cols[0]:
        st.markdown(f'<div class="lyst-row-title">\n\n{md(title)}\n\n</div>', unsafe_allow_html=True)
        if meta:
            st.markdown(f'<div class="lyst-row-meta">\n\n{md(meta)}\n\n</div>', unsafe_allow_html=True)
    i = 2 if reserve else 1
    if status:
        chip(status[0], status[1], cols[1])
    elif reserve:
        cols[1].html('<div class="lyst-slot"></div>')
    clicked = None
    for (text, aid), col in zip(actions, cols[i:], strict=True):
        if col.button(text, key=f"{key}_{aid}", type="tertiary", width="stretch"):
            clicked = aid
    if divider:
        st.divider()
    return clicked


def empty(icon: str, title: str, body: str, *, action: str | None = None, key: str | None = None) -> bool:
    # markdown, not raw HTML, so the :material/...: icon renders as an icon
    st.markdown(f'<div class="lyst-empty">\n\n<div class="icon">\n\n{icon}\n\n</div>\n\n'
                f'<h4>{_esc(title)}</h4>\n\n<p>{_esc(body)}</p>\n\n</div>', unsafe_allow_html=True)
    if action:
        c = st.columns([2, 1.4, 2])
        return c[1].button(action, type="primary", key=key or f"empty_{title}", width="stretch")
    return False


def guarded(fn: Callable[[], Any], ok: str | None = None, *, icon: str = ":material/check:") -> Any:
    """Run an API call: a toast on success, the reason inline on failure. Nothing fails silently."""
    from lyst.ui.client import APIError  # noqa: PLC0415

    try:
        out = fn()
    except APIError as e:
        st.error(str(e), icon=":material/error:")
        return None
    st.session_state.pop("_waiting", None)  # something changed: recount what waits on this person
    if ok:
        st.toast(ok, icon=icon)
    return out


def when(ts: Any) -> str:
    """'just now', '5 min ago', 'yesterday 14:05', '3 Nov' — times people read, not ISO stamps."""
    from datetime import UTC, datetime  # noqa: PLC0415

    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)[:16]
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    secs = (datetime.now(UTC) - t).total_seconds()
    if secs < -60:  # a time ahead (a schedule's next run): 'in 5 min', 'in 3 h', 'tomorrow 08:00', '9 Nov'
        ahead = -secs
        if ahead < 3600:
            return f"in {int(ahead // 60)} min"
        if ahead < 6 * 3600:
            return f"in {int(ahead // 3600)} h"
        local, today = t.astimezone(), datetime.now(UTC).astimezone().date()
        if local.date() == today:
            return "today " + local.strftime("%H:%M")
        if (local.date() - today).days == 1:
            return "tomorrow " + local.strftime("%H:%M")
        return local.strftime("%-d %b %H:%M")
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    if secs < 2 * 86400:
        return "yesterday " + t.astimezone().strftime("%H:%M")
    return t.astimezone().strftime("%-d %b")


def lower(title: str) -> str:
    """'COGS ratio' -> 'COGS ratio', 'Operating margin' -> 'operating margin': lower case that keeps acronyms."""
    return " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in title.split())


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
