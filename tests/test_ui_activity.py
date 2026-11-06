# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The activity kit as people see it: rules in Settings, activity in Work and on a dataset, the approval surface,
and live updates that never let a stale or repeated event through."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from lyst.autonomy import TEMPLATES
from lyst.ui import activity

from .test_autonomy import _reingest
from .test_ui_pages import _texts, ui  # noqa: F401 - the fixture


def test_the_live_cursor_drops_stale_and_repeated_events():
    """Scenario H in the browser: an event at or below the cursor, or the same event twice, is never applied."""
    evs = [{"seq": 5, "type": "a.b"}, {"seq": 3, "type": "stale"}, {"seq": 5, "type": "a.b"}, {"seq": 7, "type": "c.d"}]
    cursor, fresh = activity.advance(4, evs)
    assert cursor == 7 and [e["seq"] for e in fresh] == [5, 7]
    assert activity.advance(7, evs) == (7, [])


def test_only_what_matters_interrupts():
    me = "u_1"
    assert activity.notable({"type": "activity.changed", "data": {"to": "awaiting_approval"}}, me)
    assert activity.notable({"type": "task.completed", "data": {"created_by": me}}, me)
    assert activity.notable({"type": "task.completed", "data": {"created_by": "someone"}}, me) is None
    assert activity.notable({"type": "dataset.version_created", "data": {}}, me) is None
    assert activity.notable({"type": "activity.changed", "data": {"to": "running"}}, me) is None


def test_one_chip_says_where_an_objects_activity_stands():
    acts = [{"status": "completed"}, {"status": "suggested"}, {"status": "awaiting_approval"}]
    assert activity.summary(acts) == ("Needs your approval", "wait")
    assert activity.summary([{"status": "completed"}]) is None


def _run(page: str):
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page(page)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_settings_offers_rules_and_work_shows_what_they_did(ui):  # noqa: F811
    lyst = ui
    owner_id = lyst.stores.control.scalar("SELECT id FROM users WHERE email = 'avery@northwind.test'")
    from lyst.security import Principal

    owner = Principal(owner_id, "avery@northwind.test")
    at = _run("app_pages/settings.py")
    at.query_params["view"] = "workspace"
    at.run()
    text = _texts(at)
    assert "Event rules" in text and TEMPLATES["schema_change"]["name"] in text
    # adopt and turn on the schema-change rule, then change a schema
    rid = lyst.autonomy.save(owner, "northwind", TEMPLATES["schema_change"])["id"]
    lyst.autonomy.activate(owner, "northwind", rid)
    lyst.events.dispatch()
    _reingest(lyst, owner, "pnl", extra_col="channel")
    lyst.events.dispatch()
    lyst.autonomy.work()
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.query_params["work"] = "activity"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    next(g for g in at.get("button_group") if g.key == "act_filter").set_value("Automatic").run()
    assert not at.exception
    text = _texts(at)
    assert "Review what depends on it" in text and "Monthly P&L" in text


def test_a_proposal_shows_its_approval_where_it_happened(ui):  # noqa: F811
    lyst = ui
    from lyst.security import Principal

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
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.query_params["work"] = "activity"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    next(g for g in at.get("button_group") if g.key == "act_filter").set_value("Automatic").run()
    text = _texts(at)
    assert "Needs your approval" in " ".join(str(b.value) for b in at.get("badge")) or "Needs your approval" in text
    assert "will not run it until an admin approves it" in " ".join(str(i.value) for i in at.info)
    approve = [b for b in at.button if b.label == "Approve" and b.key.startswith("actok_ws_")]
    approve[0].click().run()
    assert lyst.autonomy.activities(owner, "northwind")[0]["status"] in ("running", "completed")
