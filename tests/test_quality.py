# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data quality, reviewed row by row and resolved with Lyst: every flagged row is listed with Lyst's proposal and its
reason, a person's call is recorded, and nothing in the data changes until an approved resolution writes a new
version (which can be undone)."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.security import AccessDenied

WS, DS = "northwind", "pnl_ledger"


def _version(lyst, owner):
    return next(d for d in lyst.data.catalog(owner, WS) if d["name"] == DS)["version"]


def _rows(lyst, owner):
    r = lyst.data.query(owner, WS, f'SELECT * FROM "{DS}"', max_rows=100_000)
    return pd.DataFrame(r.rows, columns=r.columns)


def test_every_flagged_row_is_listed_with_a_reasoned_proposal(seeded):
    lyst, owner = seeded
    r = lyst.quality.report(owner, WS, DS)
    assert r["counts"] == {"duplicate": 1, "negative": 1, "null": 1} and r["open"] == 3 and r["fixable"] == 3
    by = {i["kind"]: i for i in r["issues"]}
    dup, null, neg = by["duplicate"], by["null"], by["negative"]
    # a duplicate names the row it copies; dropping it loses nothing
    assert dup["proposal"]["action"] == "drop" and str(dup["original"]) in dup["why"]
    assert dup["column"] is None and dup["values"]["region"] == "APAC" and dup["severity"] == "high"
    # an empty revenue is reconciled from the identity the ledger carries, not guessed
    assert null["column"] == "revenue" and null["value"] is None
    assert null["proposal"]["action"] == "set" and null["proposal"]["confidence"] == "high"
    assert "revenue = " in null["proposal"]["reason"] and "operating_income" in null["proposal"]["reason"]
    row = null["values"]
    assert null["proposal"]["value"] == pytest.approx(row["cogs"] + row["opex"] + row["operating_income"], abs=0.02)
    # a negative cogs whose positive makes the row reconcile is a flipped sign
    assert neg["column"] == "cogs" and neg["value"] < 0
    assert neg["proposal"] == {**neg["proposal"], "action": "set", "value": -neg["value"], "confidence": "high"}
    # the table carries the dataset's own columns, typed for display
    assert [c["name"] for c in r["columns"]][:3] == ["month", "region", "product_line"]
    assert {c["name"]: c["type"] for c in r["columns"]}["revenue"] == "n"


def test_lyst_only_stages_and_nothing_changes_until_an_approval(seeded):
    lyst, owner = seeded
    v0 = _version(lyst, owner)
    staged = lyst.quality.propose(owner, WS, DS)
    assert staged["proposed"] == 3 and len(staged["staged"]) == 3
    assert {c["origin"] for c in staged["staged"]} == {"lyst"} and all(c["reason"] for c in staged["staged"])
    assert staged["impact"]["rows_before"] == 397 and staged["impact"]["rows_after"] == 396
    assert _version(lyst, owner) == v0  # staged is not applied
    res = lyst.quality.apply(owner, WS, DS)
    assert res["status"] == "pending" and res["approval_id"] and "fix 2 cells, remove 1 row" in res["summary"]
    assert _version(lyst, owner) == v0  # requested is not applied either
    pending = lyst.runtime.approvals(owner, WS)
    assert any(a["id"] == res["approval_id"] and a["action"] == "quality.resolve" for a in pending)
    lyst.runtime.decide(owner, res["approval_id"], True)
    v1 = _version(lyst, owner)
    assert v1 == v0 + 1
    after = _rows(lyst, owner)
    assert len(after) == 396 and after["revenue"].notna().all() and (after["cogs"] >= 0).all()
    assert not after.duplicated().any()
    # the same schema: a fix never turns a date into a timestamp or a number into text
    schema = {d["name"]: d["schema"] for d in lyst.data.catalog(owner, WS)}[DS]
    assert {c["name"]: c["type"] for c in schema}["month"] == "date"
    r = lyst.quality.report(owner, WS, DS)
    assert r["total"] == 0 and r["recent"][0]["status"] == "applied" and r["recent"][0]["result_version"] == v1
    # the earlier version is kept: undo puts it back as a new version
    undone = lyst.quality.undo(owner, WS, res["id"])
    assert undone["version"] == v1 + 1 and len(_rows(lyst, owner)) == 397
    assert lyst.quality.report(owner, WS, DS)["counts"] == {"duplicate": 1, "negative": 1, "null": 1}


