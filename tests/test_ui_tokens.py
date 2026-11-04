# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Design tokens, chart theme and the command palette's pure parts."""

from __future__ import annotations

import pathlib
import re

import pytest

from lyst.ui import palette, tokens

UI = pathlib.Path(__file__).parents[1] / "src/lyst/ui"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_text_and_status_colours_meet_wcag_aa(mode):
    c = tokens.colors(mode)
    for name in ("text", "text-muted", "link", "good", "warn", "bad", "info"):
        assert tokens.contrast(c[name], c["bg"]) >= 4.5, (mode, name, c[name])
        assert tokens.contrast(c[name], c["surface"]) >= 4.5, (mode, name, c[name])


def test_colours_come_from_the_streamlit_theme():
    # one source: the accent in the tokens is the accent Streamlit's own widgets use
    cfg = (pathlib.Path(__file__).parents[1] / ".streamlit/config.toml").read_text()
    assert f'primaryColor = "{tokens.colors("light")["accent"]}"' in cfg
    assert tokens.series("dark")[0] == tokens.theme("dark")["chartCategoricalColors"][0]


def test_css_exposes_every_token_and_honours_reduced_motion():
    css = tokens.css("dark")
    for var in ("--lyst-text-muted", "--lyst-space-4", "--lyst-radius-md", "--lyst-motion-base", "--lyst-size-xl",
                "--lyst-content-max", "--lyst-focus"):
        assert var + ":" in css
    assert "prefers-reduced-motion" in css


def test_the_stylesheet_uses_tokens_not_raw_colours():
    kit = (UI / "kit.py").read_text()
    sheet = kit[kit.index('_CSS = """'):kit.index('</style>')]
    assert not re.findall(r"#[0-9a-fA-F]{3,6}\b", sheet), "colours belong in tokens or the theme"
    assert "focus-visible" in sheet


def test_chart_layout_follows_the_theme():
    lay = tokens.plotly_layout("dark", fmt="percent")
    assert lay["colorway"][0] == tokens.series("dark")[0]
    assert lay["yaxis"]["tickformat"] == ".0%"
    assert lay["paper_bgcolor"] == "rgba(0,0,0,0)"


def test_chart_alt_text_describes_the_data():
    from lyst.ui.components import _alt  # noqa: PLC0415

    alt = _alt({"type": "line", "title": "Operating margin by month", "format": "percent",
                "x": ["2026-07", "2026-08", "2026-09"], "y": [0.36, 0.33, 0.325]})
    assert "Operating margin by month" in alt and "last 32.5%" in alt and "3 points" in alt


def test_palette_finds_places_by_name_and_by_the_words_people_use():
    assert ("Work", "Needs you") in {(a, s) for a, s, _, _ in palette.places("approvals")}
    assert ("Data", "SQL") in {(a, s) for a, s, _, _ in palette.places("query")}
    assert {(a, s) for a, s, _, _ in palette.places("settings voice")} == {("Settings", "Workspace")}
    assert palette.places("") == []


def test_every_palette_place_is_a_real_page_and_section():
    for area, section, page, qp in palette.PLACES:
        src = (UI / page).read_text()
        if section:
            assert f'"{section}"' in src, (area, section)
            assert qp["view"] == section.lower()
