# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Source precedence: an authoritative source answers when two definitions compute the same number from different sources."""

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.security import AccessDenied
from lyst.semantic import Measure, MetricSpec

from .conftest import ask

WS = "northwind"


def _ledger_revenue(lyst, owner):
    lyst.semantic.define(owner, WS, MetricSpec(name="ledger_revenue", title="Ledger revenue", dataset="pnl_ledger",
                                               measure=Measure(agg="sum", column="revenue"), time_column="month",
                                               dimensions=["region"], synonyms=["booked revenue"], format="currency"))
    spec = lyst.semantic.get(owner, WS, "revenue")
    lyst.semantic.define(owner, WS, spec.model_copy(update={"synonyms": [*spec.synonyms, "booked revenue"]}))


def test_without_precedence_the_shared_term_is_ambiguous_and_the_audit_says_so(seeded):
    lyst, owner = seeded
    _ledger_revenue(lyst, owner)
    r = lyst.semantic.resolve(owner, WS, "What was booked revenue last quarter?")
    assert r["ambiguous"] == {"booked revenue": ["ledger_revenue", "revenue"]} and r["precedence"] == {}
    kinds = {i["kind"] for i in lyst.semantic.audit(owner, WS)}
    assert "conflicting_source" in kinds


def test_the_authoritative_source_answers_and_the_answer_says_why(seeded):
    lyst, owner = seeded
    _ledger_revenue(lyst, owner)
    lyst.semantic.set_authority(owner, WS, "pnl", "authoritative", subject="P&L", note="Closed books")
    lyst.semantic.set_authority(owner, WS, "pnl_ledger", "secondary")
    r = lyst.semantic.resolve(owner, WS, "What was booked revenue last quarter?")
    assert r["metrics"] == ["revenue"] and r["ambiguous"] == {}
    assert r["precedence"]["booked revenue"] == {"chosen": "revenue", "over": ["ledger_revenue"],
                                                 "because": "pnl is the authoritative source for P&L"}
    task = ask(lyst, owner, "What was booked revenue last quarter?")
    assert task["status"] == "done"
    assumptions = task["result"]["supporting"]["assumptions"]
    assert any("Lyst used revenue because pnl is the authoritative source for P&L" in a for a in assumptions)
    issues = lyst.semantic.audit(owner, WS)
    assert not any(i["kind"] in ("conflicting_source", "ambiguous_term") and "ledger_revenue" in i["items"] for i in issues)
    assert any(i["kind"] == "secondary_source_definition" and i["items"] == ["ledger_revenue", "revenue"] for i in issues)
    rows = {r["name"]: r for r in lyst.catalog.overview(owner, WS)}
    assert rows["pnl"]["authority"] == "authoritative" and rows["pnl_ledger"]["authority"] == "secondary"
    assert not rows["pnl_ledger"]["trusted"]
    hits = [h["name"] for h in lyst.catalog.search(owner, WS, "booked revenue")["results"] if h["kind"] == "metric"]
    assert hits.index("revenue") < hits.index("ledger_revenue")
    assert lyst.catalog.explain_metric(owner, WS, "revenue")["authority"]["level"] == "authoritative"


def test_secondary_alone_is_enough_and_permissions(seeded):
    lyst, owner = seeded
    _ledger_revenue(lyst, owner)
    lyst.semantic.set_authority(owner, WS, "pnl_ledger", "secondary")
    assert lyst.semantic.resolve(owner, WS, "booked revenue by region")["metrics"] == ["revenue"]
    editor = lyst.guard.create_user("e@northwind.test", "E")
    lyst.guard.add_member(owner, WS, editor.user_id, "editor")
    with pytest.raises(AccessDenied):
        lyst.semantic.set_authority(editor, WS, "pnl", "authoritative")
    with pytest.raises(ValueError):
        lyst.semantic.set_authority(owner, WS, "pnl", "primary")
    lyst.semantic.set_authority(owner, WS, "pnl_ledger", None)
    assert "booked revenue" in lyst.semantic.resolve(owner, WS, "booked revenue by region")["ambiguous"]


def test_authority_routes(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.put(f"/v1/workspaces/{WS}/datasets/pnl/authority", json={"level": "authoritative", "subject": "P&L"}, headers=h)
        assert r.json() == {"dataset": "pnl", "level": "authoritative", "subject": "P&L"}
        assert c.get(f"/v1/workspaces/{WS}/source-authority", headers=h).json()["pnl"]["level"] == "authoritative"
        assert c.put(f"/v1/workspaces/{WS}/datasets/nope/authority", json={"level": "secondary"}, headers=h).status_code == 404
