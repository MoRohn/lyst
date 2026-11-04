# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The asset registry: reports, workbooks, decks, dashboards and workflows as versioned assets with dependencies,
health, repair and history. Acceptance scenario F (artifact dependency change) runs here end to end, and scenario C
(automated reporting: build, refresh, verify, inspect history) for a dashboard."""

from pathlib import Path

from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.contracts import ContractSpec

from .conftest import ask

WS = "northwind"
Q = "What was operating margin last quarter?"


def _report(lyst, owner, kind="pdf"):
    task = ask(lyst, owner, Q)
    assert task["status"] == "done"
    return task, lyst.artifacts.build(owner, task, kind)


def test_every_deliverable_is_a_registered_asset_with_dependencies(seeded):
    lyst, owner = seeded
    task, art = _report(lyst, owner, "pdf")
    lyst.artifacts.build(owner, task, "xlsx")
    assets = {a["kind"]: a for a in lyst.assets.list(owner, WS)}
    assert {"report", "workbook", "workflow"} <= set(assets)
    rep = assets["report"]
    assert rep["id"] == art["asset_id"]
    assert rep["owner"] == owner.user_id and rep["version"] == 1 and rep["health"] == "current"
    assert rep["validation"] == "verified" and rep["question"] == Q
    assert rep["depends"]["datasets"] == {"pnl": 1} and rep["depends"]["metrics"] == {"operating_margin": 1}
    wf = assets["workflow"]
    assert wf["depends"]["metrics"] == {"operating_margin": 1} and "pnl" in wf["depends"]["datasets"]
    detail = lyst.assets.get(owner, WS, rep["id"])
    assert detail["versions"][0]["sha256"] and detail["proposal"]["text"] == "Up to date."


def test_f_upstream_changes_mark_assets_stale_invalidated_or_broken_and_notify_owners(seeded):
    lyst, owner = seeded
    _, art = _report(lyst, owner)
    aid = art["asset_id"]
    # new data: stale, no notice (noise stays low)
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(demo.pnl(seed=8)), filename="pnl.csv")
    a = lyst.assets.get(owner, WS, aid)
    assert a["health"] == "stale" and "pnl is now v2" in a["health_reason"] and a["proposal"]["action"] == "rebuild"
    assert not [n for n in lyst.monitors.inbox(owner, WS) if n["kind"].startswith("asset_")]
    # a redefined metric: invalidated, with a notice for the owner
    spec = lyst.semantic.get(owner, WS, "operating_margin")
    lyst.semantic.define(owner, WS, spec.model_copy(update={"description": "Operating income over revenue, finance v2."}))
    a = lyst.assets.get(owner, WS, aid)
    assert a["health"] == "invalidated" and "operating_margin changed (v1 → v2)" in a["health_reason"]
    assert any(n["kind"] == "asset_invalidated" for n in lyst.monitors.inbox(owner, WS))
    # a breaking upstream version: broken, and the proposal asks a person to fix the cause first
    lyst.contracts.define(owner, WS, "pnl", ContractSpec.model_validate(lyst.contracts.draft(owner, WS, "pnl")))
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(demo.pnl().drop(columns=["opex"])), filename="pnl.csv")
    a = lyst.assets.get(owner, WS, aid)
    assert a["health"] == "broken" and "contract" in a["health_reason"] and not a["proposal"]["automatic"]
    assert lyst.assets.affected_by(owner, WS, dataset="pnl") and lyst.assets.affected_by(owner, WS, metric="operating_margin")


def test_a_stale_report_is_rebuilt_verified_and_its_previous_version_kept(seeded):
    lyst, owner = seeded
    _, art = _report(lyst, owner)
    aid = art["asset_id"]
    first_path = Path(art["path"])
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(demo.pnl(seed=8)), filename="pnl.csv")
    out = lyst.assets.repair(owner, WS, aid)
    assert out["status"] == "repairing" and out["task_id"]
    assert lyst.assets.get(owner, WS, aid)["health"] == "repairing"
    lyst.runtime.drain()
    a = lyst.assets.get(owner, WS, aid)
    assert a["health"] == "current" and a["version"] == 2 and a["depends"]["datasets"] == {"pnl": 2}
    assert [v["version"] for v in a["versions"]] == [2, 1] and a["versions"][0]["note"] == "rebuilt"
    assert first_path.exists()  # version 1's file is untouched
    assert a["versions"][0]["ref_id"] != a["versions"][1]["ref_id"]


def test_c_dashboard_refresh_is_a_new_verified_version_and_keeps_the_old_folder(seeded):
    lyst, owner = seeded
    ask(lyst, owner, "Build a revenue dashboard by region in Streamlit")
    dash = lyst.assets.list(owner, WS, kind="dashboard")[0]
    assert dash["validation"] == "verified" and dash["depends"]["metrics"] == {"revenue": 1}
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(demo.pnl(seed=8)), filename="pnl.csv")
    assert lyst.assets.get(owner, WS, dash["id"])["proposal"]["action"] == "refresh"
    out = lyst.assets.repair(owner, WS, dash["id"])
    assert out["status"] == "published" and out["version"] == 2
    a = lyst.assets.get(owner, WS, dash["id"])
    assert a["health"] == "current" and a["depends"]["datasets"] == {"pnl": 2}
    folder = Path(lyst.stores.workspace(WS).scalar("SELECT path FROM dashboards WHERE id = ?", [dash["ref_id"]]))
    assert (folder.parent / f"{folder.name}.versions" / "v1" / "manifest.json").exists()
    lyst.assets.retire(owner, WS, dash["id"])
    assert not lyst.assets.list(owner, WS, kind="dashboard")
    assert lyst.studio.list(owner, WS) == []


def test_repairs_need_an_editor_and_private_assets_stay_private(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    _, art = _report(lyst, owner)
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(demo.pnl(seed=8)), filename="pnl.csv")
    import pytest

    from lyst.security import AccessDenied
    with pytest.raises(AccessDenied):
        lyst.assets.repair(viewer, WS, art["asset_id"])
    lyst.stores.workspace(WS).execute("UPDATE assets SET visibility = 'private' WHERE id = ?", [art["asset_id"]])
    assert art["asset_id"] not in {a["id"] for a in lyst.assets.list(viewer, WS)}
    assert art["asset_id"] in {a["id"] for a in lyst.assets.list(owner, WS)}


def test_asset_routes(seeded):
    lyst, owner = seeded
    _, art = _report(lyst, owner)
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        listed = c.get(f"/v1/workspaces/{WS}/assets", params={"kind": "report"}, headers=h).json()
        assert [a["id"] for a in listed] == [art["asset_id"]]
        lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(demo.pnl(seed=8)), filename="pnl.csv")
        assert c.post(f"/v1/workspaces/{WS}/assets/scan", headers=h).status_code == 200
        assert c.get(f"/v1/workspaces/{WS}/assets", params={"health": "stale"}, headers=h).json()
        assert c.get(f"/v1/workspaces/{WS}/assets/{art['asset_id']}/proposal", headers=h).json()["action"] == "rebuild"
        assert c.post(f"/v1/workspaces/{WS}/assets/{art['asset_id']}/repair", headers=h).json()["status"] == "repairing"
        assert c.get(f"/v1/workspaces/{WS}/assets/as_nope", headers=h).status_code == 404
        assert c.post(f"/v1/workspaces/{WS}/assets/{art['asset_id']}/retire", headers=h).json()["status"] == "retired"
