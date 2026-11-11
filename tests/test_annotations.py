# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Label audit: "How consistent are the annotators?" reads a labeled dataset and reports agreement (Krippendorff's
alpha), the label pairs annotators confuse, each annotator's agreement and bias, inconsistent spellings, missing labels
and the items to adjudicate. Lyst never relabels or merges anything itself."""

from __future__ import annotations

import io
import json

import numpy as np
import pandas as pd
import pytest

from lyst.analyst import annotations as A

from .conftest import ask

WS = "labs"


def labeled() -> pd.DataFrame:
    """300 reviews, three of four annotators each; one in five is hard; a4 over-uses "negative"; a few labels carry
    stray capitals or spaces, a few are empty, and five judgments are exported twice."""
    rng = np.random.default_rng(7)
    labels, people, rows = ["positive", "negative", "neutral"], ["a1", "a2", "a3", "a4"], []
    for i in range(300):
        truth, hard = labels[int(rng.integers(0, 3))], rng.random() < 0.2
        for who in rng.choice(people, 3, replace=False):
            lab = truth if rng.random() > (0.45 if hard else 0.08) else labels[int(rng.integers(0, 3))]
            if who == "a4" and rng.random() < 0.3:
                lab = "negative"
            if rng.random() < 0.03:
                lab = lab.capitalize() + " "
            if rng.random() < 0.02:
                lab = None
            rows.append({"item_id": f"T{i:04d}", "text": f"review text {i}", "annotator": str(who), "label": lab,
                         "confidence": round(float(rng.uniform(0.4, 1)), 2)})
    return pd.DataFrame(rows + rows[:5])


def _open(lyst):
    owner = lyst.guard.create_user("ana@labs.test", "Ana")
    lyst.guard.create_workspace(owner, WS, "Labeling team")
    buf = io.StringIO()
    labeled().to_csv(buf, index=False)
    lyst.data.ingest(owner, WS, "annotations", buf.getvalue().encode(), filename="annotations.csv",
                     title="Sentiment annotations", description="Sentiment labels from four annotators, three per item")
    return owner


def _expected():
    df = labeled()
    df["norm"] = df["label"].map(A.normalize)
    judged = df[df["norm"].notna()].drop_duplicates(["item_id", "annotator"], keep="last")
    per_item = judged.groupby("item_id")["norm"].agg(list)
    multi = per_item[per_item.map(len) >= 2]
    return df, multi


def test_annotator_consistency_is_measured_and_the_odd_annotator_named(lyst):
    owner = _open(lyst)
    t = ask(lyst, owner, "How consistent are the annotators?", ws=WS)
    r = t["result"]
    u = json.loads(lyst.runtime.db.query("SELECT output FROM task_steps WHERE task_id = ? AND capability = 'understand'",
                                         [t["id"]])[0]["output"])
    assert u["intent"] == "annotations" and u["dataset"] == "annotations"
    assert r["status"] == "complete" and r["mode"] == "annotations"
    df, multi = _expected()
    alpha = A.krippendorff_alpha(multi.tolist())
    split = int((multi.map(lambda xs: len(set(xs))) > 1).sum())
    assert f"agreement (Krippendorff's alpha) is {alpha:.2f}" in r["answer"]
    assert f"{split:,} items need adjudication" in r["answer"]
    assert "a4 labels differently from the team" in r["answer"]
    assert f"{int(df['norm'].isna().sum()):,} judgments have no label" in r["answer"]
    people = {row[0]: row for row in next(tb for tb in r["tables"] if tb["title"] == "Annotators")["rows"]}
    assert people["a4"][3] == "negative" and people["a4"][4] > people["a4"][5] + 0.1
    assert any(a.startswith("Review a sample of a4's labels") for a in r["actions"])
    assert any(a.startswith("Add a guideline rule that separates") for a in r["actions"])
    # spellings are counted as one label, and the merge goes through the data quality review
    assert len(next(tb for tb in r["tables"] if tb["title"] == "Labels")["rows"]) == 3
    assert any("Merge the spellings" in a and "data quality review" in a for a in r["actions"])
    checks = {c["name"]: c["passed"] for c in r["confidence"]["checks"]}
    assert checks["labels_consistent"] is False and checks["labels_complete"] is False
    assert r["supporting"]["queries"] and any("does not relabel" in x for x in r["supporting"]["assumptions"])


def test_disagreements_lead_when_asked_and_list_the_hardest_items(lyst):
    owner = _open(lyst)
    r = ask(lyst, owner, "Which items do annotators disagree on?", ws=WS)["result"]
    assert r["answer"].startswith("Annotators disagree on ")
    table = next(tb for tb in r["tables"] if tb["title"].startswith("Items to adjudicate"))
    assert table["columns"][-1] == "text" and table["rows"][0][2] == "tie"  # a three-way split has no majority
    shares = [row[3] for row in table["rows"]]
    assert shares == sorted(shares)


def test_a_reader_who_may_not_see_the_labels_gets_no_audit(lyst):
    owner = _open(lyst)
    viewer = lyst.guard.create_user("v@labs.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    lyst.data.set_column_policy(owner, WS, "annotations", "label", "confidential", "admin")
    r = ask(lyst, viewer, "How consistent are the annotators?", ws=WS)["result"]
    assert r.get("mode") != "annotations" or r["status"] == "needs_input"


def test_a_workspace_without_labels_is_not_audited(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Check the label quality")["result"]
    assert r.get("mode") != "annotations"


def test_krippendorff_alpha_matches_the_textbook_example():
    # Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability", nominal data, 4 coders x 12 units: 0.743
    coders = [[1, 2, 3, 3, 2, 1, 4, 1, 2, None, None, None],
              [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, None, 3],
              [None, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, None],
              [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, None]]
    units = [[c[i] for c in coders if c[i] is not None] for i in range(12)]
    assert A.krippendorff_alpha(units) == pytest.approx(0.743, abs=0.001)
    assert A.krippendorff_alpha([["a", "a"], ["b", "b"], ["a", "a", "a"]]) == 1.0
    assert A.krippendorff_alpha([["a", "a"], ["a", "b"]]) == pytest.approx(0.0)
    assert A.krippendorff_alpha([["a"]]) is None


def test_find_labeled_needs_a_label_and_an_item():
    cols = [{"name": "item_id"}, {"name": "annotator"}, {"name": "label"}]
    got = A.find_labeled([{"name": "pnl", "columns": [{"name": "month"}, {"name": "revenue"}]},
                          {"name": "judgments", "columns": cols}])
    assert got and (got["dataset"], got["item"], got["annotator"], got["label"]) == ("judgments", "item_id", "annotator", "label")
    assert A.find_labeled([{"name": "x", "columns": [{"name": "label"}]}]) is None


@pytest.mark.parametrize(("raw", "norm"), [("Positive ", "positive"), ("NEGATIVE", "negative"), ("not_sure", "not sure"),
                                           ("neutral.", "neutral"), ("", None), (None, None)])
def test_normalize(raw, norm):
    assert A.normalize(raw) == norm


@pytest.mark.parametrize("text", ["How consistent are the annotators?", "Which items do annotators disagree on?",
                                  "Check the label quality", "What is the inter-annotator agreement?",
                                  "What labels are used and how often?"])
def test_audit_phrases(text):
    assert A.asks_for_audit(text)


@pytest.mark.parametrize("text", ["What was revenue last quarter?", "What do our documents say about labeling rules?"])
def test_not_audit_phrases(text):
    assert not A.asks_for_audit(text)
