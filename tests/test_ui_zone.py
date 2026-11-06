# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The activity zone, the Kanban board on every page: which column each engine record lands in, which moves and
actions a card offers to whom, that a gesture can only reach what the card offers and that anything that changes
something is confirmed first and still goes through the API's approvals; and its look, in light and dark, without a
browser (golden markup, every class styled, every token defined, every icon drawn)."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from lyst.ui import kit, tokens, zone

from .test_ui_pages import _texts, ui  # noqa: F401  (fixture)

HERE = Path(zone.__file__).parent / "zone_component"
GOLDEN = Path(__file__).parent / "golden" / "zone"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
COLS = [c for c, *_ in zone.COLUMNS]


def _at(minutes: int = 5) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def _task(tid, status, outcome=None, kind="analysis", minutes=5):
    return {"id": tid, "title": f"Question {tid}", "status": status, "outcome": outcome, "kind": kind,
            "created_at": _at(minutes), "updated_at": _at(minutes), "lifecycle": {}}


TASKS = [_task("t_run", "running"), _task("t_input", "done", "needs_input"), _task("t_bad", "done", "unverified"),
         _task("t_fail", "failed"), _task("t_pause", "paused"), _task("t_done", "done", "complete"),
         _task("t_old", "done", "complete", minutes=60 * 24 * 9), _task("t_x", "cancelled"),
         _task("t_inv", "done", "complete", kind="investigation"), _task("t_owned", "running")]
ACTS = [{"id": "a_sugg", "status": "suggested", "action": "investigate", "subject_kind": "dataset", "subject_id": "pnl",
         "subject_title": "Monthly P&L", "explanation": "a schema changed", "created_at": _at()},
        {"id": "a_wait", "status": "awaiting_approval", "action": "propose", "subject_kind": "metric",
         "subject_id": "revenue", "explanation": "x", "approval_id": "ap_act", "created_at": _at()},
        {"id": "a_run", "status": "running", "action": "investigate", "subject_kind": "dataset", "subject_id": "pnl",
         "explanation": "x", "task_id": "t_owned", "created_at": _at()},
        {"id": "a_gone", "status": "cancelled", "action": "notify", "subject_kind": "dataset", "subject_id": "pnl",
         "explanation": "x", "created_at": _at()}]
APPROVALS = [{"id": "ap_act", "action": "activity.execute", "summary": "Run what Lyst prepared", "created_at": _at()},
             {"id": "ap_mon", "action": "monitor.activate", "summary": "Watch revenue", "created_at": _at()},
             {"id": "ap_rule", "action": "rule.activate", "summary": "Turn on a rule", "created_at": _at()}]
FINDINGS = [{"id": "f_risk", "monitor_id": "mon_1", "direction": "risk", "summary": "Margin fell", "status": "new",
             "task_id": "t_inv", "metric": "operating_margin", "label": "Sep 2026", "created_at": _at()},
            {"id": "f_up", "monitor_id": "mon_1", "direction": "opportunity", "summary": "Revenue rose", "status": "new",
             "created_at": _at()},
            {"id": "f_rule", "monitor_id": "rule:a_x", "direction": "down", "summary": "x", "status": "new",
             "created_at": _at()},
            {"id": "f_seen", "monitor_id": "mon_1", "direction": "risk", "summary": "x", "status": "acknowledged",
             "created_at": _at()}]
SUGG = [{"id": "n_next", "kind": "next_best_action", "objective": "Look inside EMEA", "subject_kind": "task",
         "subject_id": "t_done", "status": "candidate", "score": 25, "reasons": ["EMEA drove it"],
         "actions": [{"id": "run_now", "label": "Run this analysis"}, {"id": "dismiss"}], "created_at": _at()},
        {"id": "n_risk", "kind": "health_foresight", "objective": "A credential expires", "subject_kind": "connection",
         "subject_id": "c1", "status": "surfaced", "verdict": "risk", "actions": [{"id": "renew"}], "created_at": _at()}]


