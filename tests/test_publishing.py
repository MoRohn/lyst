# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Publishing: snapshots for members or by link, classification-gated, versioned, revocable, counted."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.publishing import markdown_to_html
from lyst.security import AccessDenied

from .conftest import ask


def _internal(lyst):
    # the demo P&L is confidential, which a link may never carry; these tests publish an internal copy of the case
    lyst.stores.workspace("northwind").execute("UPDATE datasets SET classification = 'internal' WHERE name = 'pnl'")


def test_members_and_link_publications(seeded):
    lyst, owner = seeded
    _internal(lyst)
    t = ask(lyst, owner, "What was operating margin last quarter?")
    pub = lyst.publishing.publish(owner, "northwind", t["id"], audience="link", expires_days=7)
    assert pub["url"].startswith("/p/") and pub["versions"][0]["datasets"] == ["pnl"]
    token = pub["url"][3:]
    page = lyst.publishing.by_link(token)
    assert "operating margin" in page.lower() and "<script" not in page
    assert lyst.publishing.get(owner, "northwind", pub["id"])["views"] == 1
    # the stored record never holds the link itself
    assert token not in str(lyst.stores.workspace("northwind").query("SELECT * FROM publications"))
    t2 = ask(lyst, owner, "What was revenue last quarter?")
    again = lyst.publishing.republish(owner, "northwind", pub["id"], t2["id"], note="refreshed")
    assert again["current_version"] == 2 and "revenue" in lyst.publishing.by_link(token).lower()
    assert "operating margin" in lyst.publishing.content(owner, "northwind", pub["id"], version=1).lower()
    lyst.publishing.revoke(owner, "northwind", pub["id"])
    assert lyst.publishing.by_link(token) is None
    with pytest.raises(KeyError):
        lyst.publishing.content(owner, "northwind", pub["id"])


def test_confidential_data_never_goes_out_by_link(seeded):
    lyst, owner = seeded  # the demo P&L is classified confidential
    t = ask(lyst, owner, "What was operating margin last quarter?")
    with pytest.raises(AccessDenied, match="confidential"):
        lyst.publishing.publish(owner, "northwind", t["id"], audience="link")
    assert lyst.publishing.publish(owner, "northwind", t["id"], audience="members")["audience"] == "members"

    class Gov:
        def check_export(self, actor, ws, task_id):
            raise AccessDenied(f"publish is closed for {task_id}")

    lyst.stores.workspace("northwind").execute("UPDATE datasets SET classification = 'internal' WHERE name = 'pnl'")
    real = lyst.publishing.governance
    lyst.publishing.governance = Gov()
    with pytest.raises(AccessDenied, match="publish is closed"):
        lyst.publishing.publish(owner, "northwind", t["id"], audience="link")
    lyst.publishing.governance = real
    assert real is not None  # the installation's governance service is bound
    lyst.governance.set_policy(owner, "northwind", {"export_max": "public"})
    with pytest.raises(AccessDenied, match="does not allow"):
        lyst.publishing.publish(owner, "northwind", t["id"], audience="link")


def test_roles(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    t = ask(lyst, owner, "What was operating margin last quarter?")
    with pytest.raises(AccessDenied):
        lyst.publishing.publish(viewer, "northwind", t["id"])
    pub = lyst.publishing.publish(owner, "northwind", t["id"], min_role="admin")
    with pytest.raises(AccessDenied, match="admins"):
        lyst.publishing.content(viewer, "northwind", pub["id"])
    open_pub = lyst.publishing.publish(owner, "northwind", t["id"])
    assert "<html" in lyst.publishing.content(viewer, "northwind", open_pub["id"])
    running = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?")
    with pytest.raises(ValueError, match="finished"):
        lyst.publishing.publish(owner, "northwind", running)


def test_markdown_rendering_escapes():
    out = markdown_to_html("# A <b>\n\n- **x** <img src=x onerror=alert(1)>\n\n| a | b |\n|---|---|\n| 1 | <i> |\n\n```sql\nSELECT '<x>'\n```")
    assert "<b>" not in out and "&lt;img" in out and "<table>" in out and "&lt;x&gt;" in out and "<strong>x</strong>" in out


def test_publication_api_and_public_link_headers(seeded):
    lyst, owner = seeded
    _internal(lyst)
    t = ask(lyst, owner, "What was operating margin last quarter?")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post("/v1/workspaces/northwind/publications", headers=h, json={"task_id": t["id"], "audience": "link"})
        assert r.status_code == 201, r.text
        url = r.json()["url"]
        page = c.get(url)
        assert page.status_code == 200 and "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-robots-tag"].startswith("noindex") and page.headers["referrer-policy"] == "no-referrer"
        assert c.get("/p/not-a-real-token").status_code == 404
        pid = r.json()["id"]
        assert c.get(f"/v1/workspaces/northwind/publications/{pid}/content", headers=h).status_code == 200
        assert c.post(f"/v1/workspaces/northwind/publications/{pid}/revoke", headers=h).json()["status"] == "revoked"
        assert c.get(url).status_code == 404
