# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Business-user defaults (docs/UX_STRATEGY.md, Part A): business depth first, trust explained in words, every
recommendation a button, plain words instead of system identifiers."""

import json

from streamlit.testing.v1 import AppTest

from lyst.ui.components import heading, trust_line

from .conftest import ask
from .test_ui_pages import _texts, ui  # noqa: F401, F811  (fixture)


def _popovers(block):
    if getattr(block, "type", None) == "popover":
        yield block.proto.popover.label
    for child in getattr(block, "children", {}).values():
        yield from _popovers(child)


def _answer(result, role="owner"):
    def app(result_json, role):
        import json as _j

        import streamlit as st

        from lyst.ui.components import answer

        st.session_state.role = role
        answer(_j.loads(result_json), key="t")

    at = AppTest.from_function(app, args=(json.dumps(result, default=str), role))
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def test_owners_open_answers_at_the_business_depth_with_trust_in_words(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Why did operating margin decline in Q3 2026?")["result"]
    at = _answer(r, role="owner")
    view = at.get("button_group")[0]
    assert view.value == "Summary"
    assert not at.dataframe[1:]  # only the checks (inside the collapsed method); tables wait for a deeper view
    captions = " ".join(c.value for c in at.caption)
    assert "checks passed" in captions and "recomputed independently" in captions
    assert "**What it means**" in " ".join(m.value for m in at.markdown)


def test_trust_line_and_headings():
    assert trust_line({"status": "complete", "confidence": {"level": "verified", "checks": [{"passed": True}] * 3}}) \
        .startswith("All 3 checks passed")
    assert "1 of 2" in trust_line({"status": "complete", "confidence": {"level": "partial",
                                                                         "checks": [{"passed": True}, {"passed": False}]}})
    assert heading("prior_rate") == "Prior rate" and heading("region") == "Region"


def test_an_answer_offers_watch_repeat_and_download(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    rows = [b for b in at.button if b.label == "Open"]
    assert rows
    rows[-1].click().run()  # the verified margin answer from the fixture
    assert not at.exception, [e.value for e in at.exception]
    labels = set(_popovers(at.main))
    assert {"Watch this metric", "Repeat it", "Download"} <= labels


def test_work_speaks_in_business_words(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/work.py")
    at.query_params["view"] = "recurring"
    at.run()
    assert not at.exception
    body = _texts(at)
    assert "Watched metrics" in body and "Scheduled questions" in body and "σ" not in body


def test_pulse_compares_complete_months_as_the_reader(seeded):
    lyst, owner = seeded
    tiles = lyst.monitors.pulse(owner, "northwind")
    assert tiles and len(tiles) <= 6
    m = next(t for t in tiles if t["metric"] == "operating_margin")
    assert m["format"] == "percent" and m["change_text"].endswith("pts")
    assert abs(m["change"] - (m["value"] - m["prior"])) < 1e-12 and len(m["history"]) <= 12
    rev = next(t for t in tiles if t["metric"] == "revenue")
    assert rev["change_text"].endswith("%") and rev["value_text"].startswith("$")


def test_home_guides_a_first_run(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    body = _texts(at)
    assert "Get started" in body and "Keep watch" in body
    assert "at a glance" in body  # the business pulse above the suggestions


def test_brain_shows_the_health_of_definitions(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [e.label for e in at.expander]
    assert any("definition issue" in lb or "small gap" in lb for lb in labels)  # the semantic audit, in one line


def test_data_rows_say_whether_a_table_can_be_relied_on(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "data through" in body  # freshness from the catalog, not just the load time
    # the demo ledger repeats a row at its grain: the list flags it before anyone opens it
    assert any("badge[" in m.value and "to fix" in m.value for m in at.markdown)


def test_search_results_open_where_each_thing_lives():
    import pathlib  # noqa: PLC0415

    src = pathlib.Path(__file__).parents[1] / "src/lyst/ui/app.py"
    code = src.read_text()
    start = code.index("def _destination")
    ns: dict = {}
    exec(code[start:code.index("@st.dialog(\"Search\"")], ns)  # noqa: S102  (the pure routing function only)
    dest = ns["_destination"]
    assert dest({"kind": "metric", "name": "revenue"}) == ("app_pages/brain.py", {"metric": "revenue"})
    assert dest({"kind": "column", "name": "pnl.region"}) == ("app_pages/data.py", {"dataset": "pnl"})
    assert dest({"kind": "analysis", "name": "t_1"}) == ("app_pages/analyst.py", {"task": "t_1"})
