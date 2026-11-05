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
/* SQL: the editor and recent queries in the code face; recent queries as tight one-line links */
div.st-key-sql_text textarea {font-family: var(--lyst-font-code, ui-monospace, SFMono-Regular, Menlo, monospace);
                              font-size: var(--lyst-size-sm);}
div[class*="st-key-sqlr_"] button {min-height: 0; padding: .1rem 0; justify-content: flex-start; text-align: left;
                                   font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: var(--lyst-size-xs);
                                   color: var(--lyst-text-muted);}
/* ---------------------------------------------------------------- Lyst material
   Layered surfaces: a faint canvas, raised white (or graphite) surfaces with a soft short shadow, and a translucent
   material for the sidebar, header and sheets. Motion is short and stops under prefers-reduced-motion. */
@keyframes lyst-enter {from {opacity: 0; transform: translateY(6px);} to {opacity: 1; transform: none;}}
@keyframes lyst-sheet {from {opacity: 0; transform: translateY(18px) scale(.985);} to {opacity: 1; transform: none;}}
.block-container {animation: lyst-enter var(--lyst-motion-slow) var(--lyst-motion-ease);}
[data-testid="stHeader"] {background: transparent;}
[data-testid="stSidebar"] {background: var(--lyst-material) !important; backdrop-filter: saturate(180%) blur(24px);
                           border-right: 1px solid var(--lyst-edge) !important;}
[data-testid="stSidebar"] > div {background: transparent !important;}
[data-testid="stSidebarNavLink"] {border-radius: var(--lyst-radius-md); padding: .38rem .6rem; margin: 1px 0;
  transition: background var(--lyst-motion-fast) var(--lyst-motion-ease), box-shadow var(--lyst-motion-fast);}
[data-testid="stSidebarNavLink"][aria-current="page"] {background: var(--lyst-raised) !important;
  box-shadow: var(--lyst-shadow-1);}
[data-testid="stSidebarNavLink"][aria-current="page"] [data-testid="stIconMaterial"] {color: var(--lyst-accent) !important;}
[data-testid="stSidebarNavLink"][aria-current="page"] span {font-weight: var(--lyst-weight-strong);}
/* cards (kit.card): raised, soft edge, a gentle lift when they hold an action */
div[class*="st-key-lyst_card"] {background: var(--lyst-raised); border: 1px solid var(--lyst-edge);
  border-radius: var(--lyst-radius-lg); box-shadow: var(--lyst-shadow-1); padding: 1rem 1.1rem .7rem 1.1rem;
  transition: box-shadow var(--lyst-motion-base) var(--lyst-motion-ease), transform var(--lyst-motion-base) var(--lyst-motion-ease);}
div[class*="st-key-lyst_card"]:hover {box-shadow: var(--lyst-shadow-2); transform: translateY(-1px);}
div[class*="st-key-lyst_card"]:focus-within {box-shadow: var(--lyst-shadow-2);}
/* every other bordered container (marked by _SURFACES) and expander reads as the same surface */
[data-lyst-surface="1"] {background: var(--lyst-raised); border-color: var(--lyst-edge) !important;
  border-radius: var(--lyst-radius-lg) !important; box-shadow: var(--lyst-shadow-1);}
[data-testid="stExpander"] details {background: var(--lyst-raised); border-color: var(--lyst-edge) !important;
  border-radius: var(--lyst-radius-md) !important; box-shadow: var(--lyst-shadow-1);}
[data-testid="stExpander"] summary {border-radius: var(--lyst-radius-md);}
/* sheets: dialogs rise from below with depth; popovers and toasts float on the material */
[data-testid="stDialog"] [role="dialog"] {border-radius: var(--lyst-radius-xl) !important; box-shadow: var(--lyst-shadow-3); background: var(--lyst-raised) !important;
  border: 1px solid var(--lyst-edge); animation: lyst-sheet var(--lyst-motion-slow) var(--lyst-motion-ease);}
[data-testid="stPopoverBody"] {border-radius: var(--lyst-radius-lg) !important; box-shadow: var(--lyst-shadow-3) !important;
  border: 1px solid var(--lyst-edge) !important;}
[data-testid="stToast"] {background: var(--lyst-material) !important; backdrop-filter: saturate(180%) blur(20px);
  border: 1px solid var(--lyst-edge); box-shadow: var(--lyst-shadow-2); border-radius: var(--lyst-radius-lg) !important;}
/* controls: a tactile press, quick hover, no jumps */
button {transition: background-color var(--lyst-motion-fast) var(--lyst-motion-ease), box-shadow var(--lyst-motion-fast),
        transform var(--lyst-motion-fast) var(--lyst-motion-ease), color var(--lyst-motion-fast);}
button:active:not(:disabled) {transform: scale(.975);}
button[data-testid="stBaseButton-primary"], button[data-testid="stBaseButton-primaryFormSubmit"] {box-shadow: var(--lyst-shadow-1);
  font-weight: var(--lyst-weight-medium);}
button[data-testid="stBaseButton-secondary"] {background: var(--lyst-raised); box-shadow: var(--lyst-shadow-1);
  border-color: var(--lyst-edge) !important;}
/* segmented control: an inset track with a raised thumb, like a native segmented control */
[data-testid="stButtonGroup"] [role="radiogroup"] {background: var(--lyst-surface); border-radius: var(--lyst-radius-md);
  padding: 2px; gap: 2px;}
button[data-variant="segmented_control"] {border: none !important; border-radius: 8px !important; background: transparent;
  min-height: 1.9rem;}
button[data-variant="segmented_control"][aria-checked="true"] {background: var(--lyst-raised) !important;
  box-shadow: var(--lyst-shadow-1);}
