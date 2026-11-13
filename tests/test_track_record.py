# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Forecast track record: "How accurate were your last forecasts?" compares the forecasts Lyst already gave with what
happened, for every forecast month the data has since filled. A forecast whose months have not arrived says when it can
be checked; a forecast for a filtered slice is left out."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from lyst import demo
from lyst.analyst import track_record

from .conftest import ask

Q = "How accurate were your last forecasts?"


def _as_if_made_in_june(lyst, task):
    """The demo's data ends in Sep 2026, so a forecast made today covers months not in it yet. Moving the forecast's
    months to Jul-Sep 2026 (and its history to June) stands in for a forecast made three months ago."""
    res = task["result"]
    chart = next(c for c in res["charts"] if c["type"] == "forecast")
    chart["x"] = ["2026-07-01", "2026-08-01", "2026-09-01"]
    chart["y"], chart["lo"], chart["hi"] = chart["y"][:3], chart["lo"][:3], chart["hi"][:3]
    chart["history_x"] = [h for h in chart["history_x"] if str(h)[:7] <= "2026-06"]
    lyst.runtime.db.execute("UPDATE tasks SET result = ? WHERE id = ?", [json.dumps(res), task["id"]])
    return chart


def test_with_no_forecast_yet_it_says_so(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, Q)["result"]
    assert r["mode"] == "track_record" and r["answer"].startswith("Lyst has not given a forecast in this workspace yet")


def test_a_forecast_whose_months_have_not_arrived_says_when_it_can_be_checked(seeded):
    lyst, owner = seeded
    ask(lyst, owner, "Forecast revenue for next quarter")
    r = ask(lyst, owner, Q)["result"]
    assert r["answer"].startswith("The one forecast Lyst has given here cannot be checked yet: the first forecast month "
                                  "is Oct 2026 and the data runs to Sep 2026.")
    assert "Its own backtest put the error at " in r["answer"]
    assert r["actions"][0].startswith("Ask again once Oct 2026 is loaded")


def test_forecasts_are_compared_with_what_happened(seeded):
    lyst, owner = seeded
    chart = _as_if_made_in_june(lyst, ask(lyst, owner, "Forecast revenue for next quarter"))
    r = ask(lyst, owner, Q)["result"]
    df = demo.pnl()
    actual = df.groupby(pd.to_datetime(df["month"]).dt.strftime("%Y-%m"))["revenue"].sum()
    errs = [abs(y - actual[m]) / actual[m] for y, m in zip(chart["y"], ["2026-07", "2026-08", "2026-09"], strict=True)]
    inside = sum(lo <= actual[m] <= hi for lo, hi, m in zip(chart["lo"], chart["hi"], ["2026-07", "2026-08", "2026-09"],
                                                           strict=True))
    assert r["answer"].startswith(f"Lyst checked 1 forecast against 3 months that have since arrived: it was off by "
                                  f"{sum(errs) / 3:.1%} on average, and the actual landed inside the forecast's interval "
                                  f"{inside} of 3 times")
    table = r["tables"][0]
    assert table["title"] == "Forecasts against what happened" and len(table["rows"]) == 3
    assert [row[6] for row in table["rows"]] == pytest.approx([actual[m] for m in ["2026-07", "2026-08", "2026-09"]])
    worst = ["Jul 2026", "Aug 2026", "Sep 2026"][max(range(3), key=lambda i: errs[i])]
    assert any(e.startswith(f"Biggest miss: Revenue for {worst}") for e in r["evidence"])
    assert r["supporting"]["queries"] and r["confidence"]["level"] == "verified"


def test_a_forecast_for_a_slice_is_left_out(seeded):
    lyst, owner = seeded
    _as_if_made_in_june(lyst, ask(lyst, owner, "Forecast revenue in EMEA for next quarter"))
    r = ask(lyst, owner, Q)["result"]
    assert not r["tables"] and "Lyst has not given" not in r["answer"]


@pytest.mark.parametrize("text", [Q, "What is your forecast accuracy?", "How good are Lyst's forecasts?",
                                  "Did the revenue forecasts hold up?", "Show the track record of your forecasts"])
def test_track_record_phrases(text):
    assert track_record.asks_for_track_record(text)


@pytest.mark.parametrize("text", ["Forecast revenue for next quarter", "How accurate is the pnl data?",
                                  "What was revenue last quarter?",
                                  "Evaluate the rig tests: how well do simulation predictions match measured results?",
                                  "How accurate are the simulation predictions by load case?"])
def test_not_track_record_phrases(text):
    assert not track_record.asks_for_track_record(text)
