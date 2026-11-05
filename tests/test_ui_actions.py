# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Contextual actions: the menu shows only what the engine offers, and disappears on an engine without the API."""

from lyst.ui import actions
from lyst.ui.client import APIError


class _Fake:
    def __init__(self, result):
        self.result = result

    def get(self, path, **_):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_no_actions_api_means_no_menu(monkeypatch):
    import streamlit as st  # noqa: PLC0415

    st.session_state.workspace = "northwind"
    st.session_state.pop("_actions", None)
    monkeypatch.setattr(actions, "client", lambda: _Fake(APIError(404, "Not Found")))
    assert actions.available("metric", "revenue") is None
    st.session_state.pop("_actions", None)
    offered = [{"id": "metric.explain", "label": "Why did revenue change?", "kind": "ask", "needs_approval": False,
                "reason": "Drivers", "question": "Why?"}]
    monkeypatch.setattr(actions, "client", lambda: _Fake(offered))
    assert actions.available("metric", "revenue") == offered
    assert actions._icon(offered[0]) == ":material/psychology_alt:"  # noqa: SLF001


def test_states_map_to_honest_tones():
    assert set(actions.STATE_TONE) == {"Preparing", "Investigating", "Executing", "Verifying", "Awaiting Approval",
                                       "Completed", "Needs Attention"}
