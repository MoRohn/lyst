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


def test_data_rows_say_whether_a_table_can_be_relied_on(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "data through" in body  # freshness from the catalog, not just the load time
    # the demo ledger repeats a row at its grain: the list flags it before anyone opens it
    assert any("badge[" in m.value and "to fix" in m.value for m in at.markdown)


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
    assert any(b.label == "1 source · all working" for b in at.button)  # the header's status pill
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
    at.query_params["view"] = "workspace"
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
    at.query_params["view"] = "workspace"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(s.label == "Restricted data is read by" for s in at.selectbox)


def test_source_of_truth_and_webhooks_have_a_place(seeded, ui):  # noqa: F811
    lyst, owner = seeded  # the same instance the ui fixture serves
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/settings.py")
    at.query_params["view"] = "workspace"
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


def test_data_is_a_workspace_with_a_library_and_three_layouts(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    keys = {b.key for b in at.button}
    assert {"lib_ok_ds_pnl", "lib_bad_ds_pnl_ledger", "lib_ok_m_operating_margin"} <= keys  # the library rail
    assert "Ask about the data" in set(_popovers(at.main))  # Ask, docked beside the tabs
    tabs = next(g for g in at.get("button_group") if g.key == "sec_data_view")
    assert len(tabs.proto.options) == 5  # Datasets, Sources, Dashboards, Saved analyses, SQL
    layout = next(g for g in at.get("button_group") if g.key == "ds_layout")
    layout.set_value("List").run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Relationships · " in _texts(at) and any(b.label == "Open" for b in at.button)
    next(g for g in at.get("button_group") if g.key == "ds_layout").set_value("Map").run()
    assert not at.exception, [e.value for e in at.exception]
    at.text_input(key="ds_find").set_value("ledger").run()  # the rail's search narrows the library and the canvas
    keys = {b.key for b in at.button}
    assert "lib_bad_ds_pnl_ledger" in keys and "lib_ok_ds_pnl" not in keys
    next(b for b in at.button if b.key == "lib_bad_ds_pnl_ledger").click().run()  # the dataset opens with its actions
    assert not at.exception, [e.value for e in at.exception]
    assert any(g.key == "dsv_pnl_ledger" for g in at.get("button_group"))
    assert "More" in {lb for root in at._tree.children.values() for lb in _popovers(root)}


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
