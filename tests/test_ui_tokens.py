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
    assert ("Brain", "Needs you") in {(a, s) for a, s, _, _ in palette.places("approvals")}
    assert ("Data", "SQL") in {(a, s) for a, s, _, _ in palette.places("query")}
    assert {(a, s) for a, s, _, _ in palette.places("settings voice")} == {("Settings", "Workspace")}
    assert palette.places("") == []


def test_every_palette_place_is_a_real_page_and_section():
    for area, section, page, qp in palette.PLACES:
        src = (UI / page).read_text()
        if qp.get("view") == "work":  # Brain › Work: its views live in the work module Brain draws
            src = (UI / "work.py").read_text()
        if section:
            assert f'"{section}"' in src, (area, section)
            assert qp.get("work", qp["view"]) == section.lower()


def test_visuals_carry_a_text_equivalent_and_no_raw_svg():
    from lyst.ui import visuals  # noqa: PLC0415

    sp = visuals.spark([1, 2, 3, 2.5], label="Revenue, last 4 months")
    assert sp.startswith("<img") and 'alt="Revenue, last 4 months"' in sp  # st.html strips inline <svg>
    assert "not enough points" in visuals.spark([1], label="x")
    st_ = visuals.stacked([("Time 1", 1, "info"), ("Numbers 3", 3, "accent")], label="Columns by role")
    assert 'role="img"' in st_ and "Numbers 3 75%" in st_
    assert "Lineage of m: Source a.csv → Dataset A → Approved metric M" in visuals.flow(
        [("Source", "a.csv", ""), ("Dataset", "A", ""), ("Approved metric", "M", "")], label="Lineage of m")
    assert "↑" not in visuals.delta("0.0 pts", None) and "↓" in visuals.delta("-3.4%", False)
    assert not re.findall(r"#[0-9a-fA-F]{6}\b", visuals.CSS)


def test_sql_result_chart_picks_a_sensible_first_chart():
    import ast  # noqa: PLC0415

    src = (UI / "app_pages/data.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_result_chart")
    ns: dict = {"pd": __import__("pandas")}
    exec(compile(ast.Module([fn], []), "data.py", "exec"), ns)  # noqa: S102 - the pure function only
    c = ns["_result_chart"](["region", "revenue"], [["EMEA", 2.0], ["APAC", 5.0]])
    assert c["type"] == "bar" and c["x"] == ["APAC", "EMEA"]
    c = ns["_result_chart"](["month", "v"], [["2026-01-01", 1], ["2026-02-01", 2]])
    assert c["type"] == "line"
    assert ns["_result_chart"](["a"], [[1]]) is None


def test_a_breakdown_answer_gets_a_bar_chart_in_summary():
    from lyst.ui.components import breakdown_chart  # noqa: PLC0415

    r = {"answer": "Revenue was $25.41M", "charts": [], "tables": [
        {"title": "Revenue by region", "columns": ["region", "value"], "rows": [["EMEA", 7.4e6], ["APAC", 5.7e6]]}]}
    c = breakdown_chart(r)
    assert c["type"] == "bar" and c["x"] == ["EMEA", "APAC"] and c["format"] == "currency"
    assert breakdown_chart({**r, "charts": [{"type": "line"}]}) is None  # its own chart wins
    assert breakdown_chart({"tables": [{"columns": ["a", "b", "c"], "rows": [[1, 2, 3]]}]}) is None


def test_material_tokens_and_surfaces():
    for mode in ("light", "dark"):
        c = tokens.colors(mode)
        for k in ("raised", "material", "edge", "shadow-1", "shadow-3"):
            assert c[k]
        assert tokens.contrast(c["text-muted"], c["raised"]) >= 4.5
    kit = (UI / "kit.py").read_text()
    assert "data-lyst-surface" in kit and "prefers-reduced-motion" in tokens.css()


def test_evidence_rail_shows_checks_data_and_expertise_without_claiming_more():
    from streamlit.testing.v1 import AppTest  # noqa: PLC0415

    def app():
        from lyst.ui.components import evidence_rail  # noqa: PLC0415

        evidence_rail({"confidence": {"checks": [{"name": "a", "passed": True}, {"name": "b", "passed": False}]},
                       "supporting": {"sources": [{"dataset": "pnl", "version": 1, "rows": 396}],
                                      "expertise": {"disciplines": [{"name": "FP&A"}], "note": "Rules from FP&A",
                                                    "rules": [{"id": "r1", "status": "passed"},
                                                              {"id": "r2", "status": "not_automated"}]},
                                      "assumptions": ["Periods are anchored"], "queries": [{"sql": "SELECT 1"}]}})

    at = AppTest.from_function(app)
    at.run(timeout=30)
    assert not at.exception
    html = " ".join(str(e.proto) for e in at.get("html"))
    assert "1 of 2 passed" in html and "pnl" in html and "FP&amp;A" in html and "not automated 1" in html


def test_metric_tile_says_good_or_bad_by_the_measure():
    from lyst.ui import visuals  # noqa: PLC0415

    t = visuals.metric_tile({"title": "COGS ratio", "value_text": "45.7%", "change_text": "+0.1 pts", "good": False,
                             "history": [1, 2, 3], "grain": "month"})
    assert "45.7%" in t and "--lyst-bad" in t and "alt=" in t
