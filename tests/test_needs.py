# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Needs you: an answer that needs someone becomes one question with choices worked out from the workspace, and the
choice a person picks is carried out by Lyst under the same governance as doing it by hand (a glossary mapping is an
admin's, confirmed first; an editor asks for it through a review; nothing changes silently)."""

import json

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.needs import _phrase, _reword
from lyst.security import AccessDenied

from .conftest import ask

WS = "northwind"
GROSS = "What was gross margin last quarter by product line?"


def _people(lyst, owner):
    ed = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, WS, ed.user_id, "editor")
    vi = lyst.guard.create_user("vic@northwind.test", "Vic")
    lyst.guard.add_member(owner, WS, vi.user_id, "viewer")
    return ed, vi


def test_phrase_keeps_the_word_that_sharpens_a_term():
    assert _phrase(GROSS, "margin") == "gross margin"
    assert _phrase("What is our margin?", "margin") == "margin"
    assert _reword(GROSS, "gross margin", "Operating margin") == "What was operating margin last quarter by product line?"
    assert _reword(GROSS, "gross margin", "COGS ratio").startswith("What was COGS ratio last quarter")


def test_an_ambiguous_term_offers_each_metric_and_the_pick_runs_again(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, GROSS)
    assert t["outcome"] == "needs_input"
    v = lyst.needs.view(owner, t["id"])
    assert v["needs"] and v["issue"] == "Which “gross margin” do you mean?"
    ids = [c["id"] for c in v["choices"]]
    assert ids[:2] == ["use:cogs_ratio", "use:operating_margin"] and ids[-1] == "dismiss"
    use = v["choices"][1]
    assert use["label"] == "I mean operating margin" and use["detail"] == "Operating income divided by revenue."
    # an admin may map the sharper phrase, and is shown exactly what that changes before it does
    mapping = next(c for c in v["choices"] if c["kind"] == "map")
    assert mapping["term"] == "gross margin" and "every request in this workspace" in mapping["confirm"]

    out = lyst.needs.resolve(owner, t["id"], choice="use:operating_margin")
    assert out["resolved"] and out["question"] == "What was operating margin last quarter by product line?"
    lyst.runtime.drain()
    new = lyst.runtime.get(owner, out["task_id"])
    assert new["outcome"] == "complete" and new["request"]["resolves"] == t["id"]
    old = lyst.runtime.get(owner, t["id"])
    assert old["outcome"] == "resolved" and old["resolution"]["follow_task_id"] == out["task_id"]
    assert old["result"]["status"] == "needs_input"  # the answer itself is never rewritten
    assert {x["id"]: x["outcome"] for x in lyst.runtime.list(owner, WS)}[t["id"]] == "resolved"
    with pytest.raises(ValueError, match="already resolved"):
        lyst.needs.resolve(owner, t["id"], choice="dismiss")


def test_mapping_a_term_needs_confirmation_and_then_reads_it_that_way(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, GROSS)
    pick = "map:gross margin:cogs_ratio"
    with pytest.raises(ValueError, match="confirm"):
        lyst.needs.resolve(owner, t["id"], choice=pick)
    assert not [g for g in lyst.semantic.glossary(owner, WS) if g["term"] == "gross margin"]
    out = lyst.needs.resolve(owner, t["id"], choice=pick, confirm=True)
    assert {g["term"]: g["maps_to_metric"] for g in lyst.semantic.glossary(owner, WS)}["gross margin"] == "cogs_ratio"
    lyst.runtime.drain()
    assert lyst.runtime.get(owner, out["task_id"])["outcome"] == "complete"


def test_an_editor_asks_an_admin_and_nothing_changes_until_approved(seeded):
    lyst, owner = seeded
    ed, vi = _people(lyst, owner)
    t = ask(lyst, ed, GROSS)
    v = lyst.needs.view(ed, t["id"])
    kinds = {c["kind"] for c in v["choices"]}
    assert "map" not in kinds and "request" in kinds
    assert any(c["id"] == "define:gross margin" for c in v["choices"])  # neither metric: ask for its own
    out = lyst.needs.resolve(ed, t["id"], choice="request:gross margin:operating_margin")
    assert out["review_id"] and not [g for g in lyst.semantic.glossary(owner, WS) if g["term"] == "gross margin"]
    assert lyst.runtime.get(ed, t["id"])["outcome"] == "resolved"
    lyst.collab.decide(owner, WS, out["review_id"], approve=True)
    assert {g["term"]: g["maps_to_metric"] for g in lyst.semantic.glossary(owner, WS)}["gross margin"] == "operating_margin"
    # a viewer sees only choices that need no one else, and cannot resolve someone else's answer
    t2 = ask(lyst, ed, "What is our margin?")
    seen = lyst.needs.view(vi, t2["id"])
    assert not seen["can_resolve"] and {c["kind"] for c in seen["choices"]} <= {"ask", "dismiss"}
    with pytest.raises(AccessDenied):
        lyst.needs.resolve(vi, t2["id"], choice="dismiss")


def test_own_words_reword_the_request_or_clarify_it(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, GROSS)
    out = lyst.needs.resolve(owner, t["id"], text="the operating one")
    assert out["question"] == "What was operating margin last quarter by product line?"
    t2 = ask(lyst, owner, "What was EBITDA last quarter?")
    v = lyst.needs.view(owner, t2["id"])
    assert v["issue"] == "Which metric should Lyst use?" and v["choices"][0]["kind"] == "ask"
    out2 = lyst.needs.resolve(owner, t2["id"], text="before interest and tax")
    assert out2["question"] == "What was EBITDA last quarter: before interest and tax"


def test_an_unverified_quality_answer_points_to_the_review_and_can_be_kept(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Profile pnl_ledger for data quality issues")
    # a profile with flagged rows finishes with warnings; one whose checks block it is "unverified", with the same record
    lyst.runtime.db.execute("UPDATE tasks SET result = ? WHERE id = ?",
                            [json.dumps({**t["result"], "status": "unverified"}), t["id"]])
    v = lyst.needs.view(owner, t["id"])
    first = v["choices"][0]
    assert first["recommended"] and first["go"]["params"] == {"dataset": "pnl_ledger", "review": "quality"}
    assert lyst.needs.resolve(owner, t["id"], choice=first["id"]) == {"resolved": False, "go": first["go"],
                                                                     "done": first["label"]}
    assert lyst.runtime.get(owner, t["id"])["outcome"] == "unverified"  # opening a place resolves nothing
    assert lyst.needs.resolve(owner, t["id"], choice="dismiss:keep")["resolved"]
    assert lyst.runtime.get(owner, t["id"])["outcome"] == "resolved"


def test_routes(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, GROSS)
    token = lyst.guard.issue_token(owner)
    with TestClient(create_app(lyst, start_workers=False)) as http:
        h = {"Authorization": f"Bearer {token}"}
        v = http.get(f"/v1/tasks/{t['id']}/needs", headers=h).json()
        assert v["needs"] and len(v["choices"]) >= 3
        assert http.post(f"/v1/tasks/{t['id']}/needs/resolve", headers=h, json={}).status_code == 422
        assert http.post(f"/v1/tasks/{t['id']}/needs/resolve", headers=h, json={"choice": "nope"}).status_code == 400
        r = http.post(f"/v1/tasks/{t['id']}/needs/resolve", headers=h, json={"choice": "dismiss"}).json()
        assert r == {"resolved": True, "done": "Not needed any more"}