/* inputs: a soft field with an accent glow on focus */
[data-testid="stTextInputRootElement"], [data-testid="stTextArea"] textarea, [data-baseweb="select"] > div {
  border-radius: var(--lyst-radius-md) !important;}
[data-testid="stTextInputRootElement"]:focus-within, [data-testid="stTextArea"] [data-baseweb="textarea"]:focus-within {
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--lyst-accent) 22%, transparent);}
/* tables and charts sit on the same raised surface */
[data-testid="stDataFrame"] {border-radius: var(--lyst-radius-md); overflow: hidden; box-shadow: var(--lyst-shadow-1);}
/* settings panes: a list on the left, the current pane a raised pill */
div[class*="st-key-lyst_pane_"] button {justify-content: flex-start; padding: .45rem .7rem; border-radius: var(--lyst-radius-md);
  min-height: 0;}
div[class*="st-key-lyst_pane_"] button > div, div[class*="st-key-lyst_recent_"] button > div,
div[class*="st-key-sqlr_"] button > div {justify-content: flex-start; width: 100%;}
div[class*="st-key-lyst_pane_on_"] button {background: var(--lyst-raised); box-shadow: var(--lyst-shadow-1);
  font-weight: var(--lyst-weight-strong);}
div[class*="st-key-lyst_pane_on_"] button [data-testid="stIconMaterial"] {color: var(--lyst-accent);}
/* sidebar: recent work as quiet one-line links */
div[class*="st-key-lyst_recent_"] button {min-height: 0; padding: .22rem .6rem; justify-content: flex-start;
  text-align: left; font-size: var(--lyst-size-sm); color: var(--lyst-text-muted); border-radius: var(--lyst-radius-md);}
div[class*="st-key-lyst_recent_"] button:hover {background: var(--lyst-raised); color: var(--lyst-text);}
div[class*="st-key-lyst_recent_"] button p {overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
/* quiet chrome */
[data-testid="stToolbar"] {opacity: .55;}
footer {visibility: hidden;}
</style>
"""


# Streamlit gives a bordered container no attribute of its own, so one small observer marks them (a 1px border on a
# vertical block) as Lyst surfaces; CSS then raises them like kit.card. Without JavaScript they keep the hairline.
_SURFACES = """<script>
(() => {
  if (window.__lystSurfaces) return; window.__lystSurfaces = true;
  const mark = () => document.querySelectorAll('[data-testid="stVerticalBlock"]:not([data-lyst-surface]), [data-testid="stForm"]:not([data-lyst-surface])').forEach(e => {
    const cs = getComputedStyle(e);
    e.setAttribute('data-lyst-surface', cs.borderTopWidth === '1px' && cs.borderTopStyle === 'solid' ? '1' : '0');
  });
  let queued = false;
  new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; mark(); }); } })
    .observe(document.body, {childList: true, subtree: true});
  mark();
})();
</script>"""


def style() -> None:
    """The stylesheet, with the token block for the active theme (light or dark)."""
    from lyst.ui import artifacts, tokens, visuals  # noqa: PLC0415

    try:
        mode = st.context.theme.type or "light"
    except Exception:  # noqa: BLE001 - outside a browser session
        mode = "light"
    st.html(_CSS.replace("<style>", "<style>\n" + tokens.css(mode) + "\n" + visuals.CSS + artifacts.CSS, 1))
    st.html(_SURFACES, unsafe_allow_javascript=True)


def card(key: str, *, height: int | str = "content") -> Any:
    """A raised surface for one thing (a dataset, a metric, a finding): returns the container to write into."""
    return st.container(key=f"lyst_card_{key}", height=height, border=False)


def header(title: str, subtitle: str | None = None, *, action: str | None = None, icon: str | None = None,
           key: str | None = None) -> bool:
    """Title, purpose, and the page's one primary action. Returns True when the action was clicked."""
    cols = st.columns([5, 1.6] if action else [1], vertical_alignment="bottom")
    with cols[0]:
        st.html(f'<h1 class="lyst-title">{_esc(title)}</h1>' + (f'<p class="lyst-sub">{_esc(subtitle)}</p>' if subtitle else ""))
    if action:
        return cols[1].button(action, type="primary", icon=icon, key=key or f"hdr_{title}", width="stretch")
    return False


PANE_ICONS = {"Models": ":material/neurology:", "Connections": ":material/key:", "Workspace": ":material/apartment:",
              "Voice": ":material/graphic_eq:", "Advanced": ":material/tune:"}


def panes(names: Sequence[str], *, key: str, icons: dict[str, str] | None = None) -> tuple[str, Any]:
    """Settings-style navigation: a list of panes on the left, the chosen pane's content on the right. The choice
    lives in the URL (?view=), like sections, so links and reloads land on it. Returns (pane, content column)."""
    icons = icons or PANE_ICONS
    asked = (st.query_params.get(key) or "").lower()
    pick = next((n for n in names if n.lower() == asked), names[0])
    nav, body = st.columns([1, 3.4], gap="large")
    with nav:
        for n in names:
            on = n == pick
            if st.button(n, key=f"lyst_pane_{'on_' if on else ''}{n.lower().replace(' ', '_')}", type="tertiary",
                         icon=icons.get(n), width="stretch") and not on:
                st.query_params[key] = n.lower()
                st.rerun()
    if st.query_params.get(key) != pick.lower():
        st.query_params[key] = pick.lower()
    return pick, body


def group(title: str, key: str, note: str | None = None) -> Any:
    """A titled, raised group of related settings, like an inset group on a native settings screen."""
    label(title)
    if note:
        st.caption(note)
    return card(f"grp_{key}")


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
    st.session_state.pop("_recent", None)
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
