# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The glossary behind the rail's "?": every word is a card with an icon, one short line, and more on demand."""

from lyst.ui import glossary


def test_every_word_has_an_icon_a_short_line_and_more():
    words = [t for _, terms in glossary.GROUPS for t in terms]
    assert {t.word for t in words} >= {"Approved measure", "Verified", "Depth", "Brief", "Watch a metric", "Repeat it",
                                       "Saved analysis", "Needs you", "Brain", "Skill"}
    for t in words:
        assert t.icon and t.icon.replace("_", "").isalpha()
        assert len(t.line) <= 40, t.word  # one short line at a glance
        assert t.more and t.more != t.line


def test_html_is_cards_that_open_and_escapes_text():
    html = glossary.html()
    n = sum(len(terms) for _, terms in glossary.GROUPS)
    assert html.count('<details class="lyst-gloss-item">') == n
    assert html.count("<summary>") == n
    assert 'aria-hidden="true"' in html  # icons are for the eye only
    assert "Settings › Advanced" in html
    assert "<script" not in html
