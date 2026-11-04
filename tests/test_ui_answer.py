# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The answer component, rendered headlessly: four depths from one result, caveats always visible, and
currency never turned into LaTeX."""

import json

from streamlit.testing.v1 import AppTest

from .conftest import ask


def _render(result, view=None):
    def app(result_json, view):
        import json as _j

        import streamlit as st

        from lyst.ui.components import answer

        if view:
            st.session_state.setdefault("view_pref", view)
        answer(_j.loads(result_json), key="t")

    at = AppTest.from_function(app, args=(json.dumps(result, default=str), view))
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def test_views_change_depth_without_rerunning(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Why did operating margin decline in Q4 2025?")["result"]  # wrong premise: a caveat
    business = _render(r, "Business")
    text = " ".join(m.value for m in business.markdown)
    assert "did not decline" in business.markdown[0].value
    assert any("assumed a decline" in w.value for w in business.warning)  # limitation visible at every depth
    assert "**Evidence**" in text and not business.dataframe[1:]  # tables stay hidden for business readers
    engineer = _render(r, "Engineer")
    assert len(engineer.code) == len(r["supporting"]["queries"])  # every query, with its SQL
    executive = _render(r, "Executive")
    assert "**Evidence**" not in " ".join(m.value for m in executive.markdown)


def test_currency_is_escaped(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was revenue in Q3 2026?")["result"]
    at = _render(r, "Business")
    assert "\\$25.41M" in at.markdown[0].value
