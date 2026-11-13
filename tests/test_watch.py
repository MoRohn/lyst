# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A question that asks Lyst to watch something ("Watch operating margin in EMEA") is answered now and requests a
monitor on that metric, narrowed to the segment it named, which runs only once an admin approves it."""

from __future__ import annotations

import pytest

from lyst.analyst import watch

from .conftest import ask

WS = "northwind"


def _monitors(lyst, owner):
    return lyst.monitors.list(owner, WS)


def test_watch_a_segment_answers_and_requests_a_segment_monitor(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Set a monitor on operating margin for EMEA")["result"]
    assert r["status"] == "complete" and "24.0%" in r["answer"]
    assert r["actions"][0] == ("Lyst will watch operating margin in EMEA each month and investigate the next unusual "
                               "move once an admin approves it under Approvals.")
    (m,) = _monitors(lyst, owner)
    assert m["metric"] == "operating_margin" and m["segment"] == {"dimension": "region", "member": "EMEA"}
    assert not m["enabled"] and r["supporting"]["monitor"]["monitor_id"] == m["id"]
    # asking again reuses the request: no second monitor and no second approval
    ask(lyst, owner, "Watch operating margin in EMEA")
    assert len(_monitors(lyst, owner)) == 1
    assert len([a for a in lyst.runtime.approvals(owner, WS) if a["action"] == "monitor.activate"]) == 1


def test_once_approved_asking_again_says_it_is_already_watched(seeded):
    lyst, owner = seeded
    ask(lyst, owner, "Watch revenue")
    (m,) = _monitors(lyst, owner)
    assert m["segment"] is None
    admin = lyst.guard.create_user("admin@northwind.test", "Admin")
    lyst.guard.add_member(owner, WS, admin.user_id, "admin")
    lyst.runtime.decide(admin, m["approval_id"], True)
    r = ask(lyst, owner, "Alert me when revenue moves")["result"]
    assert r["actions"][0] == "Lyst already watches revenue and investigates its next unusual move."
    assert len(_monitors(lyst, owner)) == 1


def test_a_viewer_is_told_to_ask_an_editor(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("viewer@northwind.test", "Viewer")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    r = ask(lyst, viewer, "Keep an eye on revenue in APAC")["result"]
    assert r["actions"][0] == "Ask an editor to watch revenue in APAC: your role cannot set up a monitor."
    assert not _monitors(lyst, owner)


def test_a_plain_question_requests_nothing(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was revenue in EMEA last quarter?")["result"]
    assert "monitor" not in r["supporting"] and not _monitors(lyst, owner)


@pytest.mark.parametrize("text", ["Watch revenue", "Monitor operating margin in EMEA", "Please keep an eye on COGS ratio",
                                  "Alert me when revenue drops", "Let me know if operating income moves",
                                  "Set up an alert on support tickets"])
def test_asks_to_watch(text):
    assert watch.asks_to_watch(text)


@pytest.mark.parametrize("text", ["What was revenue last quarter?", "Which watches are running?",
                                  "Why did the monitor fire for revenue?", "Tell me about revenue"])
def test_not_a_watch(text):
    assert not watch.asks_to_watch(text)
