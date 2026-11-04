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


def test_owners_open_answers_at_the_business_depth_with_trust_in_words(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Why did operating margin decline in Q3 2026?")["result"]
    at = _answer(r, role="owner")
    view = at.get("button_group")[0]
    assert view.value == "Summary"
    assert not at.dataframe[1:]  # only the checks (inside the collapsed method); tables wait for a deeper view
    captions = " ".join(c.value for c in at.caption)
    assert "checks passed" in captions and "recomputed independently" in captions
    assert "**What it means**" in " ".join(m.value for m in at.markdown)


def test_trust_line_and_headings():
    assert trust_line({"status": "complete", "confidence": {"level": "verified", "checks": [{"passed": True}] * 3}}) \
        .startswith("All 3 checks passed")
    assert "1 of 2" in trust_line({"status": "complete", "confidence": {"level": "partial",
                                                                         "checks": [{"passed": True}, {"passed": False}]}})
    assert heading("prior_rate") == "Prior rate" and heading("region") == "Region"


def test_an_answer_offers_watch_repeat_and_download(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    rows = [b for b in at.button if b.label == "Open"]
    assert rows
    rows[-1].click().run()  # the verified margin answer from the fixture
    assert not at.exception, [e.value for e in at.exception]
    labels = set(_popovers(at.main))
    assert {"Watch this metric", "Repeat it", "Download"} <= labels


def test_work_speaks_in_business_words(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/work.py")
    at.query_params["view"] = "recurring"
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
    body = _texts(at)
    assert "Get started" in body and "Keep watch" in body
    assert "at a glance" in body  # the business pulse above the suggestions


def test_brain_shows_the_health_of_definitions(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
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
    import pathlib  # noqa: PLC0415

    src = pathlib.Path(__file__).parents[1] / "src/lyst/ui/app.py"
    code = src.read_text()
    start = code.index("def _destination")
    ns: dict = {}
    exec(code[start:code.index("@st.dialog(\"Search\"")], ns)  # noqa: S102  (the pure routing function only)
    dest = ns["_destination"]
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
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "CRM export" in _texts(at) and "feeds 1 dataset" in _texts(at)
    assert any("badge[" in m.value and "Healthy" in m.value for m in at.markdown)


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
    at.switch_page("app_pages/work.py")
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
    at.switch_page("app_pages/work.py")
    at.query_params["view"] = "recurring"
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
