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