def test_a_persons_calls_accept_fix_exclude_and_reopen(seeded):
    lyst, owner = seeded
    r = lyst.quality.report(owner, WS, DS)
    by = {i["kind"]: i for i in r["issues"]}
    r = lyst.quality.review(owner, WS, DS, [by["negative"]["id"]], "accept", note="a supplier credit")
    neg = next(i for i in r["issues"] if i["kind"] == "negative")
    assert neg["status"] == "valid" and neg["review"]["note"] == "a supplier credit" and r["open"] == 2
    with pytest.raises(ValueError, match="holds numbers"):
        lyst.quality.review(owner, WS, DS, [by["null"]["id"]], "fix", value="lots")
    with pytest.raises(ValueError, match="exclude it instead"):
        lyst.quality.review(owner, WS, DS, [by["duplicate"]["id"]], "fix", value=1)
    r = lyst.quality.review(owner, WS, DS, [by["null"]["id"]], "fix", value="1,234.5")
    assert next(c for c in r["staged"] if c["kind"] == "null")["after"] == 1234.5
    r = lyst.quality.review(owner, WS, DS, [by["duplicate"]["id"]], "exclude")
    assert {c["action"] for c in r["staged"]} == {"set", "drop"} and {c["origin"] for c in r["staged"]} == {"person"}
    # Lyst does not override a person's call: only open issues get its proposal
    assert lyst.quality.propose(owner, WS, DS)["proposed"] == 0
    r = lyst.quality.review(owner, WS, DS, [by["duplicate"]["id"]], "reopen")
    assert next(i for i in r["issues"] if i["kind"] == "duplicate")["status"] == "open" and len(r["staged"]) == 1
    # an accepted row stays accepted when the data is unchanged (the review is keyed on the row, not a version)
    lyst.quality._cache.clear()
    assert next(i for i in lyst.quality.report(owner, WS, DS)["issues"] if i["kind"] == "negative")["status"] == "valid"


def test_a_resolution_on_data_that_changed_is_not_applied(seeded):
    lyst, owner = seeded
    lyst.quality.propose(owner, WS, DS)
    res = lyst.quality.apply(owner, WS, DS)
    lyst.data.ingest(owner, WS, DS, demo.csv_bytes(demo.ledger(demo.pnl()).head(50)), filename="ledger.csv")
    v = _version(lyst, owner)
    lyst.runtime.decide(owner, res["approval_id"], True)
    assert _version(lyst, owner) == v  # nothing written over the newer data
    r = lyst.quality.report(owner, WS, DS)
    assert r["recent"][0]["status"] == "stale" and "changed" in r["recent"][0]["error"]


def test_a_rejected_resolution_puts_the_changes_back_to_staged(seeded):
    lyst, owner = seeded
    lyst.quality.propose(owner, WS, DS)
    res = lyst.quality.apply(owner, WS, DS)
    waiting = lyst.quality.report(owner, WS, DS)
    assert waiting["staged"] == [] and waiting["pending"][0]["id"] == res["id"]
    assert {i["status"] for i in waiting["issues"]} == {"pending"}
    lyst.runtime.decide(owner, res["approval_id"], False)
    r = lyst.quality.report(owner, WS, DS)
    assert len(r["staged"]) == 3 and r["recent"][0]["status"] == "rejected"