def _build(role="owner"):
    return zone.build(tasks=TASKS, activities=ACTS, approvals=APPROVALS, findings=FINDINGS, suggestions=SUGG, role=role,
                      progress={"t_run": (2, 5), "t_owned": (1, 4)}, now=NOW.timestamp())


def _by_id(cards):
    return {c["id"]: c for c in cards}


def test_every_record_lands_in_the_column_its_state_means():
    c = _by_id(_build())
    assert {k: c[k]["col"] for k in ("t:t_run", "t:t_input", "t:t_bad", "t:t_fail", "t:t_pause", "t:t_done")} == {
        "t:t_run": "working", "t:t_input": "needs", "t:t_bad": "needs", "t:t_fail": "needs", "t:t_pause": "needs",
        "t:t_done": "done"}
    assert c["a:a_sugg"]["col"] == "suggested" and c["a:a_wait"]["col"] == "needs" and c["a:a_run"]["col"] == "working"
    assert c["f:f_risk"]["col"] == "needs" and c["f:f_up"]["col"] == "suggested"
    assert c["n:n_next"]["col"] == "suggested" and c["n:n_risk"]["col"] == "needs"
    # nothing cancelled, dismissed, seen, older than a week, or shown twice
    for gone in ("t:t_x", "t:t_old", "a:a_gone", "f:f_rule", "f:f_seen", "ap:ap_act", "t:t_owned"):
        assert gone not in c, gone
    assert c["ap:ap_mon"]["col"] == "needs" and c["ap:ap_rule"]["col"] == "needs"
    # running work shows the engine's own step count; work an activity started shows on the activity
    assert c["t:t_run"]["progress"] == 0.4 and c["t:t_run"]["progressText"] == "2 of 5 steps"
    assert c["a:a_run"]["progressText"] == "1 of 4 steps"


def test_each_page_opens_on_its_own_cards():
    c = _by_id(_build())
    assert c["t:t_done"]["page"] == "home" and c["t:t_inv"]["page"] == "brain"
    assert c["a:a_sugg"]["page"] == "data" and c["a:a_wait"]["page"] == "brain"
    assert c["ap:ap_mon"]["page"] == "brain" and c["ap:ap_rule"]["page"] == "settings"
    assert c["n:n_next"]["page"] == "home" and c["n:n_risk"]["page"] == "data"
    assert set(c["f:f_risk"]["pages"]) == {"brain", "home"}
    assert all(set(x["pages"]) <= set(zone.PAGES) and x["page"] in x["pages"] for x in c.values())


def test_columns_keep_their_order_and_the_most_urgent_comes_first():
    cards = _build()
    assert [c["col"] for c in cards] == sorted((c["col"] for c in cards), key=COLS.index)
    needs = [c["kind"] for c in cards if c["col"] == "needs"]
    assert needs[0] == "approval"  # approvals lead what waits on you


def test_every_move_goes_somewhere_else_and_everything_that_changes_something_asks_first():
    for c in _build():
        assert c["ops"], c["id"]
        assert sum(bool(o.get("primary")) for o in c["ops"]) <= 1, c["id"]
        for o in c["ops"]:
            spec = zone.OPS[o["id"]]
            assert o["confirm"] == spec["confirm"]
            if o.get("to"):
                assert o["to"] in COLS and o["to"] != c["col"], (c["id"], o)
            if spec["confirm"]:
                assert spec.get("effect") and spec.get("rule"), o["id"]  # what happens, and how it is governed
        tos = [o["to"] for o in c["ops"] if o.get("to")]
        assert len(tos) == len(set(tos)), c["id"]  # one meaning per column


def test_who_may_do_what():
    viewer = _by_id(_build("viewer"))
    editor = _by_id(_build("editor"))
    owner = _by_id(_build("owner"))
    # a viewer can open everything and change nothing
    assert all(not o["confirm"] for c in viewer.values() for o in c["ops"])
    # approvals reach only admins; an activity waiting for one says so instead
    assert "ap:ap_mon" not in editor and "ap:ap_mon" in owner
    assert not any(o["id"] == "approve" for o in editor["a:a_wait"]["ops"])
    assert editor["a:a_wait"]["status"] == "Waiting for an admin"
    assert any(o["id"] == "approve" and o["to"] == "working" for o in owner["a:a_wait"]["ops"])
    assert any(o["id"] == "act.run" and o["to"] == "working" for o in editor["a:a_sugg"]["ops"])


