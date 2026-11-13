# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import time

from fastapi.testclient import TestClient

from lyst.api import create_app


def test_ask_over_http_with_background_workers(seeded):
    lyst, owner = seeded
    token = lyst.guard.issue_token(owner)
    h = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(lyst, start_workers=True)) as c:
        assert c.get("/health").json()["status"] == "ok"
        me = c.get("/v1/me", headers=h).json()
        assert me["workspaces"][0]["id"] == "northwind", me
        r = c.post("/v1/workspaces/northwind/tasks", json={"question": "Why did operating margin decline last quarter?"},
                   headers=h)
        assert r.status_code == 202
        tid = r.json()["task_id"]
        for _ in range(100):
            t = c.get(f"/v1/tasks/{tid}", headers=h).json()
            if t["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert t["status"] == "done", t
        assert "EMEA" in t["result"]["evidence"][0]
        up = c.post("/v1/workspaces/northwind/datasets", headers=h, data={"name": "targets"},
                    files={"file": ("targets.csv", b"region,target\nEMEA,0.3\n", "text/csv")})
        assert up.status_code == 200 and up.json()["version"] == 1
        q = c.post("/v1/workspaces/northwind/query", headers=h, json={"sql": "select * from targets"}).json()
        assert q["rows"] == [["EMEA", 0.3]]
        assert any(cap["name"] == "driver_analysis" for cap in c.get("/v1/capabilities", headers=h).json())
        assert c.get("/v1/workspaces/northwind/audit", headers=h).status_code == 200


def test_settings_patch_and_model_usage_need_admin(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    ho = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    hv = {"Authorization": f"Bearer {lyst.guard.issue_token(viewer)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.patch("/v1/workspaces/northwind/settings", json={"allow_external_llm": True}, headers=hv).status_code == 403
        assert c.patch("/v1/workspaces/northwind/settings", json={"allow_external_llm": True}, headers=ho).status_code == 200
        assert c.get("/v1/workspaces/northwind/settings", headers=ho).json()["workspace"]["allow_external_llm"] is True
        assert c.get("/v1/workspaces/northwind/model-usage", headers=ho).json() == []
        assert c.get("/v1/workspaces/northwind/model-usage", headers=hv).status_code == 403
        assert c.patch("/v1/workspaces/northwind/settings", json={"autonomy": "reckless"}, headers=ho).status_code == 400


def test_a_query_duckdb_cannot_run_is_the_askers_to_fix_not_a_server_error(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post("/v1/workspaces/northwind/query", headers=h, json={"sql": 'SELECT nope FROM "demand"'})
        assert r.status_code == 400 and r.json()["error"] == "query_failed"
        detail = r.json()["detail"]
        assert 'Referenced column "nope" not found' in detail and "__v" not in detail and "LINE" not in detail
        r = c.post("/v1/workspaces/northwind/query", headers=h, json={"sql": "SELECT CAST('x' AS INTEGER) FROM demand"})
        assert r.status_code == 400 and "Could not convert" in r.json()["detail"]
