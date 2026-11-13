# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Business-user defaults (docs/UX_STRATEGY.md, Part A): business depth first, trust explained in words, every
recommendation a button, plain words instead of system identifiers."""

import json

from streamlit.testing.v1 import AppTest

from lyst.ui.components import heading, trust_line

from .conftest import ask
from .test_ui_pages import _texts, ui  # noqa: F401, F811  (fixture)


def _popovers(block):
    if getattr(block, "type", None) == "popover":
        yield block.proto.popover.label
    for child in getattr(block, "children", {}).values():
        yield from _popovers(child)


def _answer(result, role="owner"):
    def app(result_json, role):
        import json as _j

        import streamlit as st

        from lyst.ui.components import answer

        st.session_state.role = role
        answer(_j.loads(result_json), key="t")

    at = AppTest.from_function(app, args=(json.dumps(result, default=str), role))
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def test_owners_open_answers_on_the_result_with_trust_in_words(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Why did operating margin decline in Q3 2026?")["result"]
    at = _answer(r, role="owner")
    assert not at.get("button_group")  # no depth switch: the details are one click away instead
    assert all(not e.proto.expanded for e in at.expander)  # tables and checks wait in closed layers
    captions = " ".join(c.value for c in at.caption)
    assert "checks passed" in captions and "recomputed independently" in captions
    assert "**What to do**" in " ".join(m.value for m in at.markdown)


def test_trust_line_and_headings():
    assert trust_line({"status": "complete", "confidence": {"level": "verified", "checks": [{"passed": True}] * 3}}) \
        .startswith("All 3 checks passed")
    assert "1 of 2" in trust_line({"status": "complete", "confidence": {"level": "partial",
                                                                         "checks": [{"passed": True}, {"passed": False}]}})
    assert heading("prior_rate") == "Prior rate" and heading("region") == "Region"


def test_an_answer_offers_watch_repeat_and_download(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    from .test_ui_agent import open_recent  # noqa: PLC0415

    open_recent(at)  # Home's dock: Recent, the verified margin answer from the fixture
    assert not at.exception, [e.value for e in at.exception]
    labels = set(_popovers(at.main))
    assert {"Keep watch", "Download", "Share", "More"} <= labels  # four menus, not a toolbar
    keep = next(g for g in at.get("button_group") if (g.key or "").startswith("kw_"))
    assert {"Watch this metric", "Repeat it"} <= {o.content for o in keep.proto.options}
    assert any((g.key or "").startswith("vd_") for g in at.get("button_group"))  # Is this right?


def test_work_speaks_in_business_words(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.query_params["work"] = "recurring"
    at.run()
    assert not at.exception
    body = _texts(at)
    assert "Watched metrics" in body and "Scheduled questions" in body and "σ" not in body


def test_pulse_compares_complete_months_as_the_reader(seeded):
    lyst, owner = seeded
    tiles = lyst.monitors.pulse(owner, "northwind")
    assert tiles and len(tiles) <= 6
    m = next(t for t in tiles if t["metric"] == "operating_margin")
    assert m["format"] == "percent" and m["change_text"].endswith("pts")
    assert abs(m["change"] - (m["value"] - m["prior"])) < 1e-12 and len(m["history"]) <= 12
    rev = next(t for t in tiles if t["metric"] == "revenue")
    assert rev["change_text"].endswith("%") and rev["value_text"].startswith("$")


def test_home_guides_a_first_run(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    from .test_ui_agent import _bar  # noqa: PLC0415

    hero = _bar(at)["hero"]
    assert hero["setup"] and hero["setup"]["done"] < hero["setup"]["need"]  # one dock item while setup is unfinished
    # what to ask, from the workspace's own approved measures; what Lyst can answer about is the starters' tooltip
    assert "Lyst can answer about" in hero["about"]
    at.session_state["_bar_do"] = {"type": "setup", "nonce": "s1"}  # the dock's Get started opens the guide
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "Get started" in body and "Let Lyst study the data" in body  # driven by the onboarding status


def test_brain_shows_the_health_of_definitions(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
    at.query_params["view"] = "definitions"  # Brain opens on its Graph; definitions are one section over
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [e.label for e in at.expander]
    assert any("definition issue" in lb or "small gap" in lb for lb in labels)  # the semantic audit, in one line


def test_search_results_open_where_each_thing_lives():
    from lyst.ui.palette import destination as dest  # noqa: PLC0415

    assert dest({"kind": "metric", "name": "revenue"}) == ("app_pages/brain.py", {"metric": "revenue"})
    assert dest({"kind": "column", "name": "pnl.region"}) == ("app_pages/data.py", {"dataset": "pnl"})
    assert dest({"kind": "analysis", "name": "t_1"}) == ("app_pages/analyst.py", {"task": "t_1"})


def test_sources_show_health_and_the_action_to_take(seeded, ui, tmp_path, monkeypatch):  # noqa: F811
    import sqlite3  # noqa: PLC0415

    lyst, owner = seeded  # the same instance the ui fixture serves
    root = tmp_path / "imports"
    root.mkdir()
    monkeypatch.setenv("LYST_IMPORT_ROOT", str(root))
    with sqlite3.connect(root / "crm.sqlite") as c:
        c.execute("CREATE TABLE deals (id INTEGER PRIMARY KEY, amount REAL)")
        c.execute("INSERT INTO deals (amount) VALUES (1.0)")
    conn = lyst.connections.create(owner, "northwind", kind="sqlite", name="CRM export", values={"path": "crm.sqlite"})
    lyst.connections.sync(owner, "northwind", conn["id"], source_table="deals")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.query_params["view"] = "sources"  # sources, their health and uploads to review live in Sources
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "CRM export" in _texts(at) and "feeds 1 dataset" in _texts(at)
    assert any("badge[" in m.value and "Healthy" in m.value for m in at.markdown)
    from .test_ui_data import objects_of  # noqa: PLC0415

    at.query_params.clear()
    at.run()
    crm = next(x for x in objects_of(at)["sources"] if x["label"] == "CRM export")  # a source, as an object on Data
    assert crm["count"] == 1 and "Healthy" in crm["hint"]
    at.query_params.clear()
    at.query_params["connection"] = conn["id"]  # a link to one source opens Sources and its dialog
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.query_params["view"] == ["sources"] or at.query_params["view"] == "sources"


def test_reports_from_answers_say_when_they_need_rebuilding(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    t = next(x for x in lyst.runtime.list(owner, "northwind") if x["status"] == "done" and "decline" in x["title"])
    lyst.artifacts.build(owner, lyst.runtime.get(owner, t["id"]), "pdf")
    spec = lyst.semantic.get(owner, "northwind", "operating_margin")
    lyst.semantic.define(owner, "northwind", spec.model_copy(update={"description": "Operating income over revenue."}))
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.query_params["view"] = "dashboards"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Reports from answers" in _texts(at) and "PDF briefing" in _texts(at)
    assert any("badge[" in m.value and "Needs rebuilding" in m.value for m in at.markdown)


def test_work_and_the_bell_show_what_colleagues_handed_you(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    dana = lyst.guard.create_user("dana@northwind.test", "Dana")
    lyst.guard.add_member(owner, "northwind", dana.user_id, "admin")
    t = next(x for x in lyst.runtime.list(owner, "northwind") if x["status"] == "done" and "decline" in x["title"])
    lyst.collab.assign(dana, "northwind", "task", t["id"], owner.user_id, note="Check the EMEA numbers")
    lyst.collab.comment(dana, "northwind", "task", t["id"], "@avery@northwind.test does this match the ledger?")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "Assigned to you" in body and "Check the EMEA numbers" in body and "from Dana" in body
    assert any(lb.startswith("Notifications · ") for lb in _popovers(at.sidebar))


def test_schedules_read_as_cadence_and_settings_show_usage(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    lyst.runtime.create_schedule(owner, "northwind", "Monday margin review", "Why did operating margin change?",
                                 at="08:00", weekdays=["mon"], timezone="Europe/London")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.query_params["work"] = "recurring"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Monday at 08:00" in _texts(at)
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "workspace"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Installation:" in _texts(at) and "Model spend this month" in _texts(at) and not at.get("metric")


def test_a_schedule_can_be_paused_and_asked_to_resume_from_recurring(seeded, ui):  # noqa: F811
    """Pause stops it at once; Resume asks an admin again, so the row then waits for approval rather than running."""
    lyst, owner = seeded
    sid = lyst.runtime.create_schedule(owner, "northwind", "Friday revenue check", "What was revenue last week?",
                                       at="09:00", weekdays=["fri"], timezone="UTC")["schedule_id"]

    def recurring() -> AppTest:
        at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
        at.run()
        at.switch_page("app_pages/brain.py")
        at.query_params["view"] = "work"
        at.query_params["work"] = "recurring"
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        return at

    at = recurring()
    assert "Waiting for approval" in _texts(at)
    next(b for b in at.button if b.label == "Pause").click().run()
    assert {s["id"]: s["state"] for s in lyst.runtime.schedules(owner, "northwind")}[sid] == "paused"
    at = recurring()
    assert "Paused" in _texts(at)
    next(b for b in at.button if b.label == "Resume").click().run()
    assert {s["id"]: s["state"] for s in lyst.runtime.schedules(owner, "northwind")}[sid] == "awaiting_approval"


def test_times_ahead_read_as_times_ahead():
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from lyst.ui.kit import when  # noqa: PLC0415

    assert when((datetime.now(UTC) + timedelta(minutes=30)).isoformat()).startswith("in ")
    assert when((datetime.now(UTC) - timedelta(minutes=30)).isoformat()) == "30 min ago"


def test_the_discussion_names_the_assignee_not_their_id(seeded, ui):  # noqa: F811
    lyst, owner = seeded
    sam = lyst.guard.create_user("sam2@northwind.test", "Sam Two")
    lyst.guard.add_member(owner, "northwind", sam.user_id, "editor")
    t = next(x for x in lyst.runtime.list(owner, "northwind") if x["status"] == "done" and "decline" in x["title"])
    lyst.collab.assign(sam, "northwind", "task", t["id"], owner.user_id, note="Check it")

    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.session_state["task_id"] = t["id"]
    at.query_params["task"] = t["id"]
    at.run()
    at.button(key=f"disc_{t['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    caps = " ".join(c.value for c in at.caption)
    assert "assigned to Avery" in caps and owner.user_id not in caps



def test_suggestions_from_the_data_can_be_accepted_where_people_work(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    lyst.onboarding.discover(owner, "northwind")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Define Cost of goods sold" in _texts(at) and any(b.label == "Accept" for b in at.button)
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "workspace"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(m.label == "Industry" for m in at.multiselect)


def test_uploads_wait_for_review_under_sources(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    lyst.ingest.inspect(owner, "northwind", b"region,amount\nEMEA,1\nAPAC,2\n", filename="regions.csv")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.query_params["view"] = "sources"  # sources, their health and uploads to review live in Sources
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "regions.csv" in _texts(at) and any(b.label == "Review" for b in at.button)


def test_a_built_dataset_explains_how_it_is_made(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    from lyst.ui import transforms  # noqa: PLC0415

    assert transforms._check_label("reconciles:revenue") == "Revenue adds up to its source"
    assert transforms._check_label("key_unique") == "One row per key"
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.session_state["data_open"] = {"kind": "dataset", "id": "pnl"}  # an open dataset's More holds it
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.label == "Build a dataset from data Lyst has" for b in at.button)


def test_study_it_runs_discovery_without_crashing(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    out = lyst.onboarding.discover(owner, "northwind")
    from lyst.ui.onboarding import _count  # noqa: PLC0415

    assert _count(out.get("new_proposals")) >= 0 and _count([1, 2]) == 2 and _count(None) == 0


def test_work_shows_what_lyst_prepared_ahead(seeded, ui):  # noqa: F811
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    lyst, owner = seeded  # the same instance the ui fixture serves
    s = lyst.runtime.create_schedule(owner, "northwind", "Monthly finance review",
                                     "Review revenue and operating margin for the latest month", 2592000)
    lyst.runtime.decide(owner, s["approval_id"], True)
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                                [datetime.now(UTC) + timedelta(minutes=30), s["schedule_id"]])
    lyst.anticipation.set_settings(owner, "northwind", {"level": 2})
    assert lyst.anticipation.cycle("northwind")["prepared"] >= 1  # earlier answers may add next steps too
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.query_params["work"] = "recurring"  # ready work waits in Coming up; only what wants a person is in Needs you
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "Monthly finance review" in body and any(b.label == "Run now" for b in at.button)
    assert any("badge[" in m.value and "Ready" in m.value for m in at.markdown)
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "preferences"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "checks the data and definitions an upcoming run needs" in _texts(at)


def test_next_steps_sit_under_their_answer_not_in_work(seeded, ui):  # noqa: F811
    from lyst.ui import anticipate  # noqa: PLC0415

    assert not anticipate._wants_person({"kind": "next_best_action", "verdict": "blocked"})
    assert anticipate._wants_person({"kind": "early_warning", "verdict": "verified"})
    assert not anticipate._wants_person({"kind": "early_warning", "verdict": "unverified"})
    assert anticipate._wants_person({"kind": "recurring_prep", "verdict": "blocked", "status": "surfaced"})
    assert not anticipate._wants_person({"kind": "recurring_prep", "verdict": "ready", "status": "prepared"})


def test_what_if_and_decisions_are_reachable_where_people_work(seeded, ui):  # noqa: F811
    from lyst.ui.scenarios import delta, fmt  # noqa: PLC0415

    assert fmt(0.3539, "percent") == "35.4%" and fmt(9148122.0, "currency") == "$9.15M"
    assert delta({"format": "percent", "delta": -0.0064}) == "-0.6 pts"
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.query_params["view"] = "saved analyses"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Quarterly P&L drivers" in _texts(at) and any(b.label == "Explore" for b in at.button)
    rows = [b for b in at.button if b.label == "Explore"]
    rows[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("change" in s.label for s in at.slider)  # one slider per driver, in the explorer dialog
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.query_params["work"] = "activity"
    at.run()
    next(g for g in at.get("button_group") if g.key == "act_filter").set_value("Decisions").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.label == "Compare options" for b in at.button)


def test_health_foresight_and_attention_are_where_people_look(seeded, ui):  # noqa: F811
    from lyst.ui import anticipate  # noqa: PLC0415

    assert anticipate._wants_person({"kind": "health_foresight", "verdict": "risk"})
    assert not anticipate._wants_person({"kind": "health_foresight", "verdict": "watch"})
    assert anticipate.VERDICT["risk"][0] == "At risk"
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(t.label == "Tell me about them" for t in at.sidebar.toggle)  # personal attention, in Notifications


def test_people_can_ask_how_an_answer_was_figured_and_see_data_sensitivity(seeded, ui):  # noqa: F811
    from lyst.ui.governance import _lines  # noqa: PLC0415

    assert _lines([{"figure": "32.5%", "steps": [{"description": "approved operating margin"}]}]) == \
        ["32.5%: approved operating margin"]
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    from .test_ui_agent import open_recent  # noqa: PLC0415

    open_recent(at)
    assert not at.exception, [e.value for e in at.exception]
    assert any("How was this figured?" in str(o) for g in at.get("button_group") for o in g.proto.options)
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "privacy"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Restricted data:" in _texts(at)  # the policy in one sentence; its controls one step further
    at.button(key="set_policy_open").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(s.label == "Restricted data is read by" for s in at.selectbox)


def test_source_of_truth_and_webhooks_have_a_place(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "connections"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Lyst can POST a signed message" in _texts(at)
    from lyst.ui.app_pages.data import health  # noqa: PLC0415  (pure function; the page itself runs under AppTest)

    assert health({"authority": "authoritative", "risks": {}}) == ("Source of truth", "verified")


def test_voice_sits_in_the_agent_bar_and_has_its_settings(seeded, ui):  # noqa: F811
    from .test_ui_agent import _bar  # noqa: PLC0415

    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]  # Home renders with the mic in the agent bar
    assert _bar(at)["voice"] is True
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "voice"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "audio does not leave it" in body and "never approves or confirms" in body
    assert any(s.label == "Talk by" for s in at.selectbox)


def test_an_ended_voice_session_is_replaced_and_the_turn_sent_again(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    old = lyst.voice.start(owner, "northwind")["id"] if hasattr(lyst.voice, "start") else None
    if old is None:
        import pytest  # noqa: PLC0415

        pytest.skip("voice service has no start()")
    lyst.voice.end(owner, "northwind", old)

    def app(old_sid):
        import streamlit as st

        from lyst.ui import voice

        st.session_state.workspace = "northwind"
        st.session_state.setdefault("voice_sid_northwind", old_sid)
        r = voice._turn(old_sid, {"text": "what was revenue last quarter"})
        st.session_state["r_intent"] = (r or {}).get("intent")

    at = AppTest.from_function(app, args=(old,))
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert at.session_state["voice_sid_northwind"] != old and at.session_state["r_intent"] == "question"


def test_connections_proposed_from_an_api_description_wait_under_sources(seeded, ui):  # noqa: F811
    from tests.test_connector_drafts import SPEC  # noqa: PLC0415

    lyst, owner = seeded  # the same instance the ui fixture serves
    made = lyst.connections.drafts.create(owner, "northwind", name="Procurement", openapi=SPEC)
    assert made
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.query_params["view"] = "sources"  # sources, their health and uploads to review live in Sources
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "Procurement purchase_orders" in body and "/purchase-orders" in body



def test_settings_advanced_reports_how_working_ahead_went(seeded, ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "advanced"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "prepared before it was needed" in body and "runs fixed before they failed" in body
def test_specializations_and_python_have_their_place(seeded, ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
    at.query_params["view"] = "specializations"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "describe the work on Home" in body and "Data Analyst" in body
    rows = [b for b in at.button if b.label == "Open"]
    rows[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.label == "Ask this" for b in at.button)  # example requests become real questions
    from lyst.ui import workbench  # noqa: PLC0415

    env = workbench.available()
    at.switch_page("app_pages/data.py")
    at.query_params["view"] = "sql"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    if env.get("available"):
        assert any(t.label == "Python" for t in at.tabs)


def test_admins_can_pack_the_workspace_with_a_confirmed_passphrase(seeded, ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "workspace"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    make = next(b for b in at.button if b.label == "Make a pack")
    assert make.disabled  # nothing until the passphrase is long enough and typed twice
    at.text_input(key="pk_p1").set_value("correct horse battery").run()
    at.text_input(key="pk_p2").set_value("correct horse battery").run()
    next(b for b in at.button if b.label == "Make a pack").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.label == "Download" for b in at.get("download_button"))


def test_a_specialization_shows_the_evidence_behind_its_maturity(seeded, ui):  # noqa: F811
    from lyst.specialists.maturity import evidence  # noqa: PLC0415

    sid = next((k for k, v in evidence().items() if v.get("operational")), None)
    if sid is None:
        import pytest  # noqa: PLC0415

        pytest.skip("no recorded evidence in this catalog")

    def app(sid):
        import streamlit as st

        from lyst.ui import specialists

        st.session_state.workspace = "northwind"
        specialists.detail(sid)

    at = AppTest.from_function(app, args=(sid,))
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert any("Representative task passed" in c.value for c in at.caption)


def test_backups_and_recovery_show_measured_objectives_to_installation_admins(seeded, ui):  # noqa: F811
    import lyst.ui.client as client_mod  # noqa: PLC0415

    try:
        client_mod.client().get("/v1/installation/continuity")
    except client_mod.APIError:
        import pytest  # noqa: PLC0415

        pytest.skip("the test owner is not an installation administrator")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "advanced"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "Data you could lose" in body and "Time to restore" in body and "never contain the master key" in body
    next(b for b in at.button if b.label == "Back up now").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.label == "Test a restore" for b in at.button)


def test_memory_review_is_quiet_when_nothing_is_stale(seeded, ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
    at.query_params["view"] = "memory"
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def test_health_lists_problems_first_with_what_to_do(seeded, ui):  # noqa: F811
    import lyst.ui.client as client_mod  # noqa: PLC0415

    try:
        d = client_mod.client().get("/v1/diagnostics")
    except client_mod.APIError:
        import pytest  # noqa: PLC0415

        pytest.skip("the test owner is not an installation administrator")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "advanced"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "Check again" in body or any(b.label == "Check again" for b in at.button)
    bad = [c for c in d["checks"] if c["status"] != "ok" and c.get("fix")]
    if bad:
        assert "What to do:" in body and bad[0]["fix"].split(".")[0] in body


def test_a_finished_answer_can_be_published_and_found_in_work(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    t = next(x for x in lyst.runtime.list(owner, "northwind") if x["status"] == "done" and "decline" in x["title"])
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.session_state["task_id"] = t["id"]
    at.query_params["task"] = t["id"]
    at.run()
    at.button_group(key=f"sh_{t['id']}").set_value("Publish").run()
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.button if b.key == f"pub_go_{t['id']}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert lyst.publishing.list(owner, "northwind")  # a real publication now exists
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.query_params["work"] = "activity"
    at.run()
    at.button_group(key="act_filter").set_value("Published").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.label == "Revoke" for b in at.button) and any(b.label == "View" for b in at.button)


def test_an_answers_next_step_is_one_card_without_echoing_the_question():
    from lyst.ui import anticipate  # noqa: PLC0415

    a = {"objective": "Look inside EMEA by product line", "actions": [{"id": "dismiss"}, {"id": "run_now"}],
         "reasons": ['EMEA accounts for 107% of the change in Q3 2026 (your question: "Why did (it) decline?").', "x"]}
    assert anticipate.short_reason(a) == "EMEA accounts for 107% of the change in Q3 2026."
    assert anticipate._primary(a) == "run_now"
    assert anticipate._primary({"actions": ["dismiss"]}) is None


def test_how_an_answer_was_figured_reads_as_words_not_sql():
    from lyst.ui import governance  # noqa: PLC0415

    assert governance.readable('CAST(SUM("operating_income") AS DOUBLE) / NULLIF(SUM("revenue"), 0)') == \
        "Operating income ÷ revenue"
    assert governance.readable("COUNT(DISTINCT x)") == "COUNT(DISTINCT x)"  # left as written when it can't simplify
    assert governance._dataset("pnl v1", {"pnl": "Monthly P&L"}) == "Monthly P&L, version 1"
    assert governance._dataset("other v2", {}) == "other v2"


def test_the_next_row_offers_nothing_the_answer_already_shows():
    from lyst.ui import anticipate  # noqa: PLC0415

    r = {"supporting": {"metric": {"spec": {"title": "Operating margin", "dimensions": ["region"]}}},
         "charts": [{"type": "line"}]}
    opts, picks = anticipate.follow_options(r, None)
    assert "Forecast operating margin" in opts and not any("trend" in o for o in opts) and not picks
    lead = {"objective": "See where it is heading", "actions": [{"id": "run_now", "question": "Forecast operating margin for 2 quarters"}]}
    other = {"objective": "Watch it", "actions": [{"id": "watch"}]}
    opts, picks = anticipate.follow_options(r, [lead, other])
    assert "Forecast operating margin" not in opts  # the card already asks it
    assert list(picks.values()) == [other]  # the card itself is not offered again


def test_a_line_chart_with_series_draws_one_named_line_each():
    import json  # noqa: PLC0415

    from streamlit.testing.v1 import AppTest  # noqa: PLC0415

    def page():
        from lyst.ui.components import chart  # noqa: PLC0415

        chart({"type": "line", "title": "Revenue by region", "format": "number", "x": ["2026-07", "2026-08", "2026-09"],
               "y": [30, 33, 36], "series": [{"name": "EMEA", "y": [10, 11, 12]}, {"name": "Americas", "y": [20, 22, 24]}]})

    at = AppTest.from_function(page)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    fig = json.loads(at.get("plotly_chart")[0].proto.spec)
    assert [t["name"] for t in fig["data"]] == ["EMEA", "Americas"]  # one line each, no latest-point marker
    assert fig["layout"]["showlegend"] is True
    from lyst.ui.components import _alt  # noqa: PLC0415

    assert "EMEA ending at 12" in _alt({"type": "line", "title": "t", "x": [1, 2, 3], "y": [30, 33, 36],
                                        "series": [{"name": "EMEA", "y": [10, 11, 12]}, {"name": "Americas", "y": [20, 22, 24]}]})


def test_a_workbook_sheet_reads_with_the_column_names_under_its_title():
    import io

    from openpyxl import Workbook

    from lyst.ui.artifacts import sheet_tables

    wb = Workbook()
    ws = wb.active
    ws.title = "Change by region"
    ws["A1"], ws["A2"] = "Change in operating margin by region", "4 rows"
    for j, name in enumerate(["Region", "Prior rate", "Current rate"], start=1):
        ws.cell(row=4, column=j, value=name)
    for i, (region, a, b) in enumerate([("EMEA", 0.368, 0.24), ("APAC", 0.354, 0.365)], start=5):
        ws.cell(row=i, column=1, value=region)
        for j, v in ((2, a), (3, b)):
            ws.cell(row=i, column=j, value=v).number_format = "0.0%"
    buf = io.BytesIO()
    wb.save(buf)
    (sheet,) = sheet_tables(buf.getvalue())
    assert sheet["title"] == "Change in operating margin by region" and sheet["note"] == "4 rows"
    assert list(sheet["frame"].columns) == ["Region", "Prior rate", "Current rate"]
    assert sheet["frame"]["Prior rate"].round(1).tolist() == [36.8, 35.4] and "Prior rate" in sheet["config"]


def test_the_preview_drops_the_title_stamp_and_folded_data_and_steps_headings_down():
    from lyst.ui.artifacts import _inline

    text = ("# Why did margin fall?\n\n*Lyst · 11 Nov 2026 · task t_1 · Verified*\n\n## Answer\n\n**Down 3.5 pts**\n\n"
            "![chart](data:image/svg+xml;base64,AAA)\n\n<details><summary>Chart data</summary>\n\n| a |\n</details>\n\n"
            "## What to do\n")
    out = _inline(text)
    assert not out.startswith("# ") and "*Lyst · " not in out and "<details>" not in out and "| a |" not in out
    assert "#### Answer" in out and "#### What to do" in out and "data:image/svg+xml" in out


def test_a_data_table_shows_money_columns_with_separators_and_the_same_places():
    import pandas as pd

    from lyst.ui import kit

    df = pd.DataFrame({"revenue": [930752.32, 386107.3], "delta": [-1.5, 2.25], "year": [2024, 2025],
                       "units": [1.0, 2.0], "region": ["EMEA", "APAC"]})
    cfg = kit.number_columns(df)
    assert cfg["revenue"]["type_config"]["format"] == "accounting"  # 386,107.30 under 930,752.32
    assert cfg["delta"]["type_config"]["format"] == "localized"  # a negative keeps its minus sign, not brackets
    assert "year" not in cfg and "units" not in cfg and "region" not in cfg


def test_a_pdf_page_reads_as_headings_bullets_and_a_table_without_its_chart_axis():
    from lyst.ui.artifacts import page_blocks

    text = ("Margin\nLyst · page 1\nANSWER\nOperating margin was 32.5% in Q3 2026, and\n-3.4 pts versus Q3 2025.\n"
            "EVIDENCE\n\x7f EMEA moved margin by -3.8 pts, 107% of the total\nchange.\nOperating margin by month\n"
            "32%\n34%\n36%\n38%\nJan 2024\nJul 2024\nJan 2025\nBY REGION\nRegion\nPrior rate\nCurrent rate\n"
            "EMEA\n36.8%\n24.0%\nAPAC\n35.4%\n36.5%\n")
    blocks = page_blocks(text, "Margin")
    assert blocks[0] == ("h", "Answer")
    assert blocks[1] == ("p", "Operating margin was 32.5% in Q3 2026, and -3.4 pts versus Q3 2025.")
    assert ("li", "EMEA moved margin by -3.8 pts, 107% of the total change.") in blocks
    flat = " ".join(str(t) for _, t in blocks)
    assert "38%" not in flat and "Jul 2024" not in flat and "page 1" not in flat  # the axis and running head go
    assert blocks[-1] == ("table", (3, ["Region", "Prior rate", "Current rate", "EMEA", "36.8%", "24.0%",
                                        "APAC", "35.4%", "36.5%"]))


def test_a_slide_reads_without_the_footer_every_slide_repeats():
    from lyst.ui.artifacts import slide_lines

    deck = "Why did margin fall?"
    lines = ["Lyst · Why did margin fall?", "WHAT IT MEANS\n• EMEA drove it.\n• Act there.", "Lyst · 11 Nov 2026"]
    assert slide_lines(lines, deck) == [("kicker", "What it means"), ("li", "EMEA drove it."), ("li", "Act there."),
                                        ("p", "Lyst · 11 Nov 2026")]


def test_a_tablet_keeps_page_tools_on_one_row_and_settings_words_whole():
    """Between a phone and a desktop, Data's and Brain's tools sit in one row under the title, Settings' list keeps a
    width that holds each word whole, and an empty activity column shrinks to its name."""
    from pathlib import Path

    ui = Path(__file__).resolve().parents[1] / "src" / "lyst" / "ui"
    kit = (ui / "kit.py").read_text(encoding="utf-8")
    tablet = kit.split("@media (min-width: 641px) and (max-width: 1100px)", 1)[1].split("\n}\n", 1)[0]
    assert "st-key-data_tools" in tablet and "st-key-brain_tools" in tablet and "st-key-set_nav" in tablet
    assert ".z-col:not(.has)" in (ui / "zone_component" / "zone.css").read_text(encoding="utf-8")