def test_a_gesture_reaches_only_what_the_card_offers():
    c = _by_id(_build())
    ap = c["ap:ap_mon"]
    assert zone.resolve(ap, {"type": "move", "to": "working"}) == ("approve", "working")
    assert zone.resolve(ap, {"type": "move", "to": "done"}) == ("reject", "done")
    assert zone.resolve(ap, {"type": "move", "to": "suggested"}) == (None, None)
    assert zone.resolve(ap, {"type": "move", "to": "needs"}) == (None, None)  # where it already is
    assert zone.resolve(ap, {"type": "op", "op": "task.cancel"}) == (None, None)  # not this card's
    assert zone.resolve(ap, {"type": "op", "op": "approve"}) == ("approve", "working")
    assert zone.resolve(c["t:t_done"], {"type": "open"}) == ("open", None)
    viewer = _by_id(_build("viewer"))
    assert zone.resolve(viewer["t:t_run"], {"type": "op", "op": "task.cancel"}) == (None, None)


# -- the board in the app ---------------------------------------------------------------------------------------

def _boards(at) -> list[dict]:
    out = []

    def walk(node):
        for ch in (getattr(node, "children", None) or {}).values():
            if getattr(ch, "type", None) == "bidi_component" and ch.proto.component_name.endswith("lyst_activity_zone"):
                out.append(json.loads(ch.proto.json))
            walk(ch)

    walk(at._tree)
    return out


@pytest.mark.parametrize("page,name", [("app_pages/analyst.py", "home"), ("app_pages/data.py", "data"),
                                       ("app_pages/brain.py", "brain"), ("app_pages/settings.py", "settings")])
def test_every_page_has_its_activity_zone(ui, page, name):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page(page)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    boards = _boards(at)
    assert len(boards) == 1 and boards[0]["page"] == name
    assert [c["id"] for c in boards[0]["columns"]] == COLS
    if name == "home":  # the ambiguous question waits on the person, on Home
        mine = [c for c in boards[0]["cards"] if c["col"] == "needs" and "home" in c["pages"]]
        assert any(c["title"] == "What is our margin?" for c in mine)


