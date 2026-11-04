# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario G: unauthorized workspaces, sensitive data and restricted operations are refused
and audited."""

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.data.sqlguard import QueryRejected
from lyst.security import AccessDenied, detect_sensitive, redact
from lyst.semantic import Measure, MetricSpec

from .conftest import ask


def _denials(lyst, ws):
    return lyst.stores.control.scalar(
        "SELECT count(*) FROM audit_log WHERE workspace_id = ? AND outcome IN ('denied', 'rejected')", [ws])


def test_outsider_cannot_reach_another_workspace(seeded):
    lyst, owner = seeded
    mallory = lyst.guard.create_user("mallory@else.test", "Mallory")
    lyst.guard.create_workspace(mallory, "elsewhere", "Elsewhere")
    with pytest.raises(AccessDenied):
        lyst.data.catalog(mallory, "northwind")
    with pytest.raises(AccessDenied):
        lyst.data.query(mallory, "northwind", "select * from pnl")
    with pytest.raises(AccessDenied):
        lyst.runtime.submit(mallory, "northwind", "What was revenue last quarter?")
    assert _denials(lyst, "northwind") == 3
    # and her own workspace cannot see northwind's datasets by name
    with pytest.raises(QueryRejected, match="unknown dataset"):
        lyst.data.query(mallory, "elsewhere", "select * from pnl")


def test_viewer_cannot_mutate(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.data.ingest(viewer, "northwind", "x", b"a\n1\n", filename="x.csv")
    with pytest.raises(AccessDenied):
        lyst.semantic.define(viewer, "northwind", MetricSpec(
            name="rows", title="Rows", dataset="pnl", measure=Measure(agg="count")))
    with pytest.raises(AccessDenied):
        lyst.guard.add_member(viewer, "northwind", viewer.user_id, "owner")
    with pytest.raises(AccessDenied):
        lyst.runtime.create_schedule(viewer, "northwind", "x", "revenue", 3600)
    # but can ask
    assert ask(lyst, viewer, "What was revenue last quarter?")["status"] == "done"


def test_column_policy_hides_sensitive_columns_from_lower_roles(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    lyst.data.set_column_policy(owner, "northwind", "pnl", "opex", "confidential", "admin")
    with pytest.raises(QueryRejected, match="restricted"):
        lyst.data.query(viewer, "northwind", "select sum(opex) from pnl")
    with pytest.raises(QueryRejected, match="restricted"):
        lyst.data.query(viewer, "northwind", "select * from pnl")
    assert lyst.data.query(owner, "northwind", "select sum(opex) from pnl").rows[0][0] > 0


def test_unknown_actions_are_denied(seeded):
    lyst, owner = seeded
    assert not lyst.guard.can(owner, "northwind", "data.delete_everything")


def test_api_rejects_missing_and_bad_tokens_and_cross_workspace(seeded):
    lyst, owner = seeded
    token = lyst.guard.issue_token(owner)
    other = lyst.guard.create_user("o@x.test", "O")
    other_token = lyst.guard.issue_token(other)
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get("/v1/workspaces/northwind/datasets").status_code == 403
        assert c.get("/v1/workspaces/northwind/datasets", headers={"Authorization": "Bearer nope"}).status_code == 403
        assert c.get("/v1/workspaces/northwind/datasets",
                     headers={"Authorization": f"Bearer {other_token}"}).status_code == 403
        assert c.get("/v1/workspaces/northwind/datasets",
                     headers={"Authorization": f"Bearer {token}"}).status_code == 200
        r = c.post("/v1/workspaces/northwind/query", json={"sql": "drop table pnl"},
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400 and r.json()["error"] == "query_rejected"


def test_proposer_cannot_approve_own_capability(seeded):
    lyst, owner = seeded
    aid = lyst.runtime.request_approval("northwind", owner.user_id, "skill.register", "x", {})
    with pytest.raises(AccessDenied):
        lyst.runtime.decide(owner, aid, True)


def test_redaction():
    text = "Mail jane.doe@acme.com, SSN 123-45-6789, key sk-ABCDEFGHIJKLMNOPQRST"
    assert set(detect_sensitive(text)) >= {"email", "ssn", "secret"}
    out = redact(text)
    assert "jane.doe" not in out and "123-45-6789" not in out and "ABCDEFGHIJ" not in out


def test_audit_details_are_redacted(seeded):
    lyst, owner = seeded
    lyst.guard.audit(owner, "northwind", "test", None, "ok", {"note": "call 555-123-4567 or x@y.io"})
    detail = lyst.stores.control.scalar("SELECT detail FROM audit_log WHERE action = 'test'")
    assert "555-123-4567" not in detail and "x@y.io" not in detail
