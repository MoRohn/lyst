# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Brain › Graph renders its map, a run with its branches and final check, and the harness, without raising."""

from streamlit.testing.v1 import AppTest

from lyst.ui.brain_graph import palette

from .test_ui_pages import _texts, ui  # noqa: F401  (fixture)


def _brain(at, **params):
    at.switch_page("app_pages/brain.py")
    for k, v in params.items():
        at.query_params[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_the_brain_opens_on_its_graph(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    _brain(at)
    assert any(t.label == "Show graph runs" for t in at.toggle)
    assert "Every dataset, approved metric" in _texts(at)


def test_runs_and_harness_render_a_finished_run(ui):  # noqa: F811
    lyst = ui
    from lyst.security import Principal  # noqa: PLC0415

    row = lyst.stores.control.query("SELECT id, email FROM users WHERE email = 'avery@northwind.test'")[0]
    owner = Principal(row["id"], row["email"])
    run = lyst.graphs.plan(owner, "northwind", "What was revenue last quarter?\nWhich region has the lowest gross "
                                               "margin?", max_attempts=2)
    lyst.graphs.go(owner, "northwind", run["id"])
    for _ in range(6):
        lyst.runtime.drain()
        lyst.graphs.advance("northwind", run["id"])
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    _brain(at)
    at.session_state["bg_tab"] = "Runs"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "What the graph learned" in body and "Merged answers" in body
    at.session_state["bg_tab"] = "Harness"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "Learned rules · 1" in body and "Evidence, strongest first" in body


def test_the_canvas_palette_keeps_status_colours_for_health():
    for mode in ("light", "dark"):
        p = palette(mode)
        status = {p["colors"][k] for k in ("good", "warn", "bad", "info")}
        assert not status & set(p["kinds"].values())  # a kind is never painted in a health colour
        assert p["kinds"]["metric"] == p["colors"]["accent"] and p["kinds"]["dataset"] == p["colors"]["neutral"]


def test_a_link_to_a_metric_still_opens_its_definition(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    _brain(at, metric="revenue")
    assert at.query_params.get("view") in ("definitions", ["definitions"])