def test_a_drop_asks_first_then_goes_through_the_approval(ui):  # noqa: F811
    """Approving by dragging an activity to In progress: a confirmation first, the API's approval on Confirm."""
    from lyst.autonomy import TEMPLATES
    from lyst.security import Principal

    from .test_autonomy import _reingest

    lyst = ui
    owner = Principal(lyst.stores.control.scalar("SELECT id FROM users WHERE email = 'avery@northwind.test'"),
                      "avery@northwind.test")
    spec = TEMPLATES["schema_change"] | {"action": {"kind": "propose", "proposal": "investigate",
                                                    "question": "What changed in {subject.id}?"}}
    r = lyst.autonomy.activate(owner, "northwind", lyst.autonomy.save(owner, "northwind", spec)["id"])
    lyst.runtime.decide(owner, r["approval_id"], True)
    lyst.events.dispatch()
    _reingest(lyst, owner, "pnl", extra_col="channel")
    lyst.events.dispatch()
    lyst.autonomy.work()
    act = lyst.autonomy.activities(owner, "northwind")[0]
    assert act["status"] == "awaiting_approval"
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.run()
    card = next(c for c in _boards(at)[0]["cards"] if c["id"] == f"a:{act['id']}")
    assert card["col"] == "needs" and "data" in card["pages"]
    at.session_state["_zone_do"] = {"type": "move", "card": card["id"], "to": "working", "nonce": "1"}
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert lyst.autonomy.activities(owner, "northwind")[0]["status"] == "awaiting_approval"  # nothing ran yet
    assert "audit trail" in _texts(at)  # the confirmation says how it is governed
    ok = [b for b in at.button if b.key == "zone_ok_approve"]
    assert ok
    ok[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert lyst.autonomy.activities(owner, "northwind")[0]["status"] in ("running", "completed")


def test_a_move_the_card_does_not_offer_does_nothing(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    card = next(c for c in _boards(at)[0]["cards"] if c["col"] == "done")
    at.session_state["_zone_do"] = {"type": "move", "card": card["id"], "to": "needs", "nonce": "2"}
    at.run()
    assert not at.exception
    assert not [b for b in at.button if (b.key or "").startswith("zone_ok_")]  # no confirmation, nothing to confirm


# -- the look, light and dark, without a browser -------------------------------------------------------------

SAMPLE = next(c for c in zone.build(tasks=[], activities=[], approvals=APPROVALS[1:2], findings=[], suggestions=[],
                                    role="owner", now=NOW.timestamp()))


def _golden(name: str, html: str) -> None:
    path = GOLDEN / f"{name}.html"
    if os.environ.get("LYST_UPDATE_GOLDEN") == "1" or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html + "\n", encoding="utf-8")
        if os.environ.get("LYST_UPDATE_GOLDEN") != "1":
            pytest.fail(f"{path} was missing and has been written; review it and commit it")
    assert html + "\n" == path.read_text(encoding="utf-8"), f"{name} changed; review, then LYST_UPDATE_GOLDEN=1"


def _stable(card: dict) -> dict:
    return card | {"when": "5 min ago"}


def test_the_confirmation_renders_as_reviewed():
    _golden("confirm-approve", zone.preview(_stable(SAMPLE), "approve", "working"))
    _golden("confirm-dismiss", zone.preview(_stable(SAMPLE) | {"title": "<b>not markup</b>"}, "act.dismiss"))


def test_the_confirmation_is_escaped_and_every_class_styled():
    html = zone.preview(_stable(SAMPLE) | {"title": "<script>x</script>"}, "approve", "working")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'aria-label="From Needs you to In progress"' in html
    sheet = kit._CSS + zone.CSS
    used = {c for attr in re.findall(r'class="([^"]+)"', html) for c in attr.split()}
    assert not sorted(c for c in used if f".{c}" not in sheet)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_token_the_board_uses_is_defined(mode):
    defined = set(re.findall(r"(--lyst-[a-z0-9-]+):", tokens.css(mode)))
    for css in ((HERE / "zone.css").read_text(encoding="utf-8"), zone.CSS):
        missing = sorted(set(re.findall(r"var\((--lyst-[a-z0-9-]+)", css)) - defined)
        assert not missing, (mode, missing)


def test_the_board_hard_codes_no_colour_outside_its_fallbacks():
    css = (HERE / "zone.css").read_text(encoding="utf-8")
    for line in css.splitlines():
        for hexcol in re.findall(r"#[0-9a-fA-F]{3,6}\b", line):
            assert "var(--lyst-" in line, f"raw colour {hexcol} outside a token fallback: {line.strip()}"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_column_and_status_colours_read_on_both_themes(mode):
    c = tokens.colors(mode)
    for tone in ("good", "warn", "bad", "info", "link"):
        for bg in ("bg", "surface", "raised"):
            assert tokens.contrast(c[tone], c[bg]) >= 3.0, (mode, tone, bg)


def test_every_icon_and_kind_is_drawn():
    js = (HERE / "zone.js").read_text(encoding="utf-8")
    drawn = set(re.findall(r"^\s{2}([a-z]+): '", js, re.M))
    assert {o["icon"] for o in zone.OPS.values()} <= drawn
    assert {"task", "activity", "approval", "finding", "suggestion", "dots"} <= drawn


def test_the_board_explains_its_keys_to_screen_readers():
    js = (HERE / "zone.js").read_text(encoding="utf-8")
    for needle in ('aria-live="polite"', 'role="radiogroup"', '"role", "menu"', 'role="menuitem"', "aria-roledescription",
                   "aria-describedby", "prefers-reduced-motion", '"ContextMenu"', "Escape"):
        assert needle in js, needle
