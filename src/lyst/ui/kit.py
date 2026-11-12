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

import re
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
  /* list rows stay one line on a phone: title, status and action side by side, not stacked. Only the row's own
     columns: a page layout that holds rows (Settings' list and section) still stacks, or its second column sits
     off-screen to the right */
  div[data-testid="stHorizontalBlock"]:has(div.lyst-row-title):not(:has(div[data-testid="stHorizontalBlock"] div.lyst-row-title)) {flex-wrap: nowrap !important; gap: .4rem;}
  div[data-testid="stHorizontalBlock"]:has(div.lyst-row-title):not(:has(div[data-testid="stHorizontalBlock"] div.lyst-row-title)) > div[data-testid="stColumn"] {
    min-width: 0 !important; width: auto !important; flex: 1 1 auto !important;}
  div[data-testid="stHorizontalBlock"]:has(div.lyst-row-title):not(:has(div[data-testid="stHorizontalBlock"] div.lyst-row-title)) > div[data-testid="stColumn"]:first-child {flex: 4 1 0 !important;}
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
/* a row's status and actions sit together at its right edge, not spread across the row */
div[data-testid="stHorizontalBlock"]:has(div.lyst-row-title):not(:has(div[data-testid="stHorizontalBlock"] div.lyst-row-title)) > div[data-testid="stColumn"]:not(:first-child) > div[data-testid="stVerticalBlock"] {align-items: flex-end;}
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
  border-radius: var(--lyst-radius-lg); box-shadow: var(--lyst-shadow-1); padding: 1rem 1.1rem 1.05rem 1.1rem;
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
/* a menu reads down its left edge: each row starts with its icon, not centred in the sheet */
[data-testid="stPopoverBody"] .stButton button[data-testid="stBaseButton-tertiary"] {justify-content: flex-start;
  text-align: left; min-height: 2.35rem; padding: .4rem .7rem; border-radius: var(--lyst-radius-md, 10px);}
[data-testid="stPopoverBody"] .stButton button[data-testid="stBaseButton-tertiary"] > div {justify-content: flex-start;}
[data-testid="stPopoverBody"] .stButton button[data-testid="stBaseButton-tertiary"]:hover:not(:disabled) {
  background: var(--lyst-surface);}
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
div[class*="st-key-sqlr_"] button > div, div[class*="st-key-act_"] button > div {justify-content: flex-start; width: 100%;}
div[class*="st-key-act_"] button {justify-content: flex-start; min-height: 0; padding: .35rem .5rem;}
div[class*="st-key-lyst_pane_on_"] button {background: var(--lyst-raised); box-shadow: var(--lyst-shadow-1);
  font-weight: var(--lyst-weight-strong);}
div[class*="st-key-lyst_pane_on_"] button [data-testid="stIconMaterial"] {color: var(--lyst-accent);}
/* ---------------------------------------------------------------- the sidebar
   Calm by construction: the four areas lead, Pinned and Recent are quiet collapsible lists, and everything about who
   and where (workspace, account, search, notifications, glossary) lives in one compact foot at the bottom. */
[data-testid="stSidebarContent"] {display: flex; flex-direction: column;}
[data-testid="stSidebarNav"] {padding-top: .25rem;}
[data-testid="stSidebarNavLink"] span {font-size: var(--lyst-size-md, .95rem);}
[data-testid="stSidebarNavSeparator"] {display: none;}
[data-testid="stSidebarUserContent"] {flex: 1 0 auto; display: flex; flex-direction: column; padding-bottom: .75rem !important;
  padding-top: .5rem !important;}
