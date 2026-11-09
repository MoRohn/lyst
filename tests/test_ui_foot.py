# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The rail's foot: initials for the avatar, a count that cannot break out of its rule, and one CSS for both widths."""

from lyst.ui import foot, glossary


def test_initials_read_like_a_person():
    assert foot.initials("Avery Chen") == "AC"
    assert foot.initials("avery@northwind.test") == "AV"
    assert foot.initials("dana.ortiz@northwind.test") == "DO"
    assert foot.initials("Mary Ann van Dyke") == "MD"
    assert foot.initials("Zoë") == "ZO"
    assert foot.initials("  ") == "?"


def test_css_text_escapes_every_character():
    out = foot._css_text('"};body{display:none')
    assert out.startswith('"') and out.endswith('"')
    assert "}" not in out[1:-1] and ";" not in out[1:-1] and '"' not in out[1:-1]


def test_foot_styles_the_rail_and_the_phone_sheet():
    assert "@media (min-width: 641px)" in foot.CSS and "@media (max-width: 640px)" in foot.CSS
    assert "2.85rem" in foot.CSS  # the same square as Home · Data · Brain · Settings
    assert "st-key-lyst_side_bell_new" in foot.CSS and "st-key-lyst_side_account" in foot.CSS


def test_glossary_can_sit_inside_help():
    assert "lyst-gloss-title" in glossary.html()
    sub = glossary.html(title=False)
    assert "lyst-gloss-title" not in sub and "lyst-gloss-sub" in sub and "What these words mean" in sub
