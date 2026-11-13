# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A segment the data does not have ("revenue in Atlantis") stops the question and lists the members that exist,
instead of answering for the whole metric as if it were the segment's figure."""

from __future__ import annotations

import pytest

from .conftest import ask


def test_an_unknown_region_is_said_not_answered_for_the_whole_metric(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Revenue in Atlantis last quarter")["result"]
    assert r["status"] == "needs_input"
    assert r["answer"].startswith("Atlantis is not a region or product line in the data behind revenue, so Lyst did not "
                                  "answer for the whole metric instead.")
    assert "The regions are APAC, EMEA" in r["answer"] and "Ask again naming one." in r["answer"]
    assert "$25.41M" not in r["answer"]


def test_a_watch_on_an_unknown_segment_requests_no_monitor(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Alert me when revenue in Europe moves")["result"]
    assert r["status"] == "needs_input" and r["answer"].startswith("Europe is not a region")
    assert not lyst.monitors.list(owner, "northwind")


@pytest.mark.parametrize("text, figure", [("Revenue in EMEA last month", "$2.43M"), ("Revenue in Q3 2026", "$25.41M"),
                                          ("Revenue in September 2026", "$8.23M"), ("Revenue for FY2025", "$85.74M"),
                                          ("What was Revenue for Hardware?", "$11.53M"),
                                          ("Revenue in USD for 2025", "$85.74M"),
                                          ("Operating margin in North America", "35.9%"),
                                          ("Revenue by region last quarter in Excel", "$25.41M")])
def test_periods_currencies_and_real_members_still_answer(seeded, text, figure):
    lyst, owner = seeded
    r = ask(lyst, owner, text)["result"]
    assert r["status"] == "complete" and figure in r["answer"], r["answer"]
