# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What Lyst's words mean: the glossary behind the "?" at the foot of the rail.

Each word is a small card: an icon, the word, and one short line. Tapping the card opens one or two more sentences,
so the popover reads at a glance and the detail is there for whoever wants it. The icons are the same Material
Symbols the rest of the interface uses for the same things (Brain, Watch a metric, Repeat it, skills).
"""

from __future__ import annotations

from typing import NamedTuple

from lyst.ui.kit import _esc


class Term(NamedTuple):
    icon: str   # a Material Symbols Rounded name
    word: str
    line: str   # one short line, always shown
    more: str   # shown when the card is opened


GROUPS: tuple[tuple[str, tuple[Term, ...]], ...] = (
    ("Answers", (
        Term("straighten", "Approved measure", "A definition an admin approved.",
             "Such as what revenue means here. Lyst answers only from these, so two people asking the same question "
             "get the same number. Also called a metric."),
        Term("verified", "Verified", "Every figure was rechecked.",
             "Each figure was recomputed independently and every required check passed."),
        Term("layers", "Depth", "How much of the method you see.",
             "Summary is the answer and what to do. Detail adds the tables and what was tested. Method adds every "
             "check and query."),
        Term("summarize", "Brief", "Two lines and a PDF.",
             "The shortest form of an answer, ready to send to someone who was not in the room."),
    )),
    ("Lyst keeps working", (
        Term("monitor_heart", "Watch a metric", "Lyst looks into unusual moves.",
             "It checks the metric on its own. Sensitivity sets how unusual a move must be before it tells you."),
        Term("event_repeat", "Repeat it", "Asked again on a schedule.",
             "Every day, week or month, for the latest period."),
        Term("replay", "Saved analysis", "A verified answer to rerun.",
             "For any period, without Lyst working it out again. Also called a workflow."),
        Term("front_hand", "Needs you", "What is waiting on you.",
             "Approvals, questions Lyst needs answered, and results that did not pass verification."),
    )),
    ("What Lyst knows", (
        Term("psychology", "Brain", "What Lyst knows here.",
             "About this business: approved definitions, documents, and what it has learned."),
        Term("extension", "Skill", "A method a builder adds.",
             "Written in Python under Settings › Advanced, tested in a sandbox and approved before use."),
    )),
)


def _card(t: Term) -> str:
    return ('<details class="lyst-gloss-item"><summary>'
            f'<span class="lyst-gloss-ic" aria-hidden="true">{_esc(t.icon)}</span>'
            f'<span class="lyst-gloss-txt"><b>{_esc(t.word)}</b><span>{_esc(t.line)}</span></span>'
            '<span class="lyst-gloss-more" aria-hidden="true">expand_more</span>'
            f'</summary><p>{_esc(t.more)}</p></details>')


def html() -> str:
    """The glossary as one block of HTML for ``st.html``: groups of cards, each opening on its own."""
    groups = "".join(f'<section aria-label="{_esc(name)}"><p class="lyst-gloss-group">{_esc(name)}</p>'
                     f'<div class="lyst-gloss-grid">{"".join(_card(t) for t in terms)}</div></section>'
                     for name, terms in GROUPS)
    return ('<div class="lyst-gloss"><p class="lyst-gloss-title">What these words mean</p>'
            '<p class="lyst-gloss-hint">Tap a word for more.</p>' + groups + "</div>")


CSS = """
/* the glossary: a calm sheet of word cards, two across on a desktop, one on a phone */
[data-testid="stPopoverBody"]:has(.lyst-gloss) {width: min(44rem, calc(100vw - 2rem)) !important;
  max-width: min(44rem, calc(100vw - 2rem)) !important;}
.lyst-gloss {font-size: var(--lyst-size-sm); line-height: 1.4;}
.lyst-gloss p {margin: 0;}
.lyst-gloss-title {font-size: var(--lyst-size-md); font-weight: var(--lyst-weight-strong); color: var(--lyst-text);}
.lyst-gloss-hint {color: var(--lyst-text-muted); margin-top: .1rem !important;}
.lyst-gloss-group {text-transform: uppercase; letter-spacing: var(--lyst-tracking-label); font-size: var(--lyst-size-xs);
  font-weight: 600; color: var(--lyst-text-muted); margin: var(--lyst-space-4) 0 var(--lyst-space-2) .15rem !important;}
.lyst-gloss-grid {display: grid; grid-template-columns: 1fr 1fr; gap: var(--lyst-space-2); align-items: start;}
@media (max-width: 640px) {.lyst-gloss-grid {grid-template-columns: 1fr;}}
.lyst-gloss-item {border-radius: var(--lyst-radius-md); background: var(--lyst-surface);
  box-shadow: inset 0 0 0 1px var(--lyst-edge); transition: background var(--lyst-motion-fast) var(--lyst-motion-ease);}
.lyst-gloss-item:hover, .lyst-gloss-item[open] {background: var(--lyst-bg);
  box-shadow: inset 0 0 0 1px var(--lyst-border);}
.lyst-gloss-item summary {list-style: none; display: flex; align-items: center; gap: var(--lyst-space-3);
  padding: .55rem .6rem; cursor: pointer; border-radius: var(--lyst-radius-md);}
.lyst-gloss-item summary::-webkit-details-marker {display: none;}
.lyst-gloss-item summary:focus-visible {outline: 2px solid var(--lyst-focus); outline-offset: 1px;}
.lyst-gloss-ic, .lyst-gloss-more {font-family: "Material Symbols Rounded"; font-weight: normal; font-style: normal;
  font-feature-settings: "liga"; -webkit-font-smoothing: antialiased; line-height: 1; letter-spacing: normal;
  text-transform: none; white-space: nowrap; direction: ltr; user-select: none;}
.lyst-gloss-ic {flex: 0 0 2.1rem; height: 2.1rem; display: grid; place-items: center; border-radius: var(--lyst-radius-sm);
  font-size: 1.2rem; color: var(--lyst-link); background: color-mix(in srgb, var(--lyst-link) 13%, transparent);}
.lyst-gloss-txt {flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column;}
.lyst-gloss-txt b {font-weight: var(--lyst-weight-medium); color: var(--lyst-text); font-size: .92rem;}
.lyst-gloss-txt span {color: var(--lyst-text-muted); font-size: var(--lyst-size-sm);}
.lyst-gloss-more {font-size: 1.1rem; color: var(--lyst-text-muted); opacity: .6;
  transition: transform var(--lyst-motion-base) var(--lyst-motion-ease);}
.lyst-gloss-item[open] .lyst-gloss-more {transform: rotate(180deg); opacity: 1;}
.lyst-gloss-item > p {padding: 0 .75rem .7rem calc(2.1rem + var(--lyst-space-3) + .6rem); color: var(--lyst-text);
  font-size: var(--lyst-size-sm); line-height: 1.45;}
"""

__all__ = ["CSS", "GROUPS", "Term", "html"]
