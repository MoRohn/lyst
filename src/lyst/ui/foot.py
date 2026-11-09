# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The foot of the rail: search, notifications, help, and the account.

On a desktop the foot is the same column of square icons as Home · Data · Brain · Settings above it: the same size,
the same centre line, the same hover and the same floating label. The account is an avatar with the person's
initials, set a little apart. Each icon opens a small menu beside the rail rather than over it.

On a phone the rail is a sheet with words, so each icon becomes a row with its name (and the bell its count).
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version

import streamlit as st

from lyst.ui import glossary, kit

SETTINGS = "app_pages/settings.py"

# The keys the agent bar and the palette already answer to, said the way the person presses them.
SHORTCUTS = (("⌘ K", "Search, go anywhere, or ask"), ("/", "Ask Lyst from any page"), ("Esc", "Close what is open"))


def initials(who: str) -> str:
    """Two letters for an avatar: the first and last word of a name, or the start of an email's local part."""
    base = who.split("@")[0] if "@" in who and " " not in who.strip() else who
    parts = [p for p in re.split(r"[\s._+-]+", base) if p and p[0].isalnum()]
    if not parts:
        return "?"
    two = parts[0][0] + (parts[-1][0] if len(parts) > 1 else parts[0][1:2])
    return two.upper()


def _css_text(s: str) -> str:
    """A string as a CSS `content` value, every character escaped, so a name can never break out of the rule."""
    return '"' + "".join(f"\\{ord(c):x} " for c in s) + '"'


def _lyst_version() -> str:
    try:
        return version("lyst")
    except PackageNotFoundError:  # pragma: no cover - running from a source tree without an install
        return ""


def badge(count: int) -> None:
    """The bell's unread count, drawn on the icon (9+ past nine). Nothing unread draws nothing."""
    if count:
        st.html(f"<style>.st-key-lyst_side_bell_new [data-testid='stPopoverButton']::before "
                f"{{content: {_css_text('9+' if count > 9 else str(count))};}}</style>")


def help_menu(spaces: list[dict]) -> None:
    """The "?": the ways around Lyst (search, the keys) and what its words mean."""
    from lyst.ui import palette  # noqa: PLC0415 - the palette imports the whole interface

    with st.popover("Help", icon=":material/help:", key="lyst_side_help"):
        ver = _lyst_version()
        st.html('<div class="lyst-foot-pop lyst-foot-head"><b>Help</b>'
                '<span>Find your way around, and what Lyst\'s words mean.</span></div>')
        if st.button("Search or go to", icon=":material/search:", key="lyst_help_search", type="tertiary",
                     width="stretch"):
            palette.palette(spaces)
        rows = "".join(f'<li><kbd>{kit._esc(k)}</kbd><span>{kit._esc(v)}</span></li>' for k, v in SHORTCUTS)
        st.html(f'<p class="lyst-foot-label">Keys</p><ul class="lyst-foot-keys">{rows}</ul>')
        st.html(f"<style>{glossary.CSS}</style>" + glossary.html(title=False))
        if ver:
            st.html(f'<p class="lyst-foot-ver">Lyst {kit._esc(ver)} · AGPL-3.0</p>')


