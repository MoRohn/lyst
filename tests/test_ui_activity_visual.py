# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Automated visual checks for the activity kit, in light and dark, without a browser:

* golden markup: the stage timeline and the "why this happened" flow render exactly as reviewed (a change shows up as
  a diff of the golden file; regenerate with ``LYST_UPDATE_GOLDEN=1`` after looking at it);
* every class the kit emits is styled by the stylesheet every page loads, so nothing renders unstyled;
* every colour an activity state uses reads against both themes' backgrounds (WCAG 3:1 for marks and chips);
* every state has words and a tone, and every stage a label, so no state shows a raw code."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from lyst.ui import activity, artifacts, kit, tokens, visuals

GOLDEN = Path(__file__).parent / "golden" / "activity"

# one activity in every stage state the timeline can show
SAMPLE = {"id": "act_sample", "stages": [
    {"stage": "detect", "status": "done", "detail": "dataset.schema_changed (event 17)"},
    {"stage": "interpret", "status": "done", "detail": "data.added has 1 column"},
    {"stage": "qualify", "status": "done", "detail": "act"},
    {"stage": "plan", "status": "done", "detail": "review what depends on Monthly P&L"},
    {"stage": "authorize", "status": "awaiting_approval", "detail": "approval ap_1"},
    {"stage": "execute", "status": "failed", "detail": "the source <table> was gone"},
    {"stage": "verify", "status": "waiting", "detail": "task t_1"},
    {"stage": "deliver", "status": "skipped", "detail": ""},
]}
FLOW = [("Event", "dataset schema changed", ""), ("Rule", "Flag a schema change", "v2"), ("Decision", "act", ""),
        ("Work", "running", ""), ("Check", "pending", ""), ("Result", "—", "")]


def _golden(name: str, html: str) -> None:
    path = GOLDEN / f"{name}.html"
    if os.environ.get("LYST_UPDATE_GOLDEN") == "1" or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html + "\n", encoding="utf-8")
        if os.environ.get("LYST_UPDATE_GOLDEN") != "1":
            pytest.fail(f"{path} was missing and has been written; review it and commit it")
    assert html + "\n" == path.read_text(encoding="utf-8"), f"{name} changed; review, then LYST_UPDATE_GOLDEN=1"


def test_the_stage_timeline_and_flow_render_as_reviewed():
    _golden("stages", activity.stages_html(SAMPLE))
    _golden("flow", visuals.flow(FLOW, label="Why this happened"))


def test_the_markup_is_escaped_and_named_for_screen_readers():
    html = activity.stages_html(SAMPLE)
    assert "<table>" not in html and "&lt;table&gt;" in html  # recorded text never becomes markup
    assert 'aria-label="How it went: 4 of 8 done"' in html
    flow = visuals.flow(FLOW, label="Why this happened")
    assert 'role="img"' in flow and 'aria-label="Why this happened: Event dataset schema changed' in flow


def test_every_class_the_kit_emits_is_styled():
    sheet = kit._CSS + visuals.CSS + artifacts.CSS
    html = activity.stages_html(SAMPLE) + visuals.flow(FLOW, label="x")
    used = {c for attr in re.findall(r'class="([^"]+)"', html) for c in attr.split()}
    unstyled = sorted(c for c in used if f".{c}" not in sheet)
    assert not unstyled, f"classes with no style: {unstyled}"
    # and every colour variable they use is a defined token in both themes
    for mode in ("light", "dark"):
        defined = set(re.findall(r"(--lyst-[a-z0-9-]+):", tokens.css(mode)))
        missing = sorted(set(re.findall(r"var\((--lyst-[a-z0-9-]+)\)", html)) - defined)
        assert not missing, (mode, missing)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_state_colour_reads_on_both_themes(mode):
    c = tokens.colors(mode)
    tones = {tone for _, tone in visuals.STEP_MARK.values()}
    for tone in sorted(tones):
        for bg in ("bg", "surface"):
            ratio = tokens.contrast(c[tone], c[bg])
            assert ratio >= 3.0, f"{mode}: step mark {tone} on {bg} is {ratio:.2f}:1"


def test_every_state_and_stage_has_words_and_a_known_tone():
    from lyst.autonomy import STAGES

    statuses = {"observed", "suggested", "planned", "running", "awaiting_approval", "completed", "needs_attention",
                "failed", "blocked", "cancelled"}
    assert statuses <= set(activity.STATUS)
    for words, tone in activity.STATUS.values():
        assert words and not re.search(r"[_.]", words) and tone in kit.CHIPS
    assert set(STAGES) <= set(activity.STAGE)
    assert {m for m in activity.MARK.values()} <= set(visuals.STEP_MARK)
