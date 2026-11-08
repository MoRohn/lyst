# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Settings, as a native settings window: seven sections in one list, Lyst's own check of the setup above them, older
links still landing, and the technical controls one step further in sheets."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from .test_ui_pages import _texts, ui  # noqa: F401  (fixture)

SEVEN = ["account", "workspaces", "connections", "models", "privacy", "preferences", "advanced"]


def _settings(view: str | None = None) -> AppTest:
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/settings.py")
    if view:
        at.query_params["view"] = view
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _nav(at) -> list[str]:
    return [b.key.removeprefix("set_nav_").removeprefix("on_") for b in at.button if (b.key or "").startswith("set_nav_")]


def test_seven_sections_in_one_list_opening_on_account(ui):  # noqa: F811
    at = _settings()
    assert _nav(at) == SEVEN
    assert at.query_params.get("view") in ("account", ["account"])
    assert any((b.key or "") == "set_nav_on_account" for b in at.button)
    assert "Your workspaces" in _texts(at) and any(b.key == "set_signout" for b in at.button)


def test_older_links_still_land(ui):  # noqa: F811
    for old, new in (("workspace", "workspaces"), ("voice", "models"), ("models", "models")):
        at = _settings(old)
        assert at.query_params.get("view") in (new, [new])


def test_lyst_says_what_needs_a_look_and_where_to_fix_it(ui):  # noqa: F811
    at = _settings("account")
    # the demo has no model connected: Lyst says so, and points at the section that fixes it
    assert "No model is connected" in _texts(at)
    hop = next(b for b in at.button if (b.key or "").startswith("set_fix_") and b.label == "Open Models & Voice")
    hop.click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.query_params.get("view") in ("models", ["models"])


def test_every_section_renders_and_technical_controls_wait_in_sheets(ui):  # noqa: F811
    for view in SEVEN:
        at = _settings(view)
        assert not at.get("metric"), view
    at = _settings("privacy")
    assert not any(s.label == "Restricted data is read by" for s in at.selectbox)  # a sentence first
    assert "Audit trail" in _texts(at)