def account_menu(me: dict, spaces: list[dict], ws: str, role: str, sign_out) -> None:
    """The avatar: who you are and where, switching workspace, your settings, and signing out."""
    who = me.get("name") or me["email"]
    here = next((w for w in spaces if w["id"] == ws), {"name": ws})
    st.html(f"<style>.st-key-lyst_side_account [data-testid='stPopoverButton']::before "
            f"{{content: {_css_text(initials(who))};}}</style>")
    with st.popover(f"{who} · {here['name']}", key="lyst_side_account"):
        email = f'<span>{kit._esc(me["email"])}</span>' if who != me["email"] else ""
        st.html(f'<div class="lyst-foot-pop lyst-foot-me">'
                f'<span class="lyst-foot-avatar" aria-hidden="true">{kit._esc(initials(who))}</span>'
                f'<span class="lyst-foot-id"><b>{kit._esc(who)}</b>{email}'
                f'<span class="lyst-foot-role"><i>{kit._esc(role.capitalize())}</i>{kit._esc(here["name"])}</span>'
                f'</span></div>')
        if len(spaces) > 1:
            st.html('<p class="lyst-foot-label">Switch workspace</p>')
            for w in spaces:
                on = w["id"] == ws
                if st.button(w["name"], key=f"lyst_side_ws_{w['id']}", type="tertiary", width="stretch", disabled=on,
                             icon=":material/check:" if on else ":material/apartment:"):
                    st.session_state["_switch_ws"] = w["id"]  # app.py applies it at the top of the next run
                    st.rerun()
        st.html('<hr class="lyst-foot-rule">')
        st.page_link(SETTINGS, label="Account", icon=":material/manage_accounts:", query_params={"view": "account"},
                     width="stretch")
        st.page_link(SETTINGS, label="Preferences", icon=":material/tune:", query_params={"view": "preferences"},
                     width="stretch")
        st.html('<hr class="lyst-foot-rule">')
        if st.button("Sign out", type="tertiary", icon=":material/logout:", key="lyst_side_signout", width="stretch"):
            sign_out()


