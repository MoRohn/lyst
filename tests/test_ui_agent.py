# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The agentic layer as people see it: Home leads with what Lyst is doing and the answers themselves (details fold
open, Open shows the full artifact), Data leads with Lyst's read on the data, and Brain opens on a band of what Lyst
knows, does and runs on its own, with Work as one of its sections."""

import base64
import re

from streamlit.testing.v1 import AppTest

from lyst.ui import agent, tokens

from .test_ui_pages import _texts, ui  # noqa: F401  (fixture)


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


def test_home_leads_with_status_and_answer_cards_whose_details_fold_open(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(str(h.value) for h in at.get("html"))
    assert 'class="lyst-pulse"' in html and "need" in html  # the live line: something waits on the person
    assert any((b.key or "").startswith("ans_open_") for b in at.button)  # answers, each opening its full artifact
    assert any(e.label == "Why, and what to do" for e in at.expander)  # details folded under the answer
    assert "Latest answers" in _texts(at)


def test_data_opens_with_lyst_reading_its_data(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/data.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(str(h.value) for h in at.get("html"))
    assert "need" in html and "attention" in html and "Ready" not in html  # the ledger copy has a high-risk finding
    hit = next(b for b in at.button if b.key == "read_pnl_ledger_open")
    hit.click().run()  # one click to the dataset's full record
    assert not at.exception, [e.value for e in at.exception]
    assert any(g.key == "dsv_pnl_ledger" for g in at.get("button_group"))


def test_brain_opens_on_its_band_with_work_as_a_section(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/brain.py")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(str(h.value) for h in at.get("html"))
    assert 'class="lyst-hero"' in html and "Knows" in html and "On its own" in html
    assert 'alt="What Lyst knows:' in html  # the drawing has a text equivalent
    nav = next(g for g in at.get("button_group") if g.key == "sec_view")
    assert "Work" in str(nav.proto.options[0])  # Work is Brain's first section
    nav.set_value("Work").run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Waiting on you" in _texts(at)  # the fixture's ambiguous question waits on the person
