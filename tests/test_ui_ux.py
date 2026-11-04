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
    assert view.value == "Business"
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


def test_home_guides_a_first_run(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    body = _texts(at)
    assert "Get started" in body and "Keep watch" in body