CSS = """
/* ---------------------------------------------------------------- the rail's foot
   One column with the four areas above it: the same square, the same centre line, the same hover, the same label. */
.st-key-lyst_side_foot {gap: .3rem !important;}
.st-key-lyst_side_icons {gap: .3rem !important;}
.st-key-lyst_side_foot [data-testid="stPopoverButton"], .st-key-lyst_side_foot [data-testid^="stBaseButton"] {
  position: relative; overflow: visible; color: var(--lyst-text-muted);
  transition: background var(--lyst-motion-fast) var(--lyst-motion-ease), color var(--lyst-motion-fast),
    box-shadow var(--lyst-motion-fast);}
.st-key-lyst_side_foot [data-testid="stPopoverButton"]:hover, .st-key-lyst_side_foot [data-testid^="stBaseButton"]:hover {
  color: var(--lyst-text);}
.st-key-lyst_side_foot button:focus-visible {outline: 2px solid var(--lyst-focus) !important; outline-offset: 2px;}
/* a menu that is open reads like the page you are on: raised, its icon in the accent */
.st-key-lyst_side_foot [data-testid="stPopoverButton"][aria-expanded="true"] {background: var(--lyst-raised) !important;
  box-shadow: var(--lyst-shadow-2) !important; color: var(--lyst-text);}
.st-key-lyst_side_icons [data-testid="stPopoverButton"][aria-expanded="true"] [data-testid="stIconMaterial"] {
  color: var(--lyst-accent);}
/* the unread count: a small accent pill on the bell's shoulder, ringed in the page colour so it reads on any surface */
.st-key-lyst_side_bell_new [data-testid="stPopoverButton"]::before {position: absolute; top: .2rem; right: .15rem;
  min-width: 1.05rem; height: 1.05rem; padding: 0 .28rem; box-sizing: border-box; border-radius: 999px;
  background: var(--lyst-accent); color: #fff; font-size: .64rem; font-weight: 700; line-height: 1.05rem;
  text-align: center; font-variant-numeric: tabular-nums; box-shadow: 0 0 0 2px var(--lyst-bg); pointer-events: none;}
/* the account: an avatar with initials, in the mark's own green */
.st-key-lyst_side_account [data-testid="stPopoverButton"]::before {flex: 0 0 auto; width: 2rem; height: 2rem;
  border-radius: 50%; display: grid; place-items: center; font-size: .78rem; font-weight: 650; letter-spacing: .02em;
  color: #fff; background: linear-gradient(135deg, var(--lyst-mark-floor-a), var(--lyst-mark-stem-a));
  box-shadow: 0 0 0 1px var(--lyst-edge) inset;}
.st-key-lyst_side_account [data-testid="stPopoverButton"] div[aria-hidden="true"] {display: none;}
.st-key-lyst_side_account [data-testid="stPopoverButton"][aria-expanded="true"]::before,
.st-key-lyst_side_account [data-testid="stPopoverButton"]:focus-visible::before {
  box-shadow: 0 0 0 2px var(--lyst-bg), 0 0 0 4px var(--lyst-accent);}
/* the menus themselves */
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) {min-width: 17rem; padding: .85rem .7rem .7rem !important;}
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) [data-testid="stVerticalBlock"] {gap: .15rem;}
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) button[data-testid="stBaseButton-tertiary"],
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) a[data-testid="stPageLink-NavLink"] {justify-content: flex-start;
  min-height: 2.2rem; padding: .35rem .6rem; border-radius: var(--lyst-radius-md); color: var(--lyst-text);
  text-decoration: none; margin: 0;}
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) button[data-testid="stBaseButton-tertiary"] > div {
  justify-content: flex-start; width: 100%;}
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) button[data-testid="stBaseButton-tertiary"]:hover:not(:disabled),
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) a[data-testid="stPageLink-NavLink"]:hover {
  background: var(--lyst-raised); box-shadow: var(--lyst-shadow-1);}
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) button[data-testid="stBaseButton-tertiary"]:disabled {
  opacity: 1; color: var(--lyst-text); font-weight: var(--lyst-weight-medium); cursor: default;}
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) [data-testid="stIconMaterial"] {color: var(--lyst-text-muted);
  font-size: 1.1rem;}
[data-testid="stPopoverBody"]:has(.lyst-foot-pop) button:disabled [data-testid="stIconMaterial"] {color: var(--lyst-accent);}
.lyst-foot-pop p {margin: 0;}
[data-testid="stPopoverBody"]:has(.lyst-foot-bell) {width: min(26rem, calc(100vw - 2rem)) !important;
  max-width: min(26rem, calc(100vw - 2rem)) !important;}
.lyst-foot-head {display: flex; flex-direction: column; gap: .15rem; padding: 0 .6rem .5rem;
  font-size: var(--lyst-size-sm); color: var(--lyst-text-muted); line-height: 1.4;}
.lyst-foot-head b {font-size: var(--lyst-size-md); color: var(--lyst-text); font-weight: var(--lyst-weight-strong);}
.lyst-foot-head .lyst-foot-count {color: var(--lyst-accent); font-weight: var(--lyst-weight-medium);}
.lyst-foot-label {padding: .55rem .6rem .2rem; font-size: var(--lyst-size-xs); letter-spacing: .06em;
  text-transform: uppercase; color: var(--lyst-text-muted); font-weight: var(--lyst-weight-medium);}
hr.lyst-foot-rule {border: 0; height: 1px; margin: .45rem .3rem; background: var(--lyst-border);}
.lyst-foot-me {display: flex; gap: .75rem; align-items: center; padding: .1rem .6rem .6rem;}
.lyst-foot-avatar {flex: 0 0 auto; width: 2.75rem; height: 2.75rem; border-radius: 50%; display: grid; place-items: center;
  font-weight: 650; font-size: 1.02rem; color: #fff;
  background: linear-gradient(135deg, var(--lyst-mark-floor-a), var(--lyst-mark-stem-a));}
.lyst-foot-id {display: flex; flex-direction: column; min-width: 0; font-size: var(--lyst-size-sm); line-height: 1.35;
  color: var(--lyst-text-muted);}
.lyst-foot-id > * {overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
.lyst-foot-id b {color: var(--lyst-text); font-size: var(--lyst-size-md); font-weight: var(--lyst-weight-strong);}
.lyst-foot-role {display: flex; gap: .4rem; align-items: center; margin-top: .25rem;}
.lyst-foot-role i {font-style: normal; font-size: var(--lyst-size-xs); font-weight: var(--lyst-weight-medium);
  padding: .05rem .45rem; border-radius: 999px; color: var(--lyst-accent);
  background: color-mix(in srgb, var(--lyst-accent) 14%, transparent);}
ul.lyst-foot-keys {list-style: none; margin: 0 0 .35rem; padding: 0 .6rem; display: grid; gap: .35rem;
  font-size: var(--lyst-size-sm); color: var(--lyst-text-muted);}
ul.lyst-foot-keys li {display: flex; align-items: center; gap: .6rem;}
ul.lyst-foot-keys kbd {min-width: 2.6rem; text-align: center; padding: .1rem .4rem; border-radius: 6px;
  font: 600 var(--lyst-size-xs)/1.4 inherit; color: var(--lyst-text); background: var(--lyst-raised);
  box-shadow: var(--lyst-shadow-1), 0 0 0 1px var(--lyst-edge) inset;}
.lyst-gloss-sub {padding: 0 .6rem;}
.lyst-gloss-sub > .lyst-foot-label {padding-left: 0;}
.lyst-foot-ver {padding: .6rem .6rem 0; font-size: var(--lyst-size-xs); color: var(--lyst-text-muted);}
/* the notifications list */
.lyst-foot-empty {display: flex; flex-direction: column; align-items: center; gap: .3rem; padding: 1rem .6rem 1.1rem;
  text-align: center; font-size: var(--lyst-size-sm); color: var(--lyst-text-muted);}
.lyst-foot-empty .lyst-foot-ic {font-family: "Material Symbols Rounded"; font-size: 1.6rem; color: var(--lyst-accent);
  width: 2.6rem; height: 2.6rem; display: grid; place-items: center; border-radius: 50%;
  background: color-mix(in srgb, var(--lyst-accent) 12%, transparent);}
.lyst-foot-empty b {color: var(--lyst-text); font-weight: var(--lyst-weight-medium);}
div[class*="st-key-lyst_note_"] {padding: .4rem .5rem .4rem .6rem; border-radius: var(--lyst-radius-md);}
div[class*="st-key-lyst_note_"] [data-testid="stColumn"]:first-child p {color: var(--lyst-text);}
div[class*="st-key-lyst_note_"]:hover {background: var(--lyst-raised);}
div[class*="st-key-lyst_note_"] [data-testid="stMarkdownContainer"] p {font-size: var(--lyst-size-sm); line-height: 1.4;}
div[class*="st-key-lyst_note_"] [data-testid="stColumn"]:first-child [data-testid="stMarkdownContainer"] > p:first-child::before {content: ""; display: inline-block;
  width: .45rem; height: .45rem; border-radius: 50%; background: var(--lyst-accent); margin-right: .45rem;
  vertical-align: .08rem;}
@media (min-width: 641px) {
  /* the same 2.85rem square as the areas above, on the rail's centre line */
  [data-testid="stSidebarUserContent"] {width: 100% !important; padding-left: 0 !important; padding-right: 0 !important;}
  .st-key-lyst_side_foot {width: 100% !important; padding-top: .75rem !important; position: relative;}
  .st-key-lyst_side_foot [data-testid="stElementContainer"], .st-key-lyst_side_foot [data-testid="stLayoutWrapper"],
  .st-key-lyst_side_foot .stPopover, .st-key-lyst_side_foot .stButton,
  .st-key-lyst_side_foot [data-testid="stTooltipHoverTarget"], .st-key-lyst_side_foot .stTooltipIcon {
    width: auto !important; display: flex !important; justify-content: center;}
  .st-key-lyst_side_foot [data-testid="stPopoverButton"], .st-key-lyst_side_foot [data-testid^="stBaseButton"] {
    width: 2.85rem !important; min-width: 2.85rem !important; height: 2.85rem !important; min-height: 2.85rem !important;
    padding: 0 !important; border-radius: 14px !important; justify-content: center !important;}
  .st-key-lyst_side_foot [data-testid="stPopoverButton"] > div, .st-key-lyst_side_foot [data-testid^="stBaseButton"] > div {
    justify-content: center !important; width: auto !important; margin: 0 !important;}
  .st-key-lyst_side_icons [data-testid="stIconMaterial"] {font-size: 1.32rem !important;}
  .st-key-lyst_side_icons [data-testid="stPopoverButton"] div[aria-hidden="true"] {display: none !important;}
  /* the account sits a little apart, under a short hairline, as the person rather than a tool */
  .st-key-lyst_side_foot > [data-testid="stLayoutWrapper"]:has(.st-key-lyst_side_account),
  .st-key-lyst_side_foot > .st-key-lyst_side_account {margin-top: .45rem; padding-top: .55rem;
    box-shadow: inset 0 1px 0 var(--lyst-border); width: 2.2rem !important; overflow: visible;}
  .st-key-lyst_side_account [data-testid="stPopoverButton"],
  .st-key-lyst_side_account [data-testid="stPopoverButton"]:hover,
  .st-key-lyst_side_account [data-testid="stPopoverButton"][aria-expanded="true"] {background: transparent !important;
    box-shadow: none !important;}
  .st-key-lyst_side_account [data-testid="stPopoverButton"]:hover::before {filter: brightness(1.08);}
  .st-key-lyst_side_account [data-testid="stPopoverButton"] [data-testid="stMarkdownContainer"] {
    position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap;}
  .st-key-lyst_side_foot button[aria-expanded="true"][data-tip]::after {opacity: 0 !important;}
  /* its menus open beside the rail, bottom-aligned with the icon, never over it */
  [data-testid="stPopoverBody"]:has(.lyst-foot-pop) {translate: calc(var(--lyst-rail) - .4rem) 3.2rem;}
}
@media (max-width: 640px) {
  /* the sheet: each icon a row with its words, all starting at the same left edge as the areas above */
  .st-key-lyst_side_foot [data-testid="stPopoverButton"], .st-key-lyst_side_foot [data-testid^="stBaseButton"] {
    width: 100% !important; justify-content: flex-start !important; padding: .45rem .6rem !important; min-height: 2.75rem;}
  .st-key-lyst_side_foot button div, .st-key-lyst_side_foot button > div > span,
  .st-key-lyst_side_foot button > div > div > span {justify-content: flex-start !important; min-width: 0;}
  .st-key-lyst_side_foot button > div, .st-key-lyst_side_foot button > div > div:first-child,
  .st-key-lyst_side_foot button > div > span:first-child, .st-key-lyst_side_foot button > div > div:first-child > span {
    flex: 1 1 auto; width: 100%; gap: .75rem;}
  .st-key-lyst_side_foot [data-testid="stPopoverButton"] div[aria-hidden="true"] {display: none !important;}
  .st-key-lyst_side_icons [data-testid="stMarkdownContainer"], .st-key-lyst_side_icons kbd,
  .st-key-lyst_side_icons [data-testid="stPopoverButton"] > div > span:last-child:not(:first-child) {
    position: static !important; width: auto; height: auto; clip: auto; overflow: visible;}
  .st-key-lyst_side_icons kbd {margin-left: auto; font-size: var(--lyst-size-xs); color: var(--lyst-text-muted);}
  .st-key-lyst_side_foot [data-testid="stMarkdownContainer"] p {font-size: var(--lyst-size-md); color: var(--lyst-text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
  .st-key-lyst_side_foot [data-testid="stIconMaterial"] {font-size: 1.25rem; color: var(--lyst-text-muted);}
  .st-key-lyst_side_foot [data-testid="stLayoutWrapper"], .st-key-lyst_side_foot .stPopover,
  .st-key-lyst_side_foot [data-testid="stElementContainer"], .st-key-lyst_side_foot .stButton {width: 100% !important;}
  /* on the sheet the bell's words already say how many */
  .st-key-lyst_side_bell_new [data-testid="stPopoverButton"]::before {display: none;}
  .st-key-lyst_side_account [data-testid="stPopoverButton"] {gap: .75rem; margin-top: .35rem;
    box-shadow: inset 0 1px 0 var(--lyst-border) !important; border-radius: 0 !important; padding-top: .9rem !important;}
  .st-key-lyst_side_account [data-testid="stPopoverButton"] > div {width: calc(100% - 2.75rem);}
}
"""