[data-testid="stSidebarUserContent"] > div, [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {
  flex: 1 0 auto; display: flex; flex-direction: column;}
/* Pinned and Recent: a small quiet heading that folds, rows as muted one-line links */
div[class*="st-key-lyst_side_list_"] [data-testid="stExpander"] details {background: transparent !important;
  border: none !important; box-shadow: none !important;}
div[class*="st-key-lyst_side_list_"] [data-testid="stExpander"] summary {padding: .25rem .6rem; min-height: 0;
  background: transparent !important; border: none !important; box-shadow: none !important;
  font-size: var(--lyst-size-xs); letter-spacing: .06em; text-transform: uppercase; color: var(--lyst-text-muted);
  font-weight: var(--lyst-weight-medium);}
div[class*="st-key-lyst_side_list_"] [data-testid="stExpander"] summary:hover {color: var(--lyst-text);}
div[class*="st-key-lyst_side_list_"] [data-testid="stExpander"] summary [data-testid="stIconMaterial"] {font-size: 1rem;}
div[class*="st-key-lyst_side_list_"] [data-testid="stExpanderDetails"] {padding: 0 0 .25rem 0 !important;
  border: none !important;}
div[class*="st-key-lyst_side_list_"] [data-testid="stExpanderDetails"] [data-testid="stVerticalBlock"] {gap: 0;}
div[class*="st-key-lyst_recent_"] button {min-height: 0; padding: .3rem .6rem; justify-content: flex-start;
  text-align: left; font-size: var(--lyst-size-sm); color: var(--lyst-text-muted); border-radius: var(--lyst-radius-md);}
div[class*="st-key-lyst_recent_"] button:hover {background: var(--lyst-raised); color: var(--lyst-text);}
div[class*="st-key-lyst_recent_"] button p {overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
div[class*="st-key-lyst_recent_"] button [data-testid="stIconMaterial"] {font-size: 1rem;}
/* the foot: pinned to the bottom; its icons, badge, avatar and menus are styled in foot.py */
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(> .st-key-lyst_side_foot),
.st-key-lyst_side_foot {margin-top: auto;}
/* a hairline drawn as a shadow, so the surface observer does not raise the foot into a card */
.st-key-lyst_side_foot {padding-top: .6rem; box-shadow: inset 0 1px 0 var(--lyst-edge);}
.st-key-lyst_side_foot [data-testid="stPopoverButton"], .st-key-lyst_side_foot [data-testid^="stBaseButton"] {
  background: transparent !important; box-shadow: none !important; border-color: transparent !important;
  min-height: 2.25rem; border-radius: var(--lyst-radius-md);}
.st-key-lyst_side_foot [data-testid="stPopoverButton"]:hover, .st-key-lyst_side_foot [data-testid^="stBaseButton"]:hover {
  background: var(--lyst-raised) !important; box-shadow: var(--lyst-shadow-1) !important;}
/* icons only for the eye: the words stay in the accessible name (visually hidden, not removed) */
.st-key-lyst_side_icons [data-testid="stMarkdownContainer"], .st-key-lyst_side_icons kbd,
.st-key-lyst_side_icons [data-testid="stPopoverButton"] > div > span:last-child:not(:first-child) {
  position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap;}
/* ---------------------------------------------------------------- the rail
   On a desktop the four areas are a slim column of icons beside the page, not a sidebar of words: the mark on top,
   Home · Data · Brain · Settings, and the foot (search, notifications, glossary, account) at the bottom. Each icon
   names itself in a floating label on hover and focus; on a phone the menu opens as a sheet with its words. */
@media (min-width: 641px) {
  :root {--lyst-rail: 4.5rem;}
  section[data-testid="stSidebar"] {width: var(--lyst-rail) !important; min-width: var(--lyst-rail) !important;
    max-width: var(--lyst-rail) !important; transform: none !important; border-right: 0 !important;
    background: transparent !important; backdrop-filter: none;}
  section[data-testid="stSidebar"] > div:first-child {overflow: visible !important;}
  [data-testid="stSidebarCollapseButton"], [data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapsedControl"],
  section[data-testid="stSidebar"] [data-testid="stSidebarResizer"],
  section[data-testid="stSidebar"] > div > div[style*="cursor"] {display: none !important;}
  [data-testid="stSidebarHeader"] {padding: 1.35rem 0 .9rem !important; justify-content: center !important; height: auto !important;
    margin: 0 !important;}
  [data-testid="stSidebarHeader"] > div:first-child {margin: 0 auto;}
  img[data-testid="stSidebarLogo"] {height: 1.75rem !important; width: 1.75rem !important; max-width: none;}
  [data-testid="stSidebarContent"] {overflow: visible !important;}
  [data-testid="stSidebarNav"] {padding: .35rem 0 !important; display: flex; flex-direction: column; align-items: center;}
  [data-testid="stSidebarNavItems"] {padding: 0 !important; display: flex; flex-direction: column; align-items: center; gap: .3rem;}
  [data-testid="stSidebarNavLink"] {width: 2.85rem; height: 2.85rem; padding: 0 !important; margin: 0 !important;
    justify-content: center; align-items: center; border-radius: 14px; position: relative; overflow: visible;}
  [data-testid="stSidebarNavLink"] > span:first-child {margin: 0 !important;}
  [data-testid="stSidebarNavLink"] [data-testid="stIconMaterial"] {font-size: 1.32rem !important; color: var(--lyst-text-muted);}
  [data-testid="stSidebarNavLink"]:hover [data-testid="stIconMaterial"] {color: var(--lyst-text);}
  [data-testid="stSidebarNavLink"] > span[label] {position: absolute !important; width: 1px; height: 1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap;}
  [data-testid="stSidebarNavLink"][aria-current="page"] {background: var(--lyst-raised) !important; box-shadow: var(--lyst-shadow-2);}
  [data-testid="stSidebarNavLink"][aria-current="page"]::before {content: ""; position: absolute; left: -.72rem; top: 30%;
    bottom: 30%; width: 3px; border-radius: 3px; background: var(--lyst-accent);}
  [data-tip]::after {content: attr(data-tip); position: absolute; left: calc(100% + .7rem); top: 50%; translate: -4px -50%;
    padding: .3rem .6rem; border-radius: 8px; background: var(--lyst-text); color: var(--lyst-bg); font-size: var(--lyst-size-xs);
    font-weight: var(--lyst-weight-medium); white-space: nowrap; pointer-events: none; opacity: 0; z-index: 1000;
    transition: opacity var(--lyst-motion-fast) var(--lyst-motion-ease), translate var(--lyst-motion-base) var(--lyst-motion-ease);}
  [data-tip]:hover::after, [data-tip]:focus-visible::after {opacity: 1; translate: 0 -50%;}
  [data-testid="stSidebarUserContent"] {padding: 0 0 1rem 0 !important;}
  [data-testid="stSidebarUserContent"] [data-testid="stVerticalBlock"] {align-items: center;}
  .st-key-lyst_side_foot {box-shadow: none; padding-top: 0; align-items: center !important;}
  .st-key-lyst_side_icons {align-items: center !important; width: 100% !important;}
  .block-container {animation: lyst-page var(--lyst-motion-slow) var(--lyst-motion-ease);}
}
@keyframes lyst-page {from {opacity: 0; transform: translateY(4px);}}
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
  // the rail: each icon carries its area's name as a floating label (the accessible name is already the link text)
  const tips = () => document.querySelectorAll('[data-testid="stSidebarNavLink"]:not([data-tip])').forEach(a => {
    const t = (a.querySelector('[label]')?.getAttribute('label') || a.getAttribute('aria-label') || a.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t) a.setAttribute('data-tip', t);
  });
  // the rail's foot: the same floating label, named from the button's own words (and its key, for search); kept
  // current, since the bell's count and the account's workspace change
  const footTips = () => document.querySelectorAll('.st-key-lyst_side_foot button').forEach(b => {
    const words = (b.querySelector('[data-testid="stMarkdownContainer"]')?.innerText || '').replace(/\\s+/g, ' ').trim();
    const key = (b.querySelector('kbd')?.innerText || '').replace(/\\s+/g, '');
    const t = key ? `${words} (${key})` : words;
    if (t && b.getAttribute('data-tip') !== t) b.setAttribute('data-tip', t);
  });
  new MutationObserver(() => requestAnimationFrame(() => { tips(); footTips(); }))
    .observe(document.body, {childList: true, subtree: true, characterData: true});
  tips(); footTips();
})();
</script>"""


def style() -> None:
    """The stylesheet, with the token block for the active theme (light or dark)."""
    from lyst.ui import agent, agent_bar, artifacts, foot, tokens, visuals, zone  # noqa: PLC0415

    try:
        mode = st.context.theme.type or "light"
    except Exception:  # noqa: BLE001 - outside a browser session
        mode = "light"
    css = _CSS.replace("<style>", "<style>\n" + tokens.css(mode) + "\n" + visuals.CSS + artifacts.CSS + agent.CSS
                       + zone.CSS + agent_bar.CSS, 1)
    st.html(css.replace("</style>", foot.CSS + "</style>"))  # last, so the rail's foot overrides the generic rules
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


def _md_text(text: object) -> str:
    """Markdown-safe text that may sit inside an HTML block: markdown stays, HTML tags are escaped. Inline code
    spans are left as they are: markdown already shows their content literally (``uv run lyst backup <folder>``)."""
    parts = re.split(r"(`[^`\n]*`)", md(text))
    return "".join(p if i % 2 else p.replace("<", "&lt;").replace(">", "&gt;") for i, p in enumerate(parts))


def row(title: str, meta: str = "", *, status: tuple[str, str] | None = None,
        actions: Sequence[tuple[str, str]] = (), key: str, divider: bool = True, slot: bool = True) -> str | None:
    """One list item. ``actions`` are (label, id) pairs shown as quiet buttons; returns the id clicked, if any.

    The status chip and the actions sit together in one group at the row's right edge, so the actions of every row
    in a list end in the same place whether or not a row has a chip. ``slot`` is kept for callers; it changes
    nothing now that there is no separate status column."""
    side = (1.4 if status else 0) + 1.1 * len(actions)
    cols = st.columns([6, side], vertical_alignment="center") if side else [st.container()]
    with cols[0]:
        # Markdown (bold, :material: icons) still works; raw HTML in a title from workspace data is shown as text
        st.markdown(f'<div class="lyst-row-title">\n\n{_md_text(title)}\n\n</div>', unsafe_allow_html=True)
        if meta:
            st.markdown(f'<div class="lyst-row-meta">\n\n{_md_text(meta)}\n\n</div>', unsafe_allow_html=True)
    clicked = None
    if side:
        group = cols[1].container(horizontal=True, horizontal_alignment="right", vertical_alignment="center",
                                  gap="small", wrap=False)
        if status:
            chip(status[0], status[1], group)
        for text, aid in actions:
            if group.button(text, key=f"{key}_{aid}", type="tertiary", width="content"):
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
    st.session_state.pop("_home", None)  # and Home's starters, focus and recent work
    st.session_state.pop("_agent", None)  # and what Lyst is doing (agent.state)
    st.session_state.pop("_zone", None)  # and the activity zone's cards
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
