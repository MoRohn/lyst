# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Every page of the interface, rendered headlessly against a real (in-process) API on the demo workspace:
no page may raise, each opens with its header, and empty and populated states both render."""

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from lyst.api import create_app

from .conftest import ask

PAGES = {
    "app_pages/analyst.py": "What should Lyst work on?",
    "app_pages/data.py": "Data",
    "app_pages/brain.py": "Brain",
    "app_pages/settings.py": "Settings",
}


@pytest.fixture
def ui(seeded, monkeypatch):
    lyst, owner = seeded
    ask(lyst, owner, "Why did operating margin decline in Q3 2026?")  # some history and a compiled workflow
    ask(lyst, owner, "What is our margin?")  # needs input
    monkeypatch.setenv("LYST_TOKEN", lyst.guard.issue_token(owner))
    monkeypatch.setenv("LYST_UI_LOCAL", "1")  # what lyst start sets for a loopback-only interface (AppTest has no request)
    with TestClient(create_app(lyst, start_workers=False), base_url="http://127.0.0.1:8770") as http:
        import lyst.ui.client as client_mod

        monkeypatch.setattr(client_mod, "_http", lambda base: http)
        yield lyst


def _texts(at) -> str:
    return " ".join(str(getattr(e, "value", "")) for kind in ("markdown", "caption", "html", "button") for e in getattr(at, kind))


@pytest.mark.parametrize("page,title", PAGES.items())
def test_every_page_renders(ui, page, title):
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    at.switch_page(page)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    hero = (_bar_of(at).get("hero") or {}).get("title", "")  # Home's title is drawn by the bar, which is Home
    assert title in _texts(at) or title in hero or any(title in str(h.value) for h in at.get("html")), page


def _bar_of(at) -> dict:
    from .test_ui_agent import _bar  # noqa: PLC0415

    return _bar(at)


def test_home_shows_suggestions_from_metrics(ui):
    """Home is one composer with at most three starters, from the workspace's approved metrics; what waits on the
    person is the one focus line (tests/test_ui_zone.py), not a board."""
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception
    hero = _bar_of(at)["hero"]
    assert 0 < len(hero["starters"]) <= 3
    assert any("operating margin" in s["text"].lower() for s in hero["starters"])


def test_brain_work_lands_on_needs_you_when_something_waits(ui):
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")  # Work lives in Brain
    at.query_params["view"] = "work"
    at.run()
    assert not at.exception
    assert "Waiting on you" in _texts(at)


def test_old_work_links_land_in_brain_work(ui):
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception
    at.switch_page("app_pages/work.py")  # an old /work?view=recurring link or bookmark
    at.query_params["view"] = "recurring"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    body = _texts(at)
    assert "Scheduled questions" in body and "Watched metrics" in body  # Brain › Work › Recurring
    assert any("Brain" in str(h.value) for h in at.get("html"))


def test_an_open_answer_has_a_way_back_to_the_home_front_page(ui):
    """Back leaves the conversation for Home's front page (the composer with Lyst's mark); the conversation stays
    under Recent. Opening Home with no task in the URL (the Home icon, "go to Home") lands on the front page too."""
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    tasks = _bar_of(at)["hero"]["recent"]
    task = next(r["task"] for r in tasks if r.get("task"))
    at.query_params["task"] = task
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not _bar_of(at).get("hero")  # the answer is open
    at.button(key="lyst_back_home").click().run()
    assert not at.exception, [e.value for e in at.exception]
    hero = _bar_of(at)["hero"]
    assert hero and "task" not in at.query_params and at.session_state["task_id"] is None
    assert any(r.get("task") == task for r in hero["recent"])  # still reachable under Recent

    at.query_params["task"] = task
    at.run()
    assert not _bar_of(at).get("hero")
    at.query_params.pop("task")  # what the Home icon does: Home with no task in the URL
    at.run()
    assert _bar_of(at).get("hero"), "Home without a task in the URL must be the front page"
