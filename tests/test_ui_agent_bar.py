# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The Lyst agent bar: one field at the top of every page to ask, speak, go anywhere and watch Lyst work.

What the field understands is pure and tested here; so is where Live sends a person. In the app: one bar on every
page, a question from any page starts and is followed without leaving it, and voice keeps the voice rules (speech
never approves anything; when Lyst was not sure what it heard, nothing starts until the person says yes)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from lyst.ui import agent_bar
from lyst.ui.agent_bar import feed, find_place, interpret, where

from .test_ui_agent import _bar
from .test_ui_pages import _texts, ui  # noqa: F401  (fixture)

HERE = Path(agent_bar.__file__).parent / "agent_bar_component"
JS = (HERE / "bar.js").read_text(encoding="utf-8")
CSS = (HERE / "bar.css").read_text(encoding="utf-8")


# -- what the field understands -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,place", [
    ("go to SQL", "Data › SQL"), ("open definitions", "Brain › Definitions"), ("take me to the data page", "Data"),
    ("Settings", "Settings"), ("memory", None), ("show me needs you", "Brain › Needs you"), ("home", "Home"),
])
def test_places_by_name_or_with_go_words(text, place):
    kind, arg = interpret(text)
    if place is None:  # a word that names a section on its own still goes there, when the palette has it
        assert kind in ("go", "ask")
        return
    assert kind == "go", (text, kind, arg)
    assert arg["label"].startswith(place), (text, arg["label"])


@pytest.mark.parametrize("text,on", [("follow Lyst", True), ("watch", True), ("stop following", False),
                                     ("don't follow lyst", False)])
def test_follow_on_and_off(text, on):
    assert interpret(text) == ("follow", on)


@pytest.mark.parametrize("text", ["Why did operating margin decline in Q3 2026?", "show me revenue by region",
                                  "open a ticket with finance about the variance", "what is our margin"])
def test_anything_else_is_a_question(text):
    assert interpret(text) == ("ask", text)


def test_empty_is_nothing():
    assert interpret("   ") == ("none", None)


def test_every_place_is_a_real_page():
    places = agent_bar.places()
    assert len({p["id"] for p in places}) == len(places)
    root = Path(agent_bar.__file__).parent
    for p in places:
        assert (root / p["page"]).exists(), p
    assert find_place("sql")["label"] == "Data › SQL"
    assert find_place("nowhere at all") is None


# -- Live -------------------------------------------------------------------------------------------------------------

def test_where_things_live():
    assert where("task", "t1") == {"page": "app_pages/analyst.py", "params": {"task": "t1"}, "label": "Home"}
    assert where("dataset", "pnl")["params"] == {"dataset": "pnl"}
    assert where("metric", "margin")["page"] == "app_pages/brain.py"
    acts = {"a1": {"subject_kind": "dataset", "subject_id": "pnl"}}
    assert where("activity", "a1", acts=acts)["params"] == {"dataset": "pnl"}
    assert where("activity", "a2", acts=acts) is None
    assert where("nothing", "x") is None


def test_feed_reads_as_what_lyst_did_newest_first():
    events = [
        {"seq": 1, "type": "task.created", "subject": {"kind": "task", "id": "t1"}, "occurred_at": None},
        {"seq": 2, "type": "task.completed", "subject": {"kind": "task", "id": "t1"}, "data": {"outcome": "verified"}},
        {"seq": 3, "type": "task.completed", "subject": {"kind": "task", "id": "t2"}, "data": {"outcome": "needs_input"}},
        {"seq": 4, "type": "activity.changed", "subject": {"kind": "activity", "id": "a1"},
         "data": {"to": "awaiting_approval"}},
        {"seq": 5, "type": "webhook.delivered", "subject": {"kind": "webhook", "id": "w"}},  # plumbing: not shown
        {"seq": 6, "type": "dataset.schema_changed", "subject": {"kind": "dataset", "id": "pnl_ledger"}},
    ]
    out = feed(events, titles={"t1": "Why did margin fall?", "t2": "What is our margin?"},
               acts={"a1": {"subject_kind": "dataset", "subject_id": "pnl", "subject_title": "P&L"}})
    assert [f["id"] for f in out] == ["6", "4", "3", "2", "1"]
    by = {f["id"]: f for f in out}
    assert (by["6"]["verb"], by["6"]["what"], by["6"]["go"]["label"]) == ("Shape changed in", "pnl ledger", "Data")
    assert (by["4"]["verb"], by["4"]["tone"], by["4"]["what"]) == ("Automatic · needs approval", "warn", "P&L")
    assert (by["3"]["verb"], by["3"]["tone"]) == ("Needs your input", "warn")
    assert (by["2"]["verb"], by["2"]["tone"], by["2"]["what"]) == ("Finished", "ok", "Why did margin fall?")
    assert by["1"]["go"]["params"] == {"task": "t1"}
    many = [{"seq": i, "type": "task.created", "subject": {"kind": "task", "id": f"t{i}"}} for i in range(40)]
    assert len(feed(many, titles={}, acts={})) == agent_bar.FEED


