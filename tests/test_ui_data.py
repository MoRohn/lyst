# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data, as a flow: sources, datasets and the metrics they feed drawn as things to click; whatever is clicked opens as
a sheet over the page (never below the flow), and the deeper places (Sources, Dashboards, Saved analyses, SQL) are one
menu away."""

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


def _dialogs(at) -> list:
    """The sheets drawn over the page (st.dialog), as AppTest sees them."""
    found = []

    def walk(node):
        for ch in (getattr(node, "children", None) or {}).values():
            if getattr(ch, "type", None) == "dialog":
                found.append(ch)
            walk(ch)

    walk(at._tree)
    return found


def _click(at, kind: str, oid: str) -> AppTest:
    """A click on a thing in the flow, as the component reports it."""
    at.session_state["_data_nonce"] = None
    at.session_state["data_open"] = {"kind": kind, "id": oid}  # what open_sheet keeps, with the path it lights
    at.session_state["data_pick"] = oid if kind == "dataset" else None
    at.session_state["data_source"] = oid if kind == "source" else "all"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_clicking_a_dataset_opens_it_as_a_sheet_over_the_page(ui):  # noqa: F811
    at = _click(_open_data(), "dataset", "pnl")
    assert len(_dialogs(at)) == 1  # over the page, not drawn below the flow
    keys = {b.key for b in at.button}
    assert {"da_pnl", "dsf_pnl_operating_margin"} <= keys  # its moves, and the metrics it feeds
    html = " ".join(str(h.value) for h in at.get("html"))
    assert "lyst-sh-shape" in html and "lyst-sh-stat" in html  # its face and its numbers
    assert "one row per month" in _texts(at)  # Lyst says what it is, in a sentence
    view = next(g for g in at.get("button_group") if g.key == "dsv_pnl")  # its depth, without another click
    assert [o.content for o in view.proto.options] == ["Preview", "Columns", "Quality", "Relationships", "SQL", "Contract"]
    for pick in ("Columns", "Quality", "Relationships", "SQL", "Contract"):
        next(g for g in at.get("button_group") if g.key == "dsv_pnl").set_value(pick).run()
        assert not at.exception, (pick, [e.value for e in at.exception])
    # a metric it feeds opens as its own sheet
    next(b for b in at.button if b.key == "dsf_pnl_operating_margin").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["data_open"] == {"kind": "metric", "id": "operating_margin"}
    # closed (×, Escape, a click outside): nothing open, the flow as it was
    at.session_state["data_open"] = None
    at.session_state["data_pick"] = None
    at.run()
    assert not _dialogs(at) and objects_of(at)["selected"] is None


def test_a_metric_opens_with_its_value_formula_and_what_it_reads(ui):  # noqa: F811
    at = _click(_open_data(), "metric", "operating_margin")
    assert len(_dialogs(at)) == 1
    assert any("operating_income" in str(c.value) for c in at.code)  # how it is calculated
    keys = {b.key for b in at.button}
    assert {"msh_ask_operating_margin", "msh_brain_operating_margin", "msh_operating_margin_pnl_open"} <= keys
    at.button(key="msh_operating_margin_pnl_open").click().run()  # the dataset it reads, as a sheet
    assert at.session_state["data_open"] == {"kind": "dataset", "id": "pnl"}


def test_data_opens_as_a_flow_from_sources_to_what_the_data_feeds(ui):  # noqa: F811
    o = objects_of(_open_data())
    assert all(d["from"] for d in o["datasets"])  # every dataset is wired to where it came from
    ids = {s["id"] for s in o["sources"]}
    assert all(d["from"] in ids for d in o["datasets"])
    uses = {u["id"]: u for u in o["uses"]}  # and to the approved metrics it feeds
    assert "operating_margin" in uses and "pnl" in uses["operating_margin"]["from"]
    assert next(d for d in o["datasets"] if d["name"] == "pnl")["feeds"] >= 1


def test_a_source_opens_as_a_sheet_and_search_narrows(ui):  # noqa: F811
    at = _click(_open_data(), "source", "files")
    assert len(_dialogs(at)) == 1 and objects_of(at)["source"] == "files"  # its path lit underneath
    keys = {b.key for b in at.button}
    assert {"ssh_files_pnl_open", "ssh_files_pnl_ledger_open", "ssh_upload"} <= keys  # what it gives, and a way to add
    at.button(key="ssh_files_pnl_ledger_open").click().run()  # one of its datasets opens as its own sheet
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["data_open"] == {"kind": "dataset", "id": "pnl_ledger"}
    at.session_state["data_open"] = None
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


def quality_of(at) -> list[dict]:
    """Every data quality review drawn on the page, as Python sent it."""
    found = []

    def walk(node):
        for ch in (getattr(node, "children", None) or {}).values():
            if getattr(ch, "type", None) == "bidi_component" and ch.proto.component_name.endswith("lyst_quality"):
                found.append(json.loads(ch.proto.json))
            walk(ch)

    walk(at._tree)
    return found


def test_the_ledgers_flagged_rows_open_as_a_review_table_from_the_one_line(ui):  # noqa: F811
    at = _open_data()
    at.session_state["_data_nonce"] = None
    # Lyst's line about the data leads to the ledger's quality, which is the review table, not a list of words
    next(b for b in at.button if b.key == "read_pnl_ledger_open").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(_dialogs(at)) == 1  # the review opens in the ledger's sheet, over the page
    [q] = quality_of(at)
    assert q["dataset"] == "pnl_ledger" and q["open"] == 3 and q["canEdit"] and q["canApprove"]
    assert {i["kind"] for i in q["issues"]} == {"duplicate", "null", "negative"}
    assert all(i["proposal"]["reason"] for i in q["issues"]) and q["fixable"] == 3
    assert "3 of 3 flagged rows need review" in q["say"]
    # the table-level risks the rows already show are not listed again underneath
    assert "negative value in cogs" not in _texts(at)


def test_review_data_quality_is_a_link_from_anywhere(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.query_params["dataset"] = "pnl_ledger"
    at.query_params["review"] = "quality"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert [q["dataset"] for q in quality_of(at)] == ["pnl_ledger"] and len(_dialogs(at)) == 1


def test_a_quality_answer_opens_the_review_in_place(ui, seeded):  # noqa: F811
    from .conftest import ask  # noqa: PLC0415

    lyst, owner = seeded
    t = ask(lyst, owner, "Profile pnl_ledger for data quality issues")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.session_state["task_id"] = t["id"]
    at.query_params["task"] = t["id"]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    qs = quality_of(at)
    assert qs and qs[0]["dataset"] == "pnl_ledger" and qs[0]["compact"]
    assert "Check not passed: duplicates" not in _texts(at)  # the table says it, row by row
