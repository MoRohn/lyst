# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The answer component, rendered headlessly: three depths from one result, caveats always visible, and
currency never turned into LaTeX."""

import json

from streamlit.testing.v1 import AppTest

from lyst.ui.components import md

from .conftest import ask


def _render(result, view=None):  # view: kept for callers; every layer renders, details inside expanders
    def app(result_json):
        import json as _j

        from lyst.ui.components import answer

        answer(_j.loads(result_json), key="t")

    at = AppTest.from_function(app, args=(json.dumps(result, default=str),))
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def _head(at) -> str:
    return next(m.value for m in at.markdown if m.value.startswith("####"))


def test_the_result_leads_and_the_details_wait_one_click_away(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Why did operating margin decline in Q4 2025?")["result"]  # wrong premise: a caveat
    at = _render(r)
    assert "did not decline" in _head(at)  # the finding first
    assert any("assumed a decline" in w.value for w in at.warning)  # a limitation is never folded away
    labels = [e.label for e in at.expander]
    assert labels == ["Why, and the details", "Method, checks and queries"]
    assert all(not e.proto.expanded for e in at.expander)  # layers 2 and 3 start closed
    details, method = at.expander
    assert "**Evidence**" in " ".join(m.value for m in details.markdown)
    assert len(method.code) == len(r["supporting"]["queries"])  # every query, with its SQL, in layer 3
    executive = _render({**r, "audience": "executive"})  # an executive asked: the brief leads
    assert r.get("brief") and _head(executive) == "#### " + md(r["brief"])


def test_currency_is_escaped(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was revenue in Q3 2026?")["result"]
    at = _render(r, "Summary")
    assert "\\$25.41M" in _head(at)


def test_a_what_if_answer_renders_at_every_depth(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "What happens to operating margin if labor costs increase 6% while revenue grows 3%?")
    r = t["result"]
    assert r, t.get("error")
    for depth in ("Summary", "Detail", "Method"):
        _render(r, depth)  # asserts no exception: scenario queries carry no rows, timing or id


def test_missing_fields_in_supporting_work_do_not_break_the_answer(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was revenue in Q3 2026?")["result"]
    sup = r["supporting"]
    sup["queries"] = [{"purpose": "scenario", "sql": "SELECT 1"}, {}]
    sup["sources"] = [{"dataset": "pnl"}]
    sup["citations"] = [{"title": "Policy"}]
    r["confidence"]["checks"] = [{"name": "x", "passed": False}]
    _render(r, "Method")
