# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data, as objects: sources and datasets drawn as things to pick, the picked dataset revealed below them with three
moves (Explore, Analyze, More), and the deeper places (Sources, Dashboards, Saved analyses, SQL) one menu away."""

from __future__ import annotations

import json

from streamlit.testing.v1 import AppTest

from lyst.ui.app_pages import data as _data_page  # noqa: F401  (imported only to fail early on a syntax error)

from .test_ui_pages import _texts, ui  # noqa: F401  (fixture)


def objects_of(at) -> dict:
    """Data's objects, as Python sent them to the page."""
    found = []

    def walk(node):
        for ch in (getattr(node, "children", None) or {}).values():
            if getattr(ch, "type", None) == "bidi_component" and ch.proto.component_name.endswith("lyst_data_objects"):
                found.append(json.loads(ch.proto.json))
            walk(ch)

    walk(at._tree)
    assert len(found) == 1, f"{len(found)} object views"
    return found[0]


def _open_data(**state) -> AppTest:
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_data_opens_as_objects_not_a_dashboard(ui):  # noqa: F811
    at = _open_data()
    o = objects_of(at)
    names = {d["name"] for d in o["datasets"]}
    assert {"pnl", "pnl_ledger"} <= names and o["source"] == "all" and o["selected"] is None
    ledger = next(d for d in o["datasets"] if d["name"] == "pnl_ledger")
    assert ledger["tone"] == "bad" and "to fix" in ledger["status"] and "through" in ledger["meta"]  # rely on it?
    pnl = next(d for d in o["datasets"] if d["name"] == "pnl")
    assert pnl["types"] and set(pnl["types"]) <= set("ntdbo") and "n" in pnl["types"]  # its face: its columns' kinds
    assert [s["id"] for s in o["sources"]][:2] == ["all", "files"]
    # nothing about a dataset is on the page until one is picked: no tabs strip, no library rail, no ask dock
    keys = {b.key for b in at.button}
    assert not any((k or "").startswith(("lib_", "dx_", "da_")) for k in keys)
    assert not [g for g in at.get("button_group") if g.key in ("sec_data_view", "ds_layout")]


def test_picking_a_dataset_reveals_it_with_three_moves(ui):  # noqa: F811
    at = _open_data()
    at.session_state["_data_nonce"] = None
    at.session_state["data_pick"] = "pnl"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert objects_of(at)["selected"] == "pnl"
    keys = {b.key for b in at.button}
    assert {"dx_pnl", "da_pnl", "data_close"} <= keys
    assert "one row per month" in _texts(at)  # Lyst says what it is, in a sentence
    assert not any(g.key == "dsv_pnl" for g in at.get("button_group"))  # depth waits for Explore
    next(b for b in at.button if b.key == "dx_pnl").click().run()
    assert not at.exception, [e.value for e in at.exception]
    view = next(g for g in at.get("button_group") if g.key == "dsv_pnl")
    assert [o.content for o in view.proto.options] == ["Preview", "Columns", "Quality", "Relationships", "SQL", "Contract"]
    for pick in ("Columns", "Quality", "Relationships", "SQL", "Contract"):
        next(g for g in at.get("button_group") if g.key == "dsv_pnl").set_value(pick).run()
        assert not at.exception, (pick, [e.value for e in at.exception])
    next(b for b in at.button if b.key == "data_close").click().run()
    assert objects_of(at)["selected"] is None


def test_a_source_brings_its_datasets_forward_and_search_narrows(ui):  # noqa: F811
    at = _open_data(data_source="files")
    assert objects_of(at)["source"] == "files" and objects_of(at)["datasets"]
    at.session_state["data_source"] = "all"
    at.text_input(key="ds_find").set_value("ledger").run()
    assert [d["name"] for d in objects_of(at)["datasets"]] == ["pnl_ledger"]


def test_the_deeper_places_are_one_menu_away_and_come_back(ui):  # noqa: F811
    at = _open_data()
    keys = {b.key for b in at.button}
    assert {"data_go_sources", "data_go_dashboards", "data_go_saved_analyses", "data_go_sql", "data_go_map"} <= keys
    at.query_params["view"] = "sql"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(b.key == "sql_run" for b in at.button) and any(b.key == "data_back" for b in at.button)
