# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data intelligence: catalog insights, relationships, data contracts, metric lineage and history, semantic audit, search.

Acceptance scenarios A (catalog intelligence), B (metric lineage), D (changed schema), F (dependency change) and G
(permission-aware search) from the seventh master prompt run here against the demo workspace."""

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.contracts import ContractSpec
from lyst.security import AccessDenied
from lyst.semantic import Measure, MetricSpec

from .conftest import ask

WS = "northwind"


def _ingest(lyst, owner, name, frame, filename=None):
    return lyst.data.ingest(owner, WS, name, demo.csv_bytes(frame), filename=filename or f"{name}.csv")


def test_a_catalog_finds_grain_keys_relationships_and_planted_problems(seeded):
    lyst, owner = seeded
    out = lyst.catalog.discover(owner, WS)
    rows = {r["name"]: r for r in lyst.catalog.overview(owner, WS)}
    assert rows["pnl"]["grain"] == ["month", "product_line", "region"]
    assert rows["staffing"]["keys"] == ["month"]
    assert rows["demand"]["grain"] == ["month", "channel"]
    # the ledger's planted double booking: no grain, one repeated row, named with an example
    ledger = lyst.catalog.insights(owner, WS, "pnl_ledger")
    assert ledger["grain"] is None and ledger["duplicates_at_grain"] == 1
    kinds = {r["kind"] for r in ledger["risks"]}
    assert {"duplicates_at_grain", "missing_values", "negative_amount"} <= kinds
    missing = next(r for r in ledger["risks"] if r["kind"] == "missing_values")
    assert "LATAM" in missing["text"] or "Hardware" in missing["text"] or "2026-08" in missing["text"]
    revenue = next(c for c in ledger["columns"] if c["name"] == "revenue")
    assert revenue["semantic_type"] == "amount" and revenue["null_rows"] == 1
    assert next(c for c in ledger["columns"] if c["name"] == "month")["semantic_type"] == "date"
    assert next(c for c in ledger["columns"] if c["name"] == "region")["semantic_type"] == "geography"
    # relationships: a foreign key to staffing's key, shared dimensions, and the overlap reconciled by totals
    rels = out["relationships"]
    assert any(r["kind"] == "foreign_key" and r["from_dataset"] == "pnl" and r["to_dataset"] == "staffing" for r in rels)
    overlap = next(r for r in rels if r["kind"] == "overlap")
    assert {overlap["from_dataset"], overlap["to_dataset"]} == {"pnl", "pnl_ledger"}
    assert "revenue" in overlap["evidence"]["differing"]
    totals = overlap["evidence"]["totals"]["revenue"]
    assert totals["difference"] == pytest.approx(totals["pnl_ledger"] - totals["pnl"])
    # recorded in the knowledge graph, so lineage walks see the joins
    graph = lyst.knowledge.lineage(owner, WS, "dataset", "pnl")
    assert any(u["name"] == "staffing" for u in graph["upstream"])
    assert rows["pnl"]["freshness"]["latest"] == "2026-09-01"


def test_insights_are_computed_on_ingest_with_time_gaps_and_inconsistent_definitions(seeded):
    lyst, owner = seeded
    months = pd.date_range("2025-01-01", "2025-12-01", freq="MS")
    frame = pd.DataFrame({"month": [m.date() for m in months if m.month != 6], "region": ["emea"] * 11, "budget": [100.0] * 11})
    _ingest(lyst, owner, "budget", frame)
    ins = lyst.catalog.insights(owner, WS, "budget")
    assert ins["continuity"]["gaps"] == ["2025-06-01"]
    assert any(r["kind"] == "time_gaps" for r in ins["risks"])
    rels = lyst.catalog.relationships(owner, WS)
    case = [r for r in rels if r["kind"] == "inconsistent_definition" and "budget" in (r["from_dataset"], r["to_dataset"])
            and r["from_column"] == "region"]
    assert case and "case" in case[0]["text"]


def test_insights_respect_column_and_row_policies(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    lyst.data.set_column_policy(owner, WS, "pnl_ledger", "revenue", "confidential", "admin")
    lyst.data.set_row_policy(owner, WS, "pnl", "region", principal_kind="user", principal=viewer.user_id, allowed_values=["EMEA"])
    ledger = lyst.catalog.insights(viewer, WS, "pnl_ledger")
    assert "revenue" not in {c["name"] for c in ledger["columns"]}
    assert not any("revenue" in r["columns"] for r in ledger["risks"])
    pnl = lyst.catalog.insights(viewer, WS, "pnl")
    assert all("top" not in c for c in pnl["columns"]) and pnl["value_detail"].startswith("hidden")
    hits = lyst.catalog.search(viewer, WS, "ledger revenue")["results"]
    assert "pnl_ledger.revenue" not in {h["name"] for h in hits}
    assert "pnl_ledger.revenue" in {h["name"] for h in lyst.catalog.search(owner, WS, "ledger revenue")["results"]}


def test_b_metric_lineage_from_definition_to_source_and_downstream(seeded):
    lyst, owner = seeded
    assert ask(lyst, owner, "What was operating margin last quarter?")["status"] == "done"
    lyst.monitors.create(owner, WS, metric="operating_margin", grain="month")
    ex = lyst.catalog.explain_metric(owner, WS, "operating_margin")
    assert ex["sql"].startswith("SELECT CAST(SUM(\"operating_income\")")
    assert ex["dataset"]["name"] == "pnl" and ex["dataset"]["origin"] == {"kind": "file", "name": "pnl.csv"}
    assert ex["dataset"]["grain"] == ["month", "product_line", "region"]
    assert set(ex["columns"]) >= {"operating_income", "revenue"}
    kinds = {d["kind"] for d in ex["downstream"]}
    assert {"analysis", "monitor"} <= kinds
    assert any(s["dataset"] == "pnl_ledger" and s["rel"] == "overlaps" for s in ex["related_sources"])
    assert ex["history"][0]["summary"] == "first definition"


def test_metric_history_explains_what_changed_and_deprecation_redirects(seeded):
    lyst, owner = seeded
    spec = lyst.semantic.get(owner, WS, "revenue")
    lyst.semantic.define(owner, WS, spec.model_copy(update={"dimensions": [*spec.dimensions, "product_line"] if "product_line" not in spec.dimensions else spec.dimensions[:1],
                                                            "currency": "USD", "owner": "finance"}), note="finance owns it")
    hist = lyst.semantic.history(owner, WS, "revenue")
    assert hist[0]["version"] == 2 and "currency: None → 'USD'" in hist[0]["summary"] and hist[0]["note"] == "finance owns it"
    lyst.semantic.define(owner, WS, MetricSpec(name="net_revenue", title="Net revenue", dataset="pnl", measure=Measure(agg="sum", column="revenue"),
                                               time_column="month", dimensions=["region"], format="currency", currency="USD", owner="finance",
                                               description="Revenue, the finance definition."))
    lyst.semantic.deprecate(owner, WS, "revenue", replaced_by="net_revenue")
    assert "revenue" not in {m.name for m in lyst.semantic.metrics(owner, WS)}
    assert lyst.semantic.resolve(owner, WS, "What was revenue last month?")["metrics"] == ["net_revenue"]
    assert any(i["kind"] == "duplicate_definition" for i in lyst.semantic.audit(owner, WS)) is False
    with pytest.raises(ValueError):
        lyst.semantic.deprecate(owner, WS, "tickets", replaced_by="revenue")  # not approved any more


def test_semantic_audit_finds_duplicates_conflicting_sources_and_dangling_terms(seeded):
    lyst, owner = seeded
    lyst.semantic.define(owner, WS, MetricSpec(name="sales", title="Sales", dataset="pnl", measure=Measure(agg="sum", column="revenue"),
                                               time_column="month"))
    lyst.semantic.define(owner, WS, MetricSpec(name="ledger_revenue", title="Ledger revenue", dataset="pnl_ledger",
                                               measure=Measure(agg="sum", column="revenue"), time_column="month"))
    lyst.semantic.set_term(owner, WS, "turnover", "Revenue in British usage.", maps_to_metric="sales", source="finance glossary")
    lyst.semantic.deprecate(owner, WS, "sales")
    issues = lyst.semantic.audit(owner, WS)
    kinds = {i["kind"]: i for i in issues}
    assert "conflicting_source" in kinds and "ledger_revenue" in kinds["conflicting_source"]["items"]
    assert kinds["term_points_to_retired_metric"]["items"] == ["turnover", "sales"]
    assert "undefined_term" in kinds  # the demo's 'ebitda'
    lyst.semantic.define(owner, WS, MetricSpec(name="sales2", title="Sales two", dataset="pnl", measure=Measure(agg="sum", column="revenue"),
                                               time_column="month"))
    assert any(i["kind"] == "duplicate_definition" and set(i["items"]) == {"revenue", "sales2"} for i in lyst.semantic.audit(owner, WS))


def test_d_f_contract_catches_a_changed_schema_and_lists_affected_assets(seeded):
    lyst, owner = seeded
    assert ask(lyst, owner, "What was operating margin last quarter?")["status"] == "done"
    lyst.stores.workspace(WS).execute(
        "INSERT INTO dashboards (id, title, design, path, status, created_by) VALUES ('d_margin', 'Margin board', ?, '/tmp/x', 'published', ?)",
        [json.dumps({"metrics": [{"name": "operating_margin"}]}), owner.user_id])
    draft = lyst.contracts.draft(owner, WS, "pnl")
    assert draft["key"] == ["month", "product_line", "region"]
    region = next(c for c in draft["columns"] if c["name"] == "region")
    assert region["required"] and region["accepted_values"] == sorted(demo.REGIONS)
    saved = lyst.contracts.define(owner, WS, "pnl", ContractSpec.model_validate(draft), note="from the draft")
    assert saved["version"] == 1 and saved["check"]["outcome"] == "pass"
    # upstream drops revenue and introduces an unexpected region: the check runs on ingest
    frame = demo.pnl().drop(columns=["revenue"])
    frame.loc[0, "region"] = "Antarctica"
    _ingest(lyst, owner, "pnl", frame)
    status = lyst.catalog.overview(owner, WS)
    pnl = next(r for r in status if r["name"] == "pnl")
    assert pnl["contract"]["outcome"] == "breaking" and not pnl["trusted"]
    check = lyst.contracts.checks(owner, WS, "pnl")[0]
    kinds = {r["kind"] for r in check["results"]}
    assert {"column_removed", "unexpected_values"} <= kinds
    affected = {(i["kind"], i["name"]) for i in check["impacted"]}
    assert ("metric", "operating_margin") in affected and ("metric", "revenue") in affected
    assert ("dashboard", "d_margin") in affected
    assert any(k == "workflow" for k, _ in affected) and any(k == "analysis" for k, _ in affected)
    assert ("metric", "tickets") not in affected
    inbox = lyst.monitors.inbox(owner, WS)
    assert inbox and inbox[0]["kind"] == "contract_breaking" and "Margin board" in inbox[0]["summary"]
    # the semantic audit sees the definitions that no longer compute
    assert any(i["kind"] == "stale_definition" and i["items"] == ["revenue"] for i in lyst.semantic.audit(owner, WS))
    # the same version is not notified twice
    lyst.contracts.check(owner, WS, "pnl")
    assert len(lyst.monitors.inbox(owner, WS)) == 1


def test_contract_reference_and_duplicate_key_failures(seeded):
    lyst, owner = seeded
    spec = ContractSpec.model_validate(lyst.contracts.draft(owner, WS, "demand"))
    assert any(r.dataset == "staffing" for r in spec.references)
    lyst.contracts.define(owner, WS, "demand", spec)
    frame = demo.demand()
    frame = pd.concat([frame, frame.head(1), pd.DataFrame([{"month": pd.Timestamp("2031-01-01").date(), "channel": "email", "tickets": 5}])])
    _ingest(lyst, owner, "demand", frame)
    check = lyst.contracts.checks(owner, WS, "demand")[0]
    assert check["outcome"] == "fail"
    kinds = {r["kind"] for r in check["results"]}
    assert {"duplicate_key", "orphan_reference"} <= kinds
    assert ("metric", "tickets") in {(i["kind"], i["name"]) for i in check["impacted"]}


def test_contract_permissions(seeded):
    lyst, owner = seeded
    editor = lyst.guard.create_user("e@northwind.test", "E")
    lyst.guard.add_member(owner, WS, editor.user_id, "editor")
    spec = ContractSpec.model_validate(lyst.contracts.draft(editor, WS, "pnl"))
    with pytest.raises(AccessDenied):
        lyst.contracts.define(editor, WS, "pnl", spec)
    with pytest.raises(AccessDenied):
        lyst.semantic.deprecate(editor, WS, "revenue")
    with pytest.raises(ValueError):
        lyst.contracts.define(owner, WS, "pnl", spec.model_copy(update={"key": ["nope"]}))


def test_g_search_connects_metrics_datasets_terms_and_documents(seeded):
    lyst, owner = seeded
    lyst.knowledge.add_document(owner, WS, b"Operating margin is operating income divided by revenue, per the finance policy.",
                                filename="margin-policy.md", title="Margin policy")
    res = lyst.catalog.search(owner, WS, "Where does our operating margin come from?")["results"]
    assert res[0]["kind"] == "metric" and res[0]["name"] == "operating_margin"
    assert {"kind": "dataset", "name": "pnl"} in res[0]["related"]
    assert "document" in {h["kind"] for h in res}
    support = lyst.catalog.search(owner, WS, "support tickets staffing")["results"]
    assert {"tickets", "staffing"} <= {h["name"] for h in support}
    assert lyst.catalog.search(owner, WS, "the and of")["results"] == []


def test_api_routes_for_catalog_contracts_and_lineage(seeded):
    lyst, owner = seeded
    tok = lyst.guard.issue_token(owner)
    h = {"Authorization": f"Bearer {tok}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.post(f"/v1/workspaces/{WS}/catalog/discover", headers=h).status_code == 200
        cat = c.get(f"/v1/workspaces/{WS}/catalog", headers=h).json()
        assert {r["name"] for r in cat} >= {"pnl", "pnl_ledger", "demand", "staffing"}
        assert c.get(f"/v1/workspaces/{WS}/datasets/pnl_ledger/insights", headers=h).json()["duplicates_at_grain"] == 1
        assert c.get(f"/v1/workspaces/{WS}/catalog/relationships", headers=h).json()
        assert c.get(f"/v1/workspaces/{WS}/catalog/search", params={"q": "margin"}, headers=h).json()["results"]
        draft = c.get(f"/v1/workspaces/{WS}/datasets/pnl/contract/draft", headers=h).json()
        r = c.post(f"/v1/workspaces/{WS}/datasets/pnl/contract", json={"spec": draft, "note": "v1"}, headers=h)
        assert r.status_code == 200 and r.json()["check"]["outcome"] == "pass"
        assert c.get(f"/v1/workspaces/{WS}/datasets/pnl/contract", headers=h).json()["active"]["version"] == 1
        assert c.post(f"/v1/workspaces/{WS}/datasets/pnl/contract/check", headers=h).json()["outcome"] == "pass"
        assert len(c.get(f"/v1/workspaces/{WS}/datasets/pnl/contract/checks", headers=h).json()) == 2
        imp = c.get(f"/v1/workspaces/{WS}/datasets/pnl/impact", params={"columns": "cogs"}, headers=h).json()
        assert {i["name"] for i in imp if i["kind"] == "metric"} == {"cogs_ratio"}
        lin = c.get(f"/v1/workspaces/{WS}/metrics/operating_margin/lineage", headers=h).json()
        assert lin["dataset"]["name"] == "pnl" and lin["contract"]["outcome"] == "pass"
        assert c.get(f"/v1/workspaces/{WS}/metrics/revenue/history", headers=h).json()[0]["version"] == 1
        assert c.get(f"/v1/workspaces/{WS}/semantic/audit", headers=h).json()
        assert c.post(f"/v1/workspaces/{WS}/metrics/tickets/deprecate", json={}, headers=h).json()["status"] == "deprecated"
        assert c.get(f"/v1/workspaces/{WS}/metrics/nope/lineage", headers=h).status_code == 404
        assert c.delete(f"/v1/workspaces/{WS}/datasets/pnl/contract", headers=h).json() == {"ok": True}


def test_contract_edge_rules_null_keys_empty_sets_and_values_kept_out_of_inboxes(seeded):
    lyst, owner = seeded
    frame = pd.DataFrame({"id": [1, 2, None, None], "tier": ["gold", "silver", "gold", "x"], "ssn": ["a", "b", "c", "d"]})
    _ingest(lyst, owner, "people", frame)
    lyst.data.set_column_policy(owner, WS, "people", "ssn", "restricted", "admin")
    spec = ContractSpec.model_validate({
        "columns": [{"name": "id", "type": "double", "required": False}, {"name": "tier", "type": "varchar", "accepted_values": []},
                    {"name": "ssn", "type": "varchar", "accepted_values": ["a"]}], "key": ["id"]})
    saved = lyst.contracts.define(owner, WS, "people", spec)
    results = {r["kind"] + ":" + r["columns"][0]: r for r in saved["check"]["results"]}
    assert "duplicate_key:id" not in results  # two NULL keys are incomplete rows, not a repeated key
    assert results["unexpected_values:tier"]["rows"] == 4  # an empty accepted set accepts nothing
    protected = results["unexpected_values:ssn"]
    assert "values" not in protected and "b" not in protected["text"]
    inbox = lyst.monitors.inbox(owner, WS)
    assert inbox and "gold" not in inbox[0]["summary"] and "silver" not in inbox[0]["summary"]
    assert lyst.contracts.status(WS, "people").keys() == {"version", "outcome", "dataset_version", "checked_at", "problems"}


def test_search_matches_a_word_typed_only_in_part(seeded):
    lyst, owner = seeded
    names = [h["name"] for h in lyst.catalog.search(owner, "northwind", "rev")["results"]]
    assert "revenue" in names[:3]
    # a whole word still ranks above a partial one, and two letters are not enough to guess from
    full = lyst.catalog.search(owner, "northwind", "revenue")["results"]
    part = lyst.catalog.search(owner, "northwind", "rev")["results"]
    assert next(h for h in full if h["name"] == "revenue")["score"] > next(h for h in part if h["name"] == "revenue")["score"]
    assert not lyst.catalog.search(owner, "northwind", "re")["results"]
