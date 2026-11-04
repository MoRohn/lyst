# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Onboarding: discovery from metadata, domain recognition, proposals that only become authoritative when an
administrator accepts them, the versioned business profile, first-value milestones, and isolation between
organizations (scenarios A and B)."""

from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst import domains
from lyst.api import create_app
from lyst.security import AccessDenied

WS = "acme"


def _csv(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode()


def _acme(lyst):
    owner = lyst.guard.create_user("ops@acme.test", "Ops Lead")
    lyst.guard.create_workspace(owner, WS, "Acme Manufacturing")
    months = pd.date_range("2025-01-01", periods=12, freq="MS")
    po = pd.DataFrame([{"po_number": f"PO{m.month:02d}{i}", "order_date": m.date(), "supplier": f"Supplier {i % 4}",
                        "category": ["Steel", "Packaging", "Freight"][i % 3], "spend": 1000.0 + 37 * i + m.month,
                        "lead_time_days": 5 + i % 7} for m in months for i in range(8)])
    staff = pd.DataFrame([{"employee_id": f"E{i:03d}", "email": f"person{i}@acme.test", "department": ["Plant", "Office"][i % 2],
                           "hire_date": months[i % 12].date(), "salary": 50000.0 + 900 * i} for i in range(40)])
    lyst.data.ingest(owner, WS, "purchase_orders", _csv(po), filename="po.csv", title="Purchase orders",
                     description="Purchase orders from the purchasing system")
    lyst.data.ingest(owner, WS, "employees", _csv(staff), filename="employees.csv", title="Employees",
                     description="Employee roster from the HR system", classification="confidential")
    return owner


def test_packages_load_and_are_versioned():
    pk = domains.packages()
    assert len(pk) >= 13 and {"finance", "procurement", "sales", "workforce", "healthcare"} <= set(pk)
    for p in pk.values():
        assert p.version and p.signals and p.metrics and p.questions


def test_scenario_a_new_organization_reaches_a_verified_first_answer(lyst):
    owner = _acme(lyst)
    ob = lyst.onboarding
    st = ob.status(owner, WS)
    assert st["next"] == "discover" and st["milestones"]["seconds_to_first_dataset"] is not None

    found = ob.discover(owner, WS)
    ids = [d["id"] for d in found["domains"]]
    assert ids[0] == "procurement" and "workforce" in ids
    proc = found["domains"][0]
    assert proc["strength"] == "strong" and any(e["where"].startswith("purchase_orders") for e in proc["evidence"])

    props = {p["id"]: p for p in ob.proposals(owner, WS)}
    assert props["metric:spend"]["payload"]["measure"] == {"agg": "sum", "column": "spend", "filters": []}
    assert props["metric:spend"]["payload"]["time_column"] == "order_date"
    assert props["metric:spend"]["authority"] == "proposed"
    assert "metric:headcount" in props and props["metric:headcount"]["payload"]["measure"]["agg"] == "count_distinct"
    assert "policy:employees.email" in props  # personal data gets a column policy suggestion
    assert "contract:purchase_orders" in props
    assert any(k.startswith("question:procurement") for k in props)
    # proposals are not authority: nothing is defined until accepted
    assert not lyst.semantic.metrics(owner, WS)

    r = ob.accept(owner, WS, "metric:spend")
    assert r["result"] == {"metric": "spend", "version": 1}
    assert [m.name for m in lyst.semantic.metrics(owner, WS)] == ["spend"]
    hist = lyst.semantic.history(owner, WS, "spend")
    assert "onboarding proposal metric:spend" in hist[0]["note"]
    ob.accept(owner, WS, "policy:employees.email")
    assert lyst.data.policy_for(lyst.guard.user_by_email("ops@acme.test"), WS)  # policy recorded
    ob.accept(owner, WS, "contract:purchase_orders")
    assert lyst.contracts.status(WS, "purchase_orders")
    ob.dismiss(owner, WS, "metric:headcount")

    # a second discovery keeps decisions: accepted and dismissed proposals are not proposed again
    again = ob.discover(owner, WS)
    left = {p["id"] for p in again["proposals"]}
    assert "metric:spend" not in left and "metric:headcount" not in left

    q = next(p for p in ob.proposals(owner, WS, kind="question") if "spend" in p["title"].lower())
    tid = ob.accept(owner, WS, q["id"])["result"]["task_id"]
    lyst.runtime.drain()
    assert lyst.runtime.get(owner, tid)["status"] == "done"
    st = ob.status(owner, WS)
    m = st["milestones"]
    assert m["seconds_to_first_analysis"] is not None and m["interventions"] >= 4
    assert {s["key"]: s["done"] for s in st["steps"]}["confirm"]


def test_accepting_needs_the_governed_permission(lyst):
    owner = _acme(lyst)
    viewer = lyst.guard.create_user("viewer@acme.test", "Viewer")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    lyst.onboarding.discover(owner, WS)
    assert lyst.onboarding.proposals(viewer, WS)  # members may read suggestions
    with pytest.raises(AccessDenied):
        lyst.onboarding.accept(viewer, WS, "metric:spend")
    with pytest.raises(AccessDenied):
        lyst.onboarding.dismiss(viewer, WS, "metric:spend")
    with pytest.raises(AccessDenied):
        lyst.onboarding.set_profile(viewer, WS, {"objectives": ["cut spend"]})
    with pytest.raises(AccessDenied):
        lyst.onboarding.discover(viewer, WS)
    assert lyst.onboarding.proposals(owner, WS, kind="metric")[0]["status"] == "proposed"


def test_profile_is_versioned_and_terms_resolve_to_approved_metrics(lyst):
    owner = _acme(lyst)
    ob = lyst.onboarding
    ob.discover(owner, WS)
    ob.accept(owner, WS, "metric:spend")
    with pytest.raises(ValueError):
        ob.set_profile(owner, WS, {"terminology": {"outlay": "not_a_metric"}})
    with pytest.raises(ValueError):
        ob.set_profile(owner, WS, {"domains": ["astrology"]})
    v1 = ob.set_profile(owner, WS, {"domains": ["procurement"], "fiscal_year_start_month": 4, "objectives": ["Cut freight spend 10%"]})
    v2 = ob.set_profile(owner, WS, {"terminology": {"Outlay": "spend"}})
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v2["spec"]["fiscal_year_start_month"] == 4 and v2["spec"]["packages"] == {"procurement": "1.0.0"}
    assert [h["version"] for h in ob.profile_history(owner, WS)] == [2, 1]
    term = {g["term"]: g for g in lyst.semantic.glossary(owner, WS)}["outlay"]
    assert term["maps_to_metric"] == "spend" and term["source"] == "business profile v2"
    assert lyst.semantic.resolve(owner, WS, "what was outlay last quarter")["matched"] == {"spend": "outlay"}


def test_scenario_b_two_organizations_stay_isolated(seeded):
    lyst, avery = seeded  # northwind: finance and customer service
    _acme(lyst)
    acme_owner = lyst.guard.user_by_email("ops@acme.test")
    nw = lyst.onboarding.discover(avery, "northwind")
    ac = lyst.onboarding.discover(acme_owner, WS)
    assert nw["domains"][0]["id"] == "finance" and ac["domains"][0]["id"] == "procurement"
    nw_ids = {p["id"] for p in nw["proposals"]}
    assert "metric:spend" not in nw_ids and "metric:revenue" not in nw_ids  # revenue is already defined at Northwind
    lyst.onboarding.set_profile(acme_owner, WS, {"domains": ["procurement"]})
    assert lyst.onboarding.profile(avery, "northwind")["version"] == 0
    with pytest.raises(AccessDenied):
        lyst.onboarding.proposals(avery, WS)
    with pytest.raises(AccessDenied):
        lyst.onboarding.accept(acme_owner, "northwind", sorted(nw_ids)[0])
    for pid in nw_ids:  # whatever the kind, a non-member is refused before anything else is checked
        with pytest.raises(AccessDenied):
            lyst.onboarding.accept(acme_owner, "northwind", pid)


def test_onboarding_api(lyst):
    owner = _acme(lyst)
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get("/v1/domains", headers=h).json()
        assert c.get("/v1/domains/finance", headers=h).json()["id"] == "finance"
        assert c.get("/v1/domains/nope", headers=h).status_code == 404
        assert c.get(f"/v1/workspaces/{WS}/onboarding", headers=h).json()["next"] == "discover"
        d = c.post(f"/v1/workspaces/{WS}/onboarding/discover", headers=h).json()
        assert d["domains"][0]["id"] == "procurement"
        r = c.post(f"/v1/workspaces/{WS}/onboarding/proposals/metric:spend/accept", json={"edits": {"title": "Supplier spend"}}, headers=h)
        assert r.status_code == 200, r.text
        assert lyst.semantic.get(owner, WS, "spend").title == "Supplier spend"
        assert c.post(f"/v1/workspaces/{WS}/onboarding/proposals/metric:spend/accept", headers=h).status_code == 400
        all_ = c.get(f"/v1/workspaces/{WS}/onboarding/proposals", params={"status": "all"}, headers=h).json()
        assert any(p["status"] == "accepted" for p in all_)
        p = c.put(f"/v1/workspaces/{WS}/profile", json={"objectives": ["Know our spend"]}, headers=h).json()
        assert p["version"] == 1
        assert c.get(f"/v1/workspaces/{WS}/profile", params={"history": True}, headers=h).json()[0]["version"] == 1
