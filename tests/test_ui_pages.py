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
    assert title in _texts(at) or any(title in str(h.value) for h in at.get("html")), page


def test_home_shows_suggestions_from_metrics(ui):
    """What waits on the person is on Home's activity zone (tests/test_ui_zone.py); the suggestions come from metrics."""
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception
    pills = at.get("button_group")
    assert pills and any("operating margin" in str(o).lower() for p in pills for o in p.proto.options)


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
