# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Attention management. Acceptance scenario H: several anticipations of different value; Lyst pushes the most valuable
first within the person's cap, never twice about the same thing, holds during quiet hours, and honours muted kinds,
minimum scores and opting out, with preferences that persist."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lyst.anticipate import model
from lyst.api import create_app

WS = "northwind"
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


def _item(lyst, owner, key, kind, score, subject, verdict):
    db = lyst.stores.workspace(WS)
    a, _ = model.upsert(db, {"kind": kind, "objective": f"Item {key}", "subject_kind": "dataset", "subject_id": subject,
                             "audience": [owner.user_id], "signals": [{"kind": "test"}], "reasons": [f"Reason for {key}."],
                             "scores": {}, "score": score, "level_required": 1, "actions": [], "depends": {},
                             "needed_at": None, "valid_until": NOW + timedelta(days=3), "dedupe_key": f"test|{key}"})
    model.set_prepared(db, a["id"], verdict=verdict, prepared={}, verification={"checks": []}, resources={})
    return a["id"]


@pytest.fixture
def items(seeded):
    lyst, owner = seeded
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    ids = {"A": _item(lyst, owner, "A", "recurring_prep", 90, "pnl", "blocked"),
           "B": _item(lyst, owner, "B", "recurring_prep", 70, "demand", "blocked"),
           "C": _item(lyst, owner, "C", "early_warning", 60, "pnl", "verified"),       # same subject as A
           "D": _item(lyst, owner, "D", "recurring_prep", 30, "staffing", "blocked")}  # below the push threshold
    return lyst, owner, ids


def _pushed(lyst, owner):
    return [i["label"] for i in lyst.monitors.inbox(owner, WS) if (i["monitor_id"] or "").startswith("anticipate:")]


def test_h_most_valuable_first_within_the_cap_and_no_duplicates(items):
    lyst, owner, ids = items
    lyst.anticipation.set_attention(owner, WS, {"max_per_day": 2})
    out = lyst.anticipation.cycle(WS, NOW)
    assert out["surfaced"] == 2
    assert sorted(_pushed(lyst, owner)) == ["Item A", "Item B"]                         # C repeats A's subject; D is low value
    db = lyst.stores.workspace(WS)
    assert [e["event"] for e in model.events(db, ids["C"])][-1] == "not_pushed"
    assert db.scalar("SELECT status FROM anticipations WHERE id = ?", [ids["D"]]) == "prepared"   # still in the activity center
    listed = [a["objective"] for a in lyst.anticipation.list(owner, WS, now=NOW)]
    assert listed == ["Item A", "Item B", "Item C", "Item D"]                           # ranked by score


def test_h_cap_keeps_the_most_valuable_and_holds_the_rest(items):
    lyst, owner, ids = items
    lyst.anticipation.set_attention(owner, WS, {"max_per_day": 1})
    lyst.anticipation.cycle(WS, NOW)
    assert _pushed(lyst, owner) == ["Item A"]
    db = lyst.stores.workspace(WS)
    assert [e["event"] for e in model.events(db, ids["B"])][-1] == "held"
    lyst.anticipation.cycle(WS, NOW)
    assert [e["event"] for e in model.events(db, ids["B"])].count("held") == 1          # not re-logged every cycle


def test_h_quiet_hours_hold_until_they_end(items):
    lyst, owner, ids = items
    lyst.anticipation.set_attention(owner, WS, {"quiet_start": "09:00", "quiet_end": "17:00", "timezone": "America/New_York"})
    assert lyst.anticipation.cycle(WS, NOW)["surfaced"] == 0                             # 10:00 in New York
    assert _pushed(lyst, owner) == []
    assert lyst.anticipation.cycle(WS, NOW + timedelta(hours=8))["surfaced"] == 2        # 18:00 there
    assert sorted(_pushed(lyst, owner)) == ["Item A", "Item B"]


def test_h_quiet_hours_wrap_midnight(items):
    lyst, owner, _ = items
    lyst.anticipation.set_attention(owner, WS, {"quiet_start": "22:00", "quiet_end": "07:00"})
    assert lyst.anticipation.cycle(WS, NOW.replace(hour=23))["surfaced"] == 0
    assert lyst.anticipation.cycle(WS, NOW.replace(hour=8))["surfaced"] == 2


def test_h_muted_kinds_minimum_score_and_opt_out(items):
    lyst, owner, ids = items
    lyst.anticipation.set_attention(owner, WS, {"muted_kinds": ["early_warning"], "min_score": 65})
    assert [a["objective"] for a in lyst.anticipation.list(owner, WS, now=NOW)] == ["Item A", "Item B"]
    lyst.anticipation.set_attention(owner, WS, {"opted_out": True})
    assert lyst.anticipation.list(owner, WS, now=NOW) == []
    assert lyst.anticipation.cycle(WS, NOW)["surfaced"] == 0
    assert _pushed(lyst, owner) == []


def test_h_preferences_validate_and_persist_through_the_api(items):
    lyst, owner, _ = items
    token = lyst.guard.issue_token(owner)
    h = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get(f"/v1/workspaces/{WS}/attention", headers=h).json()["max_per_day"] == 3
        r = c.put(f"/v1/workspaces/{WS}/attention", headers=h, json={"quiet_start": "21:00", "quiet_end": "07:30",
                                                                      "timezone": "Europe/Berlin", "muted_kinds": ["next_best_action"]})
        assert r.status_code == 200 and r.json()["timezone"] == "Europe/Berlin"
        assert c.put(f"/v1/workspaces/{WS}/attention", headers=h, json={"quiet_start": "25:00", "quiet_end": "07:00"}).status_code == 400
        assert c.put(f"/v1/workspaces/{WS}/attention", headers=h, json={"quiet_start": "21:00", "quiet_end": ""}).status_code == 400
        assert c.put(f"/v1/workspaces/{WS}/attention", headers=h, json={"muted_kinds": ["everything"]}).status_code == 400
        assert c.put(f"/v1/workspaces/{WS}/attention", headers=h, json={"timezone": "Mars/Olympus"}).status_code == 400
        got = c.get(f"/v1/workspaces/{WS}/attention", headers=h).json()
        assert got["quiet_end"] == "07:30" and got["muted_kinds"] == ["next_best_action"]


def test_admins_are_told_when_the_person_it_is_for_opted_out(items):
    lyst, owner, _ = items
    member = lyst.guard.create_user("mo@northwind.test", "Mo")
    lyst.guard.add_member(owner, WS, member.user_id, "editor")
    lyst.stores.workspace(WS).execute("UPDATE anticipations SET audience = ?", [f'["{member.user_id}"]'])
    lyst.anticipation.set_attention(member, WS, {"opted_out": True})
    lyst.anticipation.cycle(WS, NOW)
    assert "Item A" in _pushed(lyst, owner)                                            # the workspace owner still hears
    assert not [i for i in lyst.monitors.inbox(member, WS) if (i["monitor_id"] or "").startswith("anticipate:")]