# -- in the app -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("page,name", [("app_pages/analyst.py", "home"), ("app_pages/data.py", "data"),
                                       ("app_pages/brain.py", "brain"), ("app_pages/settings.py", "settings")])
def test_one_bar_on_every_page(ui, page, name):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page(page)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    bar = _bar(at)
    assert bar["page"] == name and bar["voice"] is True
    assert {"Home", "Data › SQL", "Brain › Definitions", "Settings"} <= {p["label"] for p in bar["places"]}
    assert bar["tone"] in ("run", "wait", "ok") and bar["head"]


def test_home_has_no_second_ask_box(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not [f for f in at.get("form") if getattr(f, "form_id", "") == "ask"]  # the bar is the one place to ask
    assert 'class="lyst-pulse"' not in " ".join(str(h.value) for h in at.get("html"))


def test_a_question_from_data_starts_and_is_followed_there(ui):  # noqa: F811
    lyst = ui
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.run()
    at.session_state["_bar_do"] = {"type": "submit", "text": "Which region grew fastest last quarter?", "nonce": "1"}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    bar = _bar(at)
    assert bar["page"] == "data" and bar["open"] is True  # stayed on Data, with the conversation open
    turn = bar["turns"][-1]
    assert turn["q"] == "Which region grew fastest last quarter?" and turn["via"] == "typed" and turn["task"]
    assert turn["status"] and lyst.stores.control.scalar("SELECT count(*) FROM tasks WHERE id = ?", [turn["task"]]) == 1


def test_go_to_a_section_from_the_bar(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.session_state["_bar_do"] = {"type": "submit", "text": "go to definitions", "nonce": "1"}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert _bar(at)["page"] == "brain"


def test_speech_never_approves_or_runs_anything(ui):  # noqa: F811
    """'Approve it' spoken is refused by the voice engine; the bar says to confirm on screen and nothing changes."""
    lyst = ui
    pending = lyst.stores.control.scalar("SELECT count(*) FROM approvals WHERE status = 'pending'")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.session_state["_bar_do"] = {"type": "voice", "text": "approve the pending request", "confidence": 0.97,
                                   "nonce": "1"}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert lyst.stores.control.scalar("SELECT count(*) FROM approvals WHERE status = 'pending'") == pending
    turn = _bar(at)["turns"][-1]
    assert turn["via"] == "voice" and "Confirm it on screen" in (turn["note"] or "") and not turn.get("task")


def test_when_lyst_is_not_sure_what_it_heard_nothing_starts_until_yes(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
    at.run()
    at.session_state["_bar_do"] = {"type": "voice", "text": "why did operating margin fall", "confidence": 0.41,
                                   "nonce": "1"}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    bar = _bar(at)
    assert bar["heard"] == "why did operating margin fall"
    assert not bar["turns"][-1].get("task")  # nothing started
    at.session_state["_bar_do"] = {"type": "heard", "yes": True, "nonce": "2"}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    bar = _bar(at)
    assert bar["heard"] is None and bar["turns"][-1]["task"]  # started once the person said yes


def test_follow_is_off_until_the_person_turns_it_on(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert _bar(at)["follow"] is False
    at.session_state["_bar_do"] = {"type": "follow", "on": True, "nonce": "1"}
    at.run()
    assert _bar(at)["follow"] is True


# -- the component -----------------------------------------------------------------------------------------------------

def test_the_mic_starts_only_on_a_press():
    assert JS.count("startMic(") == 2  # defined once, called once: from the mic button's click
    click = re.search(r'this\.mic\.addEventListener\("click", \(\) => \(this\.listening \? this\.stopMic\(\) : this\.startMic\(\)\)\)', JS)
    assert click
    assert "rec.continuous = false" in JS  # stops when the person pauses
    assert "getUserMedia" not in JS and "MediaRecorder" not in JS  # no audio is captured or kept


def test_spoken_replies_are_said_once():
    assert "say.id === this.spoken" in JS


def test_a11y_markers():
    for needle in ('role="combobox"', 'aria-autocomplete="list"', 'role="listbox"', 'role="option"', 'role="switch"',
                   'aria-live="polite"', 'role="progressbar"', "aria-expanded", "aria-pressed", "aria-activedescendant"):
        assert needle in JS, needle
    assert "prefers-reduced-motion" in CSS and "forced-colors" in CSS and ":focus-visible" in CSS


def test_only_tokens_for_colour():
    body = re.sub(r"var\(--lyst-[a-z0-9-]+,\s*[^()]*(?:\([^()]*\)[^()]*)*\)", "", CSS)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
    assert not re.findall(r"rgba?\(", body)
    assert "fetch(" not in JS and "http" not in JS.replace("https://", "")  # no network from the bar
