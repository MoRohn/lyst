# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario 7, cross-tenant security, as a sweep over every API route rather than a hand-picked list.

An outsider with a valid token, and a member of another workspace, call every route that names a workspace or
an object of the victim's workspace. Nothing may succeed, nothing may answer with a server error, and the
victim's data must not appear in any response body.
"""

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.connectors import CONNECTORS

from .conftest import ask
from .test_gaps import Fake  # a connector that needs no server

PUBLIC = {"/health", "/setup/status", "/setup/initialize", "/metrics", "/auth/login", "/auth/callback", "/auth/logout",
          "/v1/me", "/v1/connectors", "/v1/capabilities", "/v1/credentials/providers", "/v1/credentials",
          "/v1/installation"}


@pytest.fixture
def two_tenants(seeded, monkeypatch):
    lyst, victim = seeded
    monkeypatch.setitem(CONNECTORS, "fake", Fake)
    Fake.accepted = {"tok-first-0001"}
    attacker = lyst.guard.create_user("mallory@acme.test", "Mallory")
    demo.seed(lyst, attacker, "acme")
    t = ask(lyst, victim, "What was revenue last quarter?")
    art = lyst.artifacts.build(victim, t, "md")
    conn = lyst.connections.create(victim, "northwind", kind="fake", name="Victim API",
                                   values={"url": "https://x", "token": "tok-first-0001"})
    sched = lyst.runtime.create_schedule(victim, "northwind", "Weekly", "What was revenue last month?", 7 * 86400)
    ids = {"ws": "northwind", "task_id": t["id"], "artifact_id": art["id"], "cid": conn["id"],
           "approval_id": sched["approval_id"], "name": "pnl", "kind": "md", "action": "cancel",
           "dashboard_id": "d_missing", "wf_id": "wf_missing", "monitor_id": "m_missing", "finding_id": "f_missing",
           "memory_id": "mem_missing", "version": "1", "filename": "manifest.json", "user_id": victim.user_id}
    return lyst, victim, attacker, ids


def _routes(app):
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path not in PUBLIC and "{" in r.path:
            for m in r.methods - {"HEAD", "OPTIONS"}:
                yield m, r.path


def test_no_route_lets_another_workspace_reach_the_victim(two_tenants):
    lyst, victim, attacker, ids = two_tenants
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(attacker)}"}
    secrets = ("tok-first-0001",)
    app = create_app(lyst, start_workers=False)
    leaks, errors, checked = [], [], 0
    with TestClient(app, raise_server_exceptions=False) as c:
        for method, path in _routes(app):
            url = re.sub(r"{(\w+)}", lambda m: ids.get(m.group(1), "x"), path)
            r = c.request(method, url, headers=h, json={})
            checked += 1
            if r.status_code < 400:
                leaks.append(f"{method} {url} -> {r.status_code}")
            elif r.status_code >= 500:
                errors.append(f"{method} {url} -> {r.status_code}")
            if any(s in r.text for s in secrets) or "Northwind Industries" in r.text:
                leaks.append(f"{method} {url} body names victim data")
    print(f"route sweep: {checked} route/method pairs")
    assert checked > 60
    assert leaks == [], leaks
    assert errors == [], errors
    # every refusal on a workspace route is in the audit log under the attacker
    denied = lyst.stores.control.scalar("SELECT count(*) FROM audit_log WHERE actor = ? AND outcome = 'denied'",
                                        [attacker.user_id])
    assert denied > 30


def test_objects_of_one_workspace_are_unreachable_through_the_attackers_own_workspace(two_tenants):
    """An object id from northwind, addressed under the attacker's own workspace acme, is not found."""
    lyst, victim, attacker, ids = two_tenants
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(attacker)}"}
    app = create_app(lyst, start_workers=False)
    with TestClient(app, raise_server_exceptions=False) as c:
        for method, path in [("POST", "/v1/workspaces/acme/connections/{cid}/test"),
                             ("GET", "/v1/workspaces/acme/connections/{cid}/tables"),
                             ("DELETE", "/v1/workspaces/acme/connections/{cid}")]:
            r = c.request(method, path.format(**ids), headers=h, json={})
            assert r.status_code == 404, (method, path, r.status_code, r.text)
        # the sync history is filtered by workspace, so the victim's connection id yields nothing
        assert c.get("/v1/workspaces/acme/connections/{cid}/syncs".format(**ids), headers=h).json() == []
    assert lyst.stores.control.scalar("SELECT count(*) FROM connections WHERE id = ?", [ids["cid"]]) == 1
