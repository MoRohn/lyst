# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Conversation: a question that asks for no figure gets a grounded reply, not the nearest metric's analysis.

The case that started it: "Do we have an executive dashboard to monitor our top metrics in one place?" came back as a
COGS ratio driver analysis, marked Verified. Here it is answered from what the workspace really holds, with an offer to
build the dashboard, and nothing about it is called verified."""

from __future__ import annotations

import re

import pytest

from lyst.analyst import converse
from lyst.analyst.engine import _grounded_phrase

from .conftest import ask

ASKED = "Do we have an executive dashboard to monitor our top metrics in one place?"


# -- routing (pure) -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,named,kind,topic", [
    (ASKED, False, "discovery", "outputs"),
    ("Is there a revenue dashboard?", True, "discovery", "outputs"),
    ("What datasets do we have?", False, "discovery", "data"),
    ("Show me our dashboards", False, "discovery", "outputs"),
    ("Which metrics do we track?", False, "discovery", "metrics"),
    ("Do we have any alerts set up?", False, "discovery", "automation"),
    ("Can you build me an executive dashboard?", False, "build", "outputs"),
    ("How do I add a new data source?", False, "help", "data"),
    ("How is operating margin calculated?", True, "definition", "metrics"),
    ("What can you do?", False, "capability", "general"),
    ("hi", False, "greeting", "general"),
    ("Thanks!", False, "greeting", "general"),
    ("Should we worry about EMEA?", False, "discussion", "general"),
])
def test_conversational_requests_are_routed_to_conversation(text, named, kind, topic):
    assert converse.route(text, metrics_named=named) == {"kind": kind, "topic": topic}


@pytest.mark.parametrize("text,named", [
    ("What was revenue last quarter?", True),
    ("Why did the COGS ratio change last quarter?", True),
    ("What is our margin?", True),  # ambiguous: still the governed path, which asks which margin
    ("What was EBITDA last quarter?", False),  # unknown metric: still the governed path, which says so
    ("Can you show revenue by region?", True),
    ("Show me revenue by region", True),
    ("Build a dashboard of revenue by region", True),
    ("Forecast revenue", True),
    ("What if revenue grows 5%?", True),
    ("Profile pnl_ledger for data quality issues", False),
    ("SELECT region, sum(revenue) FROM pnl GROUP BY 1", False),
    ("refresh dashboard db_0123abcd", False),
])
def test_requests_for_figures_stay_analysis(text, named):
    assert converse.route(text, metrics_named=named) is None


def test_a_model_pick_must_quote_words_that_name_the_metric():
    assert _grounded_phrase("sales", "How did sales do last quarter?")
    assert not _grounded_phrase("top metrics", ASKED)  # generic words never stand for a metric
    assert not _grounded_phrase("cogs ratio", ASKED)  # not in the request at all
    assert not _grounded_phrase("", ASKED)


# -- the reply (pure) -----------------------------------------------------------------------------------------------

def _inv(**over):
    inv = {"workspace": "northwind", "can_build": True, "datasets": [{"name": "pnl", "title": "Monthly P&L", "version": 1}],
           "metrics": [
               {"name": "revenue", "title": "Revenue", "dataset": "pnl", "dimensions": ["region", "product_line"],
                "description": "Sum of booked revenue."},
               {"name": "operating_margin", "title": "Operating margin", "dataset": "pnl", "dimensions": ["region", "product_line"]},
               {"name": "cogs_ratio", "title": "COGS ratio", "dataset": "pnl", "dimensions": ["region", "product_line"]},
               {"name": "hardware_revenue", "title": "Hardware revenue", "dataset": "pnl", "dimensions": ["region"]},
               {"name": "tickets", "title": "Support tickets", "dataset": "demand", "dimensions": ["channel"]}],
           "dashboards": [], "assets": [], "workflows": [], "scenarios": [], "monitors": [], "rules": []}
    return inv | over


def test_no_dashboard_yet_offers_to_build_one_from_approved_metrics():
    r = converse.compose(ASKED, {"kind": "discovery", "topic": "outputs"}, _inv())
    assert r["answer"].startswith("Not yet.")
    assert "revenue, operating margin and COGS ratio" in r["answer"]
    assert r["suggestions"][0] == 'Build a dashboard called "Executive overview" with revenue, operating margin and COGS ratio by region'
    assert not re.search(r"\d", r["answer"])  # no figures, not even invented counts


def test_existing_dashboards_are_named_and_a_missing_one_is_said_plainly():
    inv = _inv(dashboards=[{"id": "db_1", "title": "Revenue by region", "status": "published"}])
    r = converse.compose(ASKED, {"kind": "discovery", "topic": "outputs"}, inv)
    assert r["answer"].startswith("Not quite. There's no executive dashboard yet")
    assert "Revenue by region" in r["answer"]
    assert r["items"][0]["id"] == "db_1"
    hit = converse.compose("Do we have a revenue dashboard?", {"kind": "discovery", "topic": "outputs"}, inv)
    assert hit["answer"] == "Yes: Revenue by region."


def test_a_definition_question_gets_the_approved_definition():
    r = converse.compose("What does revenue mean?", {"kind": "definition", "topic": "metrics"}, _inv(), named=["revenue"])
    assert "Revenue is an approved metric on the pnl dataset: Sum of booked revenue." in r["answer"]
    assert r["suggestions"] == ["What was revenue last quarter?"]


def test_a_viewer_is_told_an_editor_has_to_build_it():
    r = converse.compose(ASKED, {"kind": "discovery", "topic": "outputs"}, _inv(can_build=False))
    assert any("editor" in n for n in r["notes"])


def test_the_result_is_a_reply_never_verified():
    reply = converse.compose("hi", {"kind": "greeting", "topic": "general"}, _inv())
    out = converse.result("hi", reply, narrative_by="template")
    assert out["mode"] == "conversation" and out["confidence"]["level"] == "reply"
    assert out["confidence"]["checks"] == [] and out["evidence"] == [] and out["charts"] == []


# -- in the engine -------------------------------------------------------------------------------------------------

def test_the_dashboard_question_from_the_screenshot_gets_a_reply_not_an_analysis(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, ASKED)
    r = t["result"]
    assert t["status"] == "done" and r["mode"] == "conversation"
    assert r["confidence"]["level"] == "reply"
    assert "COGS ratio was" not in r["answer"] and "%" not in r["answer"]
    assert r["answer"].startswith("Not yet.")
    caps = [s["capability"] for s in lyst.runtime.trace(owner, t["id"])["steps"]]
    assert "converse" in caps and "metric_value" not in caps and "validate" not in caps and "learn" not in caps
    # the offer runs as written: it reads as a dashboard request on approved metrics that share a split
    offer = r["conversation"]["suggestions"][0]
    from lyst import studio as studio_mod

    assert studio_mod.wants_dashboard(offer)
    resolved = lyst.semantic.resolve(owner, "northwind", offer)
    assert len(resolved["metrics"]) >= 2 and not resolved["ambiguous"]


def test_governed_questions_still_run_the_analysis(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "What was revenue last quarter?")
    assert t["result"].get("mode") is None and t["result"]["confidence"]["level"] == "verified"
    assert ask(lyst, owner, "What was EBITDA last quarter?")["result"]["status"] == "needs_input"


def test_a_model_cannot_pin_an_unrelated_metric_on_a_question(seeded, monkeypatch):
    """The model once mapped "top metrics" to the COGS ratio. A pick must quote words of the request that name it."""
    lyst, owner = seeded
    llm = lyst.services.llm
    monkeypatch.setattr(llm, "allowed_for", lambda ws: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"metrics": ["cogs_ratio"], "phrase": "top metrics",
                                                           "intent": "explain"})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "")
    t = ask(lyst, owner, "How are we tracking against the plan this year?")
    assert t["result"]["status"] == "needs_input"  # no metric named: say so instead of answering about COGS
    caps = [s["capability"] for s in lyst.runtime.trace(owner, t["id"])["steps"]]
    assert "metric_value" not in caps


def test_a_model_can_call_a_request_conversation_and_word_the_reply(seeded, monkeypatch):
    lyst, owner = seeded
    llm = lyst.services.llm
    monkeypatch.setattr(llm, "allowed_for", lambda ws: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"metrics": [], "phrase": "", "intent": "conversation"})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "Good question. Nothing like that lives here yet, but I can set it up.")
    t = ask(lyst, owner, "Our board wants something they can glance at before meetings")
    r = t["result"]
    assert r["mode"] == "conversation" and r["answer"].startswith("Good question.")
    assert r["supporting"]["narrative_by"].startswith("model")
    # a model reply that slips in a figure is dropped for the grounded draft
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "Revenue was 4.2 million last quarter, so you are fine.")
    r2 = ask(lyst, owner, "Our board wants something they can glance at before meetings")["result"]
    assert "4.2" not in r2["answer"] and "dropped" in r2["supporting"]["narrative_by"]


# -- follow-ups: "yes" takes up the offer ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,index", [
    ("yes", 0), ("Yes please!", 0), ("sure", 0), ("go ahead", 0), ("do it, thanks", 0), ("ok", 0),
    ("the second one", 1), ("2", 1), ("the last one", 2), ("Offer C", 2),
    ("yes, but by product line", None), ("What was revenue last quarter?", None),
])
def test_a_short_yes_takes_up_an_offer(text, index):
    assert converse.offer_index(text, ["Offer A", "Offer B", "Offer C"]) == index


@pytest.mark.parametrize("text", ["no", "No thanks", "not now", "maybe later", "never mind"])
def test_a_no_is_a_reply_too(text):
    assert converse.route(text) == {"kind": "decline", "topic": "general"}
    r = converse.compose(text, converse.route(text), _inv())
    assert r["answer"].startswith("No problem") and not r["suggestions"]


def test_yes_runs_the_offer_as_written_over_http(seeded):
    from fastapi.testclient import TestClient

    from lyst.api import create_app

    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        first = c.post("/v1/workspaces/northwind/tasks", json={"question": ASKED}, headers=h).json()["task_id"]
        lyst.runtime.drain()
        offer = lyst.runtime.get(owner, first)["result"]["conversation"]["suggestions"][0]
        tid = c.post("/v1/workspaces/northwind/tasks", json={"question": "yes please"}, headers=h).json()["task_id"]
        t = lyst.runtime.get(owner, tid)
        assert t["request"]["question"] == offer
        assert t["request"]["in_reply_to"] == first and t["request"]["said"] == "yes please"
        # a yes with no offer before it is just a line on its own: nothing is run on its behalf
        other = lyst.guard.create_user("m@n.test", "M")
        lyst.guard.add_member(owner, "northwind", other.user_id, "editor")
        ho = {"Authorization": f"Bearer {lyst.guard.issue_token(other)}"}
        tid2 = c.post("/v1/workspaces/northwind/tasks", json={"question": "yes"}, headers=ho).json()["task_id"]
        assert lyst.runtime.get(other, tid2)["request"]["question"] == "yes"


def test_an_unknown_metric_names_the_ones_that_exist(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was EBITDA last quarter?")["result"]
    assert r["status"] == "needs_input" and "No approved metric" in r["answer"]
    assert "Revenue" in r["answer"] and "Operating income" in r["answer"]
