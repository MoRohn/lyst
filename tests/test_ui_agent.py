# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The agentic layer as people see it: Home leads with what Lyst is doing and the answers themselves (details fold
open, Open shows the full artifact), Data leads with Lyst's read on the data, and Brain opens on one line of what Lyst
knows and does, the field of what it knows, and Work as its other section."""

import base64
import json
import re

from streamlit.testing.v1 import AppTest

from lyst.ui import agent, tokens

from .test_ui_pages import _texts, ui  # noqa: F401  (fixture)


def _bar(at) -> dict:
    """The agent bar's data, as Python sent it to the page."""
    found = []

    def walk(node):
        for ch in (getattr(node, "children", None) or {}).values():
            if getattr(ch, "type", None) == "bidi_component" and ch.proto.component_name.endswith("lyst_agent_bar"):
                found.append(json.loads(ch.proto.json))
            walk(ch)

    walk(at._tree)
    assert len(found) == 1, f"{len(found)} agent bars"
    return found[0]


def _state(*tasks: dict, monitors=(), schedules=()) -> dict:
    return {"tasks": list(tasks), "monitors": list(monitors), "schedules": list(schedules)}


def test_the_status_line_says_what_lyst_is_doing_in_words():
    tone, head, parts = agent.summary(_state({"status": "running"}, {"status": "done", "created_at": None}), 2)
    assert tone == "run" and head == "Working on 1 request" and "2 need you" in parts
    tone, head, parts = agent.summary(_state(monitors=[{"enabled": True}], schedules=[{"enabled": False}]), 1)
    assert tone == "wait" and head == "1 thing needs you" and "watching 1 metric" in parts
    tone, head, parts = agent.summary(_state(), 0)
    assert tone == "ok" and head == "Up to date" and "nothing runs on its own yet" in parts
    for _t, h, p in (agent.summary(_state({"status": "queued"}), 0), agent.summary(_state(), 3)):
        assert not re.search(r"[_{}]", h + " ".join(p))  # no codes or template leftovers


def test_the_knowledge_drawing_is_an_image_from_real_counts_without_alarm_colours():
    for mode in ("light", "dark"):
        src = agent.constellation([("approved metrics", 6), ("datasets", 4), ("documents", 0), ("memories", 30)], mode)
        assert src.startswith("data:image/svg+xml;base64,")  # st.html strips inline <svg>
        svg = base64.b64decode(src.split(",", 1)[1]).decode()
        assert svg.count("<circle") == 2 * (6 + 4 + 24) + 3  # one point per record, 24 at most per kind, and the core
        assert tokens.colors(mode)["bad"] not in svg and tokens.series(mode)[4] not in svg


def open_recent(at, n: int = -1) -> None:
    """Open an answer the way a person does on Home: the dock's Recent."""
    recent = [r for r in _bar(at)["hero"]["recent"] if r.get("task")]
    assert recent
    at.session_state["_bar_do"] = {"type": "open", "task": recent[n]["task"], "nonce": f"open-{recent[n]['task']}"}
    at.run()


def test_home_is_one_composer_with_the_rest_one_gesture_away(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    bar = _bar(at)
    assert bar["need"] and "need" in bar["head"] + " ".join(bar["parts"])  # the agent bar: something waits on you
    hero = bar["hero"]
    assert hero["title"] and hero["focus"]["kicker"] == "Needs you"  # the one thing, not a board
    assert not any((b.key or "").startswith("ans_open_") for b in at.button)  # no answer cards on Home itself
    assert any(r.get("task") for r in hero["recent"])  # answers wait in the dock's Recent
    open_recent(at)
    assert not at.exception, [e.value for e in at.exception]
    assert _bar(at)["hero"] is None  # an open answer: the bar is a bar again
    assert any(e.label.startswith("Why") for e in at.expander)  # details folded under the answer


def test_data_opens_with_lyst_reading_its_data(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    hit = next(b for b in at.button if b.key == "read_pnl_ledger_open")  # Lyst's one line: the ledger copy needs a look
    assert "to fix" in hit.label and "need" in (hit.help or "") and "attention" in (hit.help or "")
    hit.click().run()  # one click to the dataset's full record
    assert not at.exception, [e.value for e in at.exception]
    assert any(g.key == "dsv_pnl_ledger" for g in at.get("button_group"))


def test_brain_opens_calm_with_search_and_work_as_a_section(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(str(h.value) for h in at.get("html"))
    assert 'class="lyst-hero"' not in html and "Knows" in html  # one line, no decorative band
    assert any(t.key == "brain_q" for t in at.text_input)  # Overview leads with search
    nav = next(g for g in at.get("button_group") if g.key == "sec_view")
    assert [str(o.content).split()[-1] for o in nav.proto.options][:2] == ["Overview", "Work"] or \
        "Overview" in str(nav.proto.options[0])
    at.text_input(key="brain_q").input("margin").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Operating margin" in b.label for b in at.button)  # what matches, one step from the field
    nav.set_value("Work").run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Waiting on you" in _texts(at)  # the fixture's ambiguous question waits on the person  # the fixture's ambiguous question waits on the person