def test_who_can_review_and_who_can_approve(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("val@northwind.test", "Val")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    editor = lyst.guard.create_user("eli@northwind.test", "Eli")
    lyst.guard.add_member(owner, WS, editor.user_id, "editor")
    assert lyst.quality.report(viewer, WS, DS)["total"] == 3  # a viewer sees what is flagged
    issue = lyst.quality.report(viewer, WS, DS)["issues"][0]["id"]
    with pytest.raises(AccessDenied):
        lyst.quality.review(viewer, WS, DS, [issue], "accept")
    with pytest.raises(AccessDenied):
        lyst.quality.propose(viewer, WS, DS)
    lyst.quality.propose(editor, WS, DS)
    res = lyst.quality.apply(editor, WS, DS)
    with pytest.raises(AccessDenied):  # an editor requests; an admin decides
        lyst.runtime.decide(editor, res["approval_id"], True)


def test_the_routes(seeded):
    lyst, owner = seeded
    token = lyst.guard.issue_token(owner)
    with TestClient(create_app(lyst, start_workers=False)) as http:
        h = {"Authorization": f"Bearer {token}"}
        base = f"/v1/workspaces/{WS}/datasets/{DS}/quality"
        r = http.get(base, headers=h).json()
        assert r["open"] == 3
        r = http.post(f"{base}/review", headers=h, json={"issue_ids": [r["issues"][0]["id"]], "decision": "accept"}).json()
        assert r["valid"] == 1
        assert http.post(f"{base}/review", headers=h, json={"issue_ids": ["nope"], "decision": "accept"}).status_code == 404
        assert http.post(f"{base}/propose", headers=h, json={}).json()["proposed"] == 2
        res = http.post(f"{base}/apply", headers=h, json={}).json()
        assert http.post(f"/v1/approvals/{res['approval_id']}", headers=h, json={"approve": True}).status_code == 200
        assert http.get(base, headers=h).json()["recent"][0]["status"] == "applied"
        assert http.post(f"/v1/workspaces/{WS}/quality/resolutions/{res['id']}/undo", headers=h).json()["status"] == "undone"


def test_dataset_actions_offer_the_review_everywhere(seeded):
    lyst, owner = seeded
    acts = {a["id"]: a for a in lyst.ambient.actions(owner, WS, "dataset", DS)}
    assert acts["dataset.quality"]["href"] == f"/data?dataset={DS}&review=quality"


def test_a_quality_answer_opens_its_review_and_reads_plainly(seeded):
    from .conftest import ask

    lyst, owner = seeded
    t = ask(lyst, owner, "Profile pnl_ledger for data quality issues")
    res = t["result"]
    assert res["open"] == {"kind": "quality", "id": DS, "title": f"Flagged rows in {DS}"}
    nulls = next(c for c in res["confidence"]["checks"] if c["name"] == "nulls")
    assert nulls["detail"] == "1 empty cell in revenue" and "{" not in nulls["detail"]


def test_a_category_spelled_another_way_is_flagged_and_merged_on_approval(lyst):
    import io

    owner = lyst.guard.create_user("ana@labs.test", "Ana")
    lyst.guard.create_workspace(owner, "labs", "Labeling team")
    labels = ["positive"] * 40 + ["negative"] * 30 + ["Positive "] * 2 + ["NEGATIVE"] + ["neutral"] * 20 + ["neutral."]
    df = pd.DataFrame({"item_id": [f"T{i:03d}" for i in range(len(labels))], "label": labels})
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    lyst.data.ingest(owner, "labs", "annotations", buf.getvalue().encode(), filename="annotations.csv")
    r = lyst.quality.report(owner, "labs", "annotations")
    assert r["counts"] == {"variant": 4}
    by = {i["value"]: i for i in r["issues"]}
    assert set(by) == {"Positive ", "NEGATIVE", "neutral."}
    pos = by["Positive "]
    assert pos["label"] == "Inconsistent spelling" and pos["column"] == "label" and "'positive'" in pos["why"]
    assert pos["proposal"] == {**pos["proposal"], "action": "set", "value": "positive", "confidence": "high"}
    # an identifier column is not a category: T001 and T010 never collide, and free text is left alone
    assert all(i["column"] == "label" for i in r["issues"])
    staged = lyst.quality.propose(owner, "labs", "annotations")
    assert staged["proposed"] == 4
    res = lyst.quality.apply(owner, "labs", "annotations")
    lyst.runtime.decide(owner, res["approval_id"], True)
    out = lyst.data.query(owner, "labs", 'SELECT label, count(*) FROM "annotations" GROUP BY 1 ORDER BY 1')
    assert dict(out.rows) == {"negative": 31, "neutral": 21, "positive": 42}
    assert lyst.quality.report(owner, "labs", "annotations")["counts"] == {}
