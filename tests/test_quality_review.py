# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workspace data quality review: "Are there any data quality issues?" with no dataset named checks every dataset the
asker may read, worst first, instead of asking which one. A named dataset still gets its own profile."""

from __future__ import annotations

import pytest

from .conftest import ask


def _review(lyst, owner, name):
    return lyst.runtime.quality.report(owner, "northwind", name)


@pytest.mark.parametrize("question", ["Are there any data quality issues?", "Is our data clean?",
                                      "Any duplicates in the data?"])
def test_an_unnamed_quality_question_checks_every_dataset(seeded, question):
    lyst, owner = seeded
    r = ask(lyst, owner, question)["result"]
    assert r["status"] == "complete" and r["mode"] == "quality_review"
    names = {d["name"] for d in lyst.data.catalog(owner, "northwind")}
    table = r["tables"][0]
    assert table["title"] == "Data quality by dataset" and {row[0] for row in table["rows"]} == names
    # the figures are the quality review's own: the demo's planted ledger issues lead
    ledger = _review(lyst, owner, "pnl_ledger")
    assert ledger["open"] > 0
    assert r["answer"].startswith(f"Lyst checked {len(names)} datasets and found {ledger['open']} open data quality issues")
    assert "The most serious are in pnl_ledger: " in r["answer"] and "duplicate row" in r["answer"]
    assert r["open"] == {"kind": "quality", "id": "pnl_ledger", "title": "Flagged rows in pnl_ledger"}
    assert any(a.startswith(f"Review the {ledger['open']} open issues in pnl_ledger") and "approved" in a for a in r["actions"])
    assert any(e.startswith("pnl: nothing open in ") for e in r["evidence"])
    assert r["confidence"]["level"] == "verified"


def test_an_issue_accepted_as_valid_is_no_longer_open(seeded):
    lyst, owner = seeded
    ledger = _review(lyst, owner, "pnl_ledger")
    first = next(i for i in ledger["issues"] if i["status"] == "open")
    lyst.runtime.quality.review(owner, "northwind", "pnl_ledger", [first["id"]], "accept", note="checked with finance")
    r = ask(lyst, owner, "Are there any data quality issues?")["result"]
    row = next(x for x in r["tables"][0]["rows"] if x[0] == "pnl_ledger")
    assert row[2] == ledger["open"] - 1
    assert any(e.startswith("pnl_ledger: ") and "1 accepted as valid" in e for e in r["evidence"])


def test_a_named_dataset_still_gets_its_own_profile(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Are there any data quality issues in pnl?")["result"]
    assert r.get("mode") != "quality_review" and r["answer"].startswith("pnl has ")


def test_a_reader_sees_only_the_datasets_they_may_read(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    r = ask(lyst, viewer, "Are there any data quality issues?")["result"]
    seen = {d["name"] for d in lyst.data.catalog(viewer, "northwind")}
    assert {row[0] for row in r["tables"][0]["rows"]} == seen
