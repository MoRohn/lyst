# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Brain, as a place: what Lyst knows drawn as one field gathered by collection, a thing's relationships shown only
once it is picked (with what it is, what it connects to and two moves beside it), and the full lists, the graph and
the specializations one menu away."""

from __future__ import annotations

import json
import re
from pathlib import Path

from streamlit.testing.v1 import AppTest

from .test_ui_pages import _texts, ui  # noqa: F401  (fixture)

HERE = Path(__file__).resolve().parents[1] / "src" / "lyst" / "ui" / "brain_component"


def field_of(at) -> dict:
    """Brain's field, as Python sent it to the page."""
    found = []

    def walk(node):
        for ch in (getattr(node, "children", None) or {}).values():
            if getattr(ch, "type", None) == "bidi_component" and ch.proto.component_name.endswith("lyst_knowledge"):
                found.append(json.loads(ch.proto.json))
            walk(ch)

    walk(at._tree)
    assert len(found) == 1, f"{len(found)} fields"
    return found[0]


def _open_brain(**state) -> AppTest:
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_brain_opens_on_the_field_with_nothing_wired_up(ui):  # noqa: F811
    at = _open_brain()
    f = field_of(at)
    cols = {c["id"]: c["count"] for c in f["collections"]}
    assert cols["all"] == len(f["nodes"]) and cols["metric"] >= 1 and cols["dataset"] >= 1
    assert any(n["id"] == "metric:operating_margin" for n in f["nodes"])
    assert f["selected"] is None and f["collection"] == "all"  # relationships appear only once something is picked
    assert len({(lk["source"], lk["target"], lk["rel"]) for lk in f["links"]}) == len(f["links"])  # each relation once
    assert not any(b.key == "brain_close" for b in at.button)
    assert "Lately" in _texts(at)


def test_picking_a_thing_shows_what_it_is_and_what_it_connects_to(ui):  # noqa: F811
    at = _open_brain(brain_sel="metric:operating_margin")
    assert field_of(at)["selected"] == "metric:operating_margin"
    assert "Operating margin" in _texts(at)
    rel = [b for b in at.button if (b.key or "").startswith("brain_rel_")]
    hop = next(b for b in rel if b.label == "Reads Monthly P&L")
    assert any(b.key == "brain_ask" for b in at.button) and any(b.key == "brain_open" for b in at.button)
    hop.click().run()  # one step along a relationship
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["brain_sel"] == "dataset:pnl" and field_of(at)["selected"] == "dataset:pnl"
    at.button(key="brain_close").click().run()
    assert not at.exception and field_of(at)["selected"] is None


def test_a_search_lights_up_what_matches(ui):  # noqa: F811
    at = _open_brain()
    at.text_input(key="brain_q").input("margin").run()
    assert not at.exception, [e.value for e in at.exception]
    assert "metric:operating_margin" in field_of(at)["hits"]


def test_one_collection_at_a_time(ui):  # noqa: F811
    at = _open_brain(brain_coll="dataset")
    assert field_of(at)["collection"] == "dataset"
    at.session_state["brain_coll"] = "nothing-here"
    at.run()
    assert field_of(at)["collection"] == "all"  # a collection that no longer exists falls back to everything


def test_the_full_lists_are_one_menu_away_and_back(ui):  # noqa: F811
    at = _open_brain()
    at.button(key="brain_go_definitions").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.query_params.get("view") in ("definitions", ["definitions"])
    assert not any(g.key == "sec_view" for g in at.get("button_group"))  # a deeper place: a way back, not the tabs
    at.button(key="brain_back").click().run()
    assert not at.exception, [e.value for e in at.exception]
    field_of(at)


def test_the_field_uses_only_tokens_and_no_network():
    css = (HERE / "field.css").read_text()
    js = (HERE / "field.js").read_text()
    body = re.sub(r"var\(--lyst-[a-z0-9-]+,\s*[^()]*(?:\([^()]*\)[^()]*)*\)", "", css)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", body) and not re.findall(r"rgba?\(", body)
    assert "prefers-reduced-motion" in css and "forced-colors" in css
    assert "fetch(" not in js and "http" not in js
