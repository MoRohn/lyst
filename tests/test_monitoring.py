# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario D: monitors detect a material change, investigate it, rank, deduplicate and notify."""

import json

import httpx
import pytest

from lyst.monitoring import MonitorService
from lyst.security import AccessDenied


def _approve(lyst, owner, approval_id):
    admin = lyst.guard.user_by_email("adm@northwind.test") or lyst.guard.create_user("adm@northwind.test", "Adm")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    lyst.runtime.decide(admin, approval_id, True)


def test_d_monitor_detects_investigates_ranks_and_notifies(seeded):
    lyst, owner = seeded
    received = []

    def hook(req: httpx.Request):
        received.append(json.loads(req.content))
        return httpx.Response(200)

    lyst.monitors.http = httpx.Client(transport=httpx.MockTransport(hook))
    lyst.stores.control.execute("UPDATE workspaces SET webhook_url = 'https://hooks.test/lyst' WHERE id = 'northwind'")
    m1 = lyst.monitors.create(owner, "northwind", metric="operating_margin", grain="month", threshold_sigma=3.0)
    m2 = lyst.monitors.create(owner, "northwind", metric="revenue", grain="month", threshold_sigma=3.0)
    assert lyst.monitors.tick("northwind") == []            # nothing runs before approval
    _approve(lyst, owner, m1["approval_id"])
    _approve(lyst, owner, m2["approval_id"])
    found = lyst.monitors.tick("northwind")
    kinds = {(f["metric"], f["kind"]) for f in found}
    assert ("operating_margin", "metric_shift") in kinds, found
    assert all(f["metric"] != "revenue" or f["kind"] != "metric_shift" for f in found)  # revenue is on trend
    top = found[0]
    assert top["metric"] == "operating_margin" and top["direction"] == "risk" and top["label"] == "Sep 2026"
    assert top["task_id"]
    lyst.runtime.drain()
    inv = lyst.runtime.get(owner, top["task_id"])
    assert inv["status"] == "done" and inv["kind"] == "investigation" and inv["autonomy"] == "autonomous"
    r = inv["result"]
    assert r["depth"] == "deep" and any("EMEA" in e for e in r["evidence"])
    # notifications: inbox for owner (creator and owner) and admin; webhook delivered with redacted text
    inbox = lyst.monitors.inbox(owner, "northwind")
    assert any(i["id"] == top["id"] and i["read_status"] == "unread" for i in inbox)
    admin = lyst.guard.user_by_email("adm@northwind.test")
    assert any(i["id"] == top["id"] for i in lyst.monitors.inbox(admin, "northwind"))
    hooked = {r["finding_id"]: r for r in received}
    assert top["id"] in hooked and hooked[top["id"]]["investigation_task"] == top["task_id"]
    # dedupe: a second tick (made due again) raises nothing new
    lyst.stores.workspace("northwind").execute("UPDATE monitors SET next_run_at = now() - INTERVAL 1 MINUTE")
    assert lyst.monitors.tick("northwind") == []
    # acknowledging reads the notification
    lyst.monitors.set_status(owner, "northwind", top["id"], "acknowledged")
    assert lyst.monitors.findings(owner, "northwind", status="acknowledged")[0]["id"] == top["id"]
    assert all(i["read_status"] == "read" for i in lyst.monitors.inbox(owner, "northwind") if i["id"] == top["id"])


def test_monitor_authorization(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.monitors.create(viewer, "northwind", metric="revenue")
    with pytest.raises(KeyError):
        lyst.monitors.create(owner, "northwind", metric="no_such_metric")
    m = lyst.monitors.create(owner, "northwind", metric="revenue")
    with pytest.raises(AccessDenied):  # the creator cannot approve their own monitor as a non-admin? owners can; a viewer cannot
        lyst.runtime.decide(viewer, m["approval_id"], True)
    assert lyst.monitors.list(viewer, "northwind")[0]["enabled"] is False


def test_data_quality_finding_on_the_ledger(seeded):
    lyst, owner = seeded
    from lyst.semantic import Measure, MetricSpec

    lyst.semantic.define(owner, "northwind", MetricSpec(
        name="ledger_revenue", title="Ledger revenue", dataset="pnl_ledger", measure=Measure(agg="sum", column="revenue"),
        time_column="month", dimensions=["region"], format="currency"))
    m = lyst.monitors.create(owner, "northwind", metric="ledger_revenue", grain="month", threshold_sigma=99)
    _approve(lyst, owner, m["approval_id"])
    # make Sep 2026 the period with the problem: null revenue is planted in Aug, so monitor the ledger as of Aug
    lyst.stores.workspace("northwind").execute('DELETE FROM data."pnl_ledger__v1" WHERE month >= DATE \'2026-09-01\'')
    found = lyst.monitors.tick("northwind")
    dq = [f for f in found if f["kind"] == "data_quality"]
    assert dq and "1 nulls in revenue" in dq[0]["summary"] and dq[0]["label"] == "Aug 2026"


def test_priority_prefers_large_adverse_moves():
    assert MonitorService._priority(-5, 0.30, 0.36, good=False) > MonitorService._priority(5, 0.42, 0.36, good=True)
    assert MonitorService._priority(-3, 100, 101, good=False) < MonitorService._priority(-6, 100, 150, good=False)
