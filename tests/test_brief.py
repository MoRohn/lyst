# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The business brief: a request about the business as a whole reads every approved metric for one period, flags what
moved more than that metric usually moves, names the segment behind it, and can be repeated on a schedule."""

from __future__ import annotations

import pandas as pd
import pytest

from lyst import demo
from lyst.analyst import brief
from lyst.followups import FollowUps

from .conftest import ask


def _understanding(lyst, task):
    import json

    row = lyst.runtime.db.query("SELECT output FROM task_steps WHERE task_id = ? AND capability = 'understand'", [task["id"]])
    return json.loads(row[0]["output"])


def test_a_kpi_brief_reads_every_metric_and_flags_the_planted_margin_drop(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Give me a weekly KPI brief")
    r = t["result"]
    assert t["status"] == "done" and r["status"] == "complete" and r["mode"] == "brief"
    assert _understanding(lyst, t)["intent"] == "brief"
    # the last complete month, every approved metric in the table, each with the prior month and a year earlier
    assert r["answer"].startswith("Sep 2026: ")
    table = r["tables"][0]
    assert table["columns"][:4] == ["metric", "Sep 2026", "vs Aug 2026", "vs Sep 2025"]
    titles = {row[0] for row in table["rows"]}
    assert titles == {m.title for m in lyst.semantic.metrics(owner, "northwind")}
    # the planted EMEA cost jump from Jul 2026 holds flat month to month, so it is caught against a year earlier
    sup = r["supporting"]["brief"]
    assert "operating_margin" in sup["unusual"] and "cogs_ratio" in sup["unusual"]
    # the headline tells one move in full and names the others; each has its own line under Evidence
    assert r["answer"].count(". ") <= 4 and "Also unusual: " in r["answer"] and "operating margin" in r["answer"]
    assert any(e.startswith("Operating margin: 32.4% in Sep 2026") for e in r["evidence"])
    # the move with one place to act on leads (the EMEA cost jump), not operating income, whose segments offset
    lead = r["answer"].split(". ")[1]
    assert lead.startswith(("COGS ratio is ", "Operating margin is ")) and "mostly EMEA (region" in lead
    assert "operating income" in r["answer"].split("Also unusual: ")[1]
    # the figures are the demo's own
    df = demo.pnl()
    sep = df[pd.to_datetime(df["month"]) == "2026-09-01"]
    margin = sep.operating_income.sum() / sep.revenue.sum()
    assert f"{margin * 100:.1f}%" in next(e for e in r["evidence"] if e.startswith("Operating margin:"))
    # metrics that moved normally are still listed, as normal
    assert any(e.startswith("Revenue:") and "within its normal range" in e for e in r["evidence"])
    # every figure comes from a recorded query, and the checks say how current each dataset is
    assert r["supporting"]["queries"] and all(q["sql"] for q in r["supporting"]["queries"])
    names = {c["name"] for c in r["confidence"]["checks"]}
    assert "numbers_backed" in names and "coverage:revenue" in names
    assert r["confidence"]["level"] == "verified"
    assert any(a.startswith("Ask why operating margin") or a.startswith("Ask why COGS ratio") for a in r["actions"])
    assert r["charts"] and r["charts"][0]["type"] == "bar"


def test_a_board_briefing_on_a_quarter_compares_quarters_for_an_executive(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Give me a board briefing on Q3 2026")["result"]
    assert r["audience"] == "executive" and r["answer"].startswith("Q3 2026: ")
    assert r["tables"][0]["columns"][2:4] == ["vs Q2 2026", "vs Q3 2025"]
    # quarter on quarter the margin fell sharply, concentrated in EMEA
    assert "Operating margin fell 3.5 pts to 32.5% (vs Q2 2026), mostly EMEA" in r["answer"]
    # an executive brief carries the implication with the headline
    assert r["brief"] != r["answer"] and r["brief"].startswith(r["answer"])


def test_an_incomplete_period_falls_back_to_the_last_complete_one(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Brief me on Oct 2026")["result"]
    assert r["answer"].startswith("Sep 2026: ")
    assert "Oct 2026 is not complete in the data yet" in r["supporting"]["assumptions"][0]


def test_a_brief_on_a_named_metric_covers_only_that_metric(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Brief me on revenue")
    u, r = _understanding(lyst, t), t["result"]
    assert u["intent"] == "brief" and u["metrics"] == ["revenue"]
    assert [row[0] for row in r["tables"][0]["rows"]] == ["Revenue"]
    assert r["answer"].startswith("Revenue in Sep 2026: $8.23M, -3.4% vs Aug 2026 and +11.5% vs Sep 2025")
    assert "of the change)" in r["answer"] or "spread across segments" in r["answer"]  # where the move came from
    # "what changed in revenue" and "why ... prepare a briefing" are still the engine's own explanation, not a brief
    t = ask(lyst, owner, "Why did operating margin decline last quarter? Prepare a briefing for the board.")
    assert _understanding(lyst, t)["intent"] == "explain"
    assert _understanding(lyst, ask(lyst, owner, "What changed in revenue last quarter?"))["intent"] != "brief"


def test_an_ambiguous_term_is_not_a_brief(seeded):
    lyst, owner = seeded
    # an ambiguous term still blocks with its question instead of turning into a brief of everything
    t = ask(lyst, owner, "What changed in margin?")
    assert _understanding(lyst, t)["intent"] != "brief"
    assert "more than one approved metric" in t["result"]["answer"]


def test_the_brief_leaves_out_what_the_reader_may_not_see(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    lyst.data.set_column_policy(owner, "northwind", "pnl", "cogs", "confidential", "admin")
    r = ask(lyst, viewer, "What changed this month?")["result"]
    assert r["status"] == "complete"
    titles = {row[0] for row in r["tables"][0]["rows"]}
    assert "COGS ratio" not in titles and "Revenue" in titles
    scope = next(c for c in r["confidence"]["checks"] if c["name"] == "access_scope")
    assert "COGS ratio" in scope["detail"]


def test_a_brief_can_be_repeated_or_moved_but_not_drilled_into(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "What changed this month?")
    fu = FollowUps(lyst.runtime, lyst.semantic)
    res = fu.apply(owner, t["id"], "every Monday at 8")
    assert res["action"] == "schedule" and res["question"] == "What changed this month?" and res["enabled"] is False
    assert res["approval_id"]  # a recurring run waits for approval like any other schedule
    res = fu.apply(owner, t["id"], "what about last quarter?")
    assert res["action"] == "task" and res["question"] == "Brief me on last quarter"
    lyst.runtime.drain()
    assert lyst.runtime.get(owner, res["task_id"])["result"]["answer"].startswith("Q3 2026: ")
    res = fu.apply(owner, t["id"], "why?")
    assert res["action"] == "unsupported" and "covers every metric" in res["message"]


@pytest.mark.parametrize("text", ["Give me a weekly KPI brief", "What changed this month?", "How are we doing?",
                                  "Monthly business review", "Anything unusual I should know?", "QBR please"])
def test_brief_phrases(text):
    assert brief.asks_for_brief(text)


@pytest.mark.parametrize("text", ["What was revenue last quarter?", "Top 3 regions by operating income",
                                  "Forecast revenue for the next two quarters",
                                  "Brief me on what our documents say about the Data Act regulation",
                                  "Summarise the news briefing on our competitors"])
def test_not_brief_phrases(text):
    assert not brief.asks_for_brief(text)


def test_robust_z_and_streak():
    assert brief.robust_z([1, 2, 3], 10) is None  # too little history to judge
    hist = [0.01, -0.01, 0.02, -0.02, 0.0, 0.01, -0.01]
    assert abs(brief.robust_z(hist, 0.0)) < 0.5
    assert brief.robust_z(hist, 0.2) > brief.UNUSUAL_Z
    assert brief.robust_z([0.0] * 8, 0.0) == 0.0
    assert brief.streak([1, -1, -2, -3]) == 3 and brief.streak([1, 2, 0]) == 0 and brief.streak([]) == 0
