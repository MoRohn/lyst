# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Datasheets and annotation guideline drafts: "Document the annotations dataset" writes down what Lyst has measured
about a dataset (overview, data dictionary, quality, lineage and use, labels), and "Write annotation guidelines" drafts
the guideline from how the labels have been used, leaving the definitions to the team."""

from __future__ import annotations

import pytest

from lyst.analyst import datasheet

from .conftest import ask
from .test_annotations import WS, _expected
from .test_annotations import _open as open_labs


def test_a_labeled_dataset_is_documented_from_measurements(lyst):
    owner = open_labs(lyst)
    r = ask(lyst, owner, "Document the annotations dataset", ws=WS)["result"]
    assert r["status"] == "complete" and r["mode"] == "datasheet"
    assert r["answer"].startswith("Datasheet for Sentiment annotations (annotations): 905 rows, 5 columns")
    titles = [t["title"] for t in r["tables"]]
    assert titles[0] == "Data dictionary, annotations" and "Labels" in titles and "Draft annotation guideline" in titles
    dictionary = {row[0]: row for row in r["tables"][0]["rows"]}
    assert set(dictionary) == {"item_id", "text", "annotator", "label", "confidence"}
    assert dictionary["annotator"][4] == 4
    # the quality review's open issues are part of the record: the exported duplicates, label spellings, blank labels
    issues = dict(next(t for t in r["tables"] if t["title"] == "Open data quality issues")["rows"])
    df, _ = _expected()
    assert issues["duplicate"] == 5 and issues["variant"] > 0 and issues["null"] == int(df["norm"].isna().sum())
    assert any(f.startswith("What it is: Sentiment labels from four annotators") for f in r["evidence"])
    assert any("data contract" in a for a in r["actions"])


def test_annotation_guidelines_are_drafted_from_the_data_with_definitions_left_to_the_team(lyst):
    owner = open_labs(lyst)
    r = ask(lyst, owner, "Write annotation guidelines for this dataset", ws=WS)["result"]
    assert r["answer"].startswith("Draft annotation guideline for Sentiment annotations")
    guide = r["tables"][0]
    assert guide["title"] == "Draft annotation guideline"
    rules = [row[1] for row in guide["rows"]]
    assert rules[0].startswith("Draft from the data. Definitions are for the team to write")
    assert sum(rule.startswith("Label \"") and "Definition: to be written by the team" in rule for rule in rules) == 3
    assert any("Examples every annotator agreed on: T" in rule for rule in rules)
    edge = next(rule for rule in rules if rule.startswith("Edge case"))
    assert "settle it on T" in edge
    assert any(rule.startswith("Use exactly these spellings") for rule in rules)
    assert any(rule.startswith("Never leave label empty") for rule in rules)
    assert any("write each label's definition" in a for a in r["actions"])


def test_an_unlabeled_dataset_gets_a_data_dictionary_and_its_metrics(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Write a data dictionary for pnl")["result"]
    assert r["mode"] == "datasheet" and r["answer"].startswith("Datasheet for ")
    assert "(pnl)" in r["answer"]
    assert "Labels" not in [t["title"] for t in r["tables"]]
    assert any(f.startswith("Approved metrics built on it: ") and "Revenue" in f for f in r["evidence"])


def test_an_unnamed_dataset_among_several_is_asked_for(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Document the dataset")["result"]
    assert r["status"] == "needs_input" and r["answer"].startswith("Which dataset should Lyst document?")
    assert "pnl" in r["answer"]


def test_a_column_the_reader_may_not_see_is_left_out(lyst):
    owner = open_labs(lyst)
    viewer = lyst.guard.create_user("v@labs.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    lyst.data.set_column_policy(owner, WS, "annotations", "text", "confidential", "admin")
    r = ask(lyst, viewer, "Document the annotations dataset", ws=WS)["result"]
    assert "text" not in {row[0] for row in r["tables"][0]["rows"]}
    assert any("1 column is not documented" in f for f in r["evidence"])


@pytest.mark.parametrize("text", ["Document the annotations dataset", "Write a data dictionary for pnl",
                                  "Draft annotation guidelines", "Create a datasheet", "Write the labeling instructions"])
def test_datasheet_phrases(text):
    assert datasheet.asks_for_datasheet(text)


@pytest.mark.parametrize("text", ["How consistent are the annotators?", "What was revenue last quarter?"])
def test_not_datasheet_phrases(text):
    assert not datasheet.asks_for_datasheet(text)
