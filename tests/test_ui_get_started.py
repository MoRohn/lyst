# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Get started: the count is the essentials only, the guide is drawn once (no card repeating the sheet's title), each
step has its own glyph and state, and finishing the essentials ends on a finished state rather than an empty sheet."""

from streamlit.testing.v1 import AppTest

from lyst.ui import onboarding as ob

from .test_ui_pages import _texts, ui  # noqa: F401, F811  (fixture)

STEPS = [{"key": "connect", "title": "Connect or upload data", "detail": "d", "done": True, "optional": False},
         {"key": "discover", "title": "Let Lyst study the data", "detail": "d", "done": False, "optional": False},
         {"key": "confirm", "title": "Confirm the business definitions", "detail": "d", "done": True, "optional": False},
         {"key": "people", "title": "Invite the team", "detail": "d", "done": True, "optional": True},
         {"key": "objectives", "title": "Say what matters this year", "detail": "d", "done": False, "optional": True},
         {"key": "first_answer", "title": "Ask a first question", "detail": "d", "done": True, "optional": False}]


def test_the_ring_counts_essentials_and_names_what_is_left():
    assert 'aria-label="3 of 4 essential steps done"' in ob.ring(3, 4) and "--p: 75%" in ob.ring(3, 4)
    head = ob.head(3, 4, STEPS[1], extras=True)
    assert "One essential left" in head and "Next: Let Lyst study the data." in head and "don't count" in head
    assert "Two essentials left" in ob.head(2, 4, None)


def test_each_step_has_its_own_glyph_and_a_check_once_done():
    assert ">auto_awesome<" in ob.step_html(STEPS[1], kicker="Next up") and "Next up" in ob.step_html(STEPS[1], kicker="Next up")
    done = ob.step_html(STEPS[0], pos="first")
    assert ">check<" in done and "is-done" in done and "<s>" not in done and "~~" not in done  # no strike-through
    assert len({ob.ICON[k] for k in ("connect", "discover", "confirm", "first_answer", "people", "objectives")}) == 6
    assert "&lt;b&gt;" in ob.step_html({**STEPS[1], "title": "<b>"})  # titles are escaped


def test_the_finish_says_what_it_took():
    stat = {"milestones": {"first_dataset_at": "x", "seconds_to_first_dataset": 20, "first_analysis_at": "y",
                           "seconds_to_first_analysis": 600}}
    out = ob.finish_html(stat, "dark")
    assert "Lyst is ready" in out and "&lt; 1 min" in out and "10 min" in out and "to a verified answer" not in out
    assert out.count("data:image/svg+xml;base64,") == 1  # the picture is an image (st.html drops inline SVG)
    assert ob.art("light") != ob.art("dark")  # drawn in the active theme's colours


def _guide(steps, nxt, finished=None):
    def app(steps, nxt, finished):
        from lyst.ui import onboarding

        onboarding.guide(steps, nxt=nxt, editor=True, finished=finished)

    at = AppTest.from_function(app, args=(steps, nxt, finished))
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def test_one_primary_action_for_the_next_step_and_optional_steps_apart():
    at = _guide(STEPS, "discover")
    labels = {b.label: b.proto.type for b in at.button}
    assert labels.get("Study it") == "primary" and labels.get("Add them") == "secondary"
    assert sum(t == "primary" for t in labels.values()) == 1
    body = _texts(at)
    assert "3</b><i>/4" in body and "Make it yours" in body and "Get started ·" not in body  # no title repeated inside


def test_finished_state_replaces_the_list_and_keeps_open_extras():
    at = _guide(STEPS, None, finished={"milestones": {}})
    body = _texts(at)
    assert "Lyst is ready" in body and "Essentials" not in body and "Say what matters this year" in body
    assert "Invite the team" not in body  # done extras do not linger on the finish
    assert [b.label for b in at.button][0] == "Ask Lyst a question"


def test_home_opens_the_guide_without_a_nested_card(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.session_state["_bar_do"] = {"type": "setup", "nonce": "s1"}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "essential" in body and "Let Lyst study the data" in body and "Get started ·" not in body
    assert at.session_state["_gs_seen"] is True  # so Home can show the finish once the essentials are done
