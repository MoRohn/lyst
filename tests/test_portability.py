# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workspace packs: sealed export, verified import into a new workspace, safe defaults for copies."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.portability import PackError
from lyst.security import AccessDenied

from .conftest import ask

PASS = "correct horse battery staple"


def _pack(lyst, owner, **kw):
    out = lyst.portability.export(owner, "northwind", passphrase=PASS, **kw)
    return out, lyst.portability.export_path(owner, "northwind", out["id"]).read_bytes()


def test_round_trip_into_a_new_workspace(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    t = ask(lyst, owner, "What was operating margin last quarter?")
    lyst.artifacts.build(owner, t, "md")
    lyst.runtime.schedule(owner, "northwind", "Weekly margin", "What was operating margin last quarter?", 7 * 86400) \
        if hasattr(lyst.runtime, "schedule") else None
    out, blob = _pack(lyst, owner, include_history=True)
    assert out["encrypted"] and blob[:8] == b"LYSTPK1\n" and out["files"] >= 1
    assert b"pnl" not in blob  # sealed: no table names or values in the clear
    info = lyst.portability.inspect_pack(owner, blob, passphrase=PASS)
    assert info["intact"] and info["compatibility"]["compatible"] and info["workspace_exists"]
    assert [m["email"] for m in info["members"]["mapped"]] == ["sam@northwind.test"]
    with pytest.raises(PackError, match="already exists"):
        lyst.portability.import_pack(owner, blob, passphrase=PASS)
    rep = lyst.portability.import_pack(owner, blob, passphrase=PASS, workspace_id="northwind_copy", name="Northwind (copy)")
    assert rep["workspace"] == "northwind_copy" and rep["rows"] > 0 and rep["paused"] == ["schedules", "monitors"]
    assert rep["members"]["added"] == ["sam@northwind.test"]
    a = lyst.data.query(owner, "northwind", "SELECT count(*), sum(revenue) FROM pnl").rows
    b = lyst.data.query(owner, "northwind_copy", "SELECT count(*), sum(revenue) FROM pnl").rows
    assert a == b
    assert [m.name for m in lyst.semantic.metrics(owner, "northwind_copy")] == [m.name for m in lyst.semantic.metrics(owner, "northwind")]
    assert lyst.guard.role(viewer, "northwind_copy") == "viewer"
    # the copy's monitors are paused and its history is there, but nothing is left mid-run
    assert lyst.stores.workspace("northwind_copy").scalar("SELECT count(*) FROM monitors WHERE enabled") == 0
    assert lyst.stores.control.scalar("SELECT count(*) FROM tasks WHERE workspace_id = 'northwind_copy' AND status IN "
                                      "('queued', 'running', 'pending')") == 0
    # the copy works: ask it a question
    t2 = ask(lyst, owner, "What was operating margin last quarter?", ws="northwind_copy")
    assert t2["status"] == "done"
    assert (lyst.settings.home / "artifacts" / "northwind_copy").is_dir()


def test_tampering_wrong_passphrase_and_truncation_are_refused(seeded):
    lyst, owner = seeded
    _, blob = _pack(lyst, owner)
    with pytest.raises(PackError, match="passphrase"):
        lyst.portability.inspect_pack(owner, blob, passphrase="not the passphrase")
    with pytest.raises(PackError, match="encrypted"):
        lyst.portability.inspect_pack(owner, blob, passphrase=None)
    flipped = bytearray(blob)
    flipped[len(blob) // 2] ^= 0x01
    with pytest.raises(PackError, match="altered"):
        lyst.portability.import_pack(owner, bytes(flipped), passphrase=PASS, workspace_id="evil")
    with pytest.raises(PackError, match="truncated|altered"):
        lyst.portability.import_pack(owner, blob[: len(blob) - 100], passphrase=PASS, workspace_id="short")
    with pytest.raises(PackError, match="not a Lyst"):
        lyst.portability.inspect_pack(owner, b"hello world", passphrase=PASS)
    assert not lyst.portability._exists("evil") and not lyst.portability._exists("short")  # noqa: SLF001


def test_newer_packs_and_policy_refusals(seeded, monkeypatch):
    lyst, owner = seeded
    import lyst.portability as port

    out, blob = _pack(lyst, owner)
    monkeypatch.setattr(port, "WORKSPACE", [v for v in port.WORKSPACE if v[0] != 40])  # this installation is "older"
    info = lyst.portability.inspect_pack(owner, blob, passphrase=PASS)
    assert not info["compatibility"]["compatible"] and info["compatibility"]["pack_newer_workspace_migrations"] == [40]
    with pytest.raises(PackError, match="newer Lyst"):
        lyst.portability.import_pack(owner, blob, passphrase=PASS, workspace_id="older")
    monkeypatch.undo()
    with pytest.raises(ValueError, match="passphrase"):
        lyst.portability.export(owner, "northwind", passphrase="short")

    def deny(actor, ws, scope):
        raise AccessDenied("restricted data may not leave this workspace")

    lyst.portability.export_checks.append(deny)
    with pytest.raises(AccessDenied, match="restricted"):
        lyst.portability.export(owner, "northwind", passphrase=PASS)
    lyst.portability.export_checks.clear()

    class Gov:
        def evaluate(self, actor, ws, *, datasets, purpose):
            assert purpose == "export" and "pnl" in datasets
            return {"allowed": False, "reason": "above the workspace export ceiling"}

    lyst.portability.governance = Gov()
    with pytest.raises(AccessDenied, match="ceiling"):
        lyst.portability.export(owner, "northwind", passphrase=PASS)


def test_the_workspace_export_ceiling_applies(seeded):
    lyst, owner = seeded
    assert lyst.portability.governance is lyst.governance
    lyst.governance.set_policy(owner, "northwind", {"export_max": "internal"})  # the demo P&L is confidential
    with pytest.raises(AccessDenied, match="cannot be exported"):
        lyst.portability.export(owner, "northwind", passphrase=PASS)
    lyst.governance.set_policy(owner, "northwind", {"export_max": "confidential"})
    assert lyst.portability.export(owner, "northwind", passphrase=PASS)["encrypted"]


def test_permissions_and_unencrypted_packs(seeded):
    lyst, owner = seeded
    lyst.stores.control.execute(
        "INSERT INTO connections (id, workspace_id, kind, name, config, secrets, status, created_by) VALUES "
        "('c_inline', 'northwind', 'rest', 'Inline', '{}', '{\"token\": \"sk-live-very-secret-value-123\"}', 'ok', 'u')")
    admin = lyst.guard.create_user("ada@northwind.test", "Ada")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    with pytest.raises(AccessDenied, match="owner"):
        lyst.portability.export(admin, "northwind", passphrase=None, encrypt=False)
    out = lyst.portability.export(owner, "northwind", passphrase=None, encrypt=False)
    blob = lyst.portability.export_path(owner, "northwind", out["id"]).read_bytes()
    assert blob[:4] == b"PK\x03\x04"
    import io
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(blob))
    assert all(b"very-secret" not in z.read(n) for n in z.namelist())
    import pyarrow.parquet as pq

    conns = pq.read_table(io.BytesIO(z.read("control/connections.parquet"))).to_pylist()
    assert conns and all(c["secrets"] in ("{}", {}) for c in conns)
    with pytest.raises(AccessDenied, match="installation administrators"):
        lyst.portability.import_pack(admin, blob, passphrase=None, workspace_id="x_copy")
    rep = lyst.portability.import_pack(owner, blob, passphrase=None, workspace_id="plain_copy", activate=True)
    assert rep["paused"] == [] and not rep["encrypted"]


def test_export_import_api(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post("/v1/workspaces/northwind/exports", headers=h, json={"passphrase": PASS})
        assert r.status_code == 201, r.text
        dl = c.get(r.json()["download"], headers=h)
        assert dl.status_code == 200 and dl.content[:8] == b"LYSTPK1\n"
        assert c.get("/v1/workspaces/northwind/exports", headers=h).json()[0]["id"] == r.json()["id"]
        files = {"file": ("nw.lystpack", dl.content)}
        info = c.post("/v1/imports/inspect", headers=h, files=files, data={"passphrase": PASS}).json()
        assert info["intact"] is True
        r2 = c.post("/v1/imports", headers=h, files=files, data={"passphrase": PASS, "workspace_id": "nw_api"})
        assert r2.status_code == 201, r2.text
        assert c.post("/v1/imports", headers=h, files=files, data={"passphrase": "wrong one entirely"}).status_code == 400
        assert c.delete(f"/v1/workspaces/northwind/exports/{r.json()['id']}", headers=h).json() == {"deleted": True}
