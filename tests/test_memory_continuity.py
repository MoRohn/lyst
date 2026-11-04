# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Long-term memory that knows when it may no longer apply, and memory retention."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app

from .conftest import ask


def _validated_memory(lyst, owner):
    t = ask(lyst, owner, "What was revenue last quarter?")
    mid = lyst.stores.workspace("northwind").scalar(
        "SELECT id FROM memory WHERE provenance->>'task_id' = ?", [t["id"]])
    lyst.stores.workspace("northwind").execute("UPDATE memory SET trust = 'user_validated' WHERE id = ?", [mid])
    return t, mid


def test_a_memory_learned_from_older_data_is_flagged_and_ranked_after_current_ones(seeded):
    lyst, owner = seeded
    _t, mid = _validated_memory(lyst, owner)
    prov = lyst.services.memory.recall(owner, "northwind", "revenue last quarter")
    m = next(x for x in prov if x["id"] == mid)
    assert m["applicable"] and m["provenance"]["sources"][0] == {"dataset": "pnl", "version": 1}
    # new data arrives: the remembered answer was about version 1
    lyst.data.ingest(owner, "northwind", "pnl", demo.csv_bytes(demo.pnl().iloc[:-1]), filename="pnl.csv")
    m = next(x for x in lyst.services.memory.recall(owner, "northwind", "revenue last quarter") if x["id"] == mid)
    assert not m["applicable"] and "pnl v1; the data is now v2" in m["stale_reason"]
    review = lyst.services.memory.review(owner, "northwind")
    assert [r["id"] for r in review] == [mid]
    # and the next answer that draws on it says it may no longer apply
    r = ask(lyst, owner, "What was revenue last quarter?")["result"]
    assert any("may no longer apply: it was learned from pnl v1" in n for n in r["supporting"]["assumptions"])


def test_a_changed_metric_definition_or_age_marks_memory_stale(seeded):
    lyst, owner = seeded
    _t, mid = _validated_memory(lyst, owner)
    spec = lyst.semantic.get(owner, "northwind", "revenue")
    lyst.semantic.define(owner, "northwind", spec.model_copy(update={"description": "Booked revenue, net of returns."}),
                         note="clarified")
    m = next(x for x in lyst.services.memory.recall(owner, "northwind", "revenue last quarter") if x["id"] == mid)
    assert not m["applicable"] and "definition of revenue changed" in m["stale_reason"]
    db = lyst.stores.workspace("northwind")
    mid2 = lyst.services.memory.remember(owner, "northwind", "team", "Hardware revenue peaks in Q4.", provenance={"note": "x"})
    db.execute("UPDATE memory SET created_at = ? WHERE id = ?", [datetime.now(UTC) - timedelta(days=500), mid2])
    m2 = lyst.services.memory.applicability("northwind", db.query("SELECT * FROM memory WHERE id = ?", [mid2]))[mid2]
    assert not m2["applicable"] and "500 days old" in m2["stale_reason"]
    lyst.services.memory.promote(owner, "northwind", mid2)  # approved knowledge is not aged out
    assert lyst.services.memory.applicability("northwind", db.query("SELECT * FROM memory WHERE id = ?", [mid2]))[mid2]["applicable"]


def test_memory_retention_removes_only_old_agent_observations(seeded):
    lyst, owner = seeded
    db = lyst.stores.workspace("northwind")
    old = lyst.services.memory.remember(owner, "northwind", "episodic", "old observation", provenance={})
    kept = lyst.services.memory.remember(owner, "northwind", "episodic", "validated", provenance={}, trust="user_validated")
    db.execute("UPDATE memory SET created_at = ? WHERE id IN (?, ?)", [datetime.now(UTC) - timedelta(days=900), old, kept])
    removed = lyst.stores.prune(memory_days=730)
    assert removed["memory"] == 1
    assert db.scalar("SELECT count(*) FROM memory WHERE id = ?", [old]) == 0
    assert db.scalar("SELECT count(*) FROM memory WHERE id = ?", [kept]) == 1


def test_memory_review_endpoint_is_for_admins(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        ho = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
        hv = {"Authorization": f"Bearer {lyst.guard.issue_token(viewer)}"}
        assert c.get("/v1/workspaces/northwind/memory/review", headers=ho).status_code == 200
        assert c.get("/v1/workspaces/northwind/memory/review", headers=hv).status_code == 403


# ----------------------------------------------------------------------------- continuity and recovery


def test_backup_is_atomic_a_full_disk_leaves_no_half_backup_and_keeps_the_last_good_one(seeded, monkeypatch):
    from lyst import continuity

    lyst, owner = seeded
    good = continuity.take(lyst)
    real = lyst.stores.backup

    def disk_full(dest):
        real(dest)  # files land, then the disk fills while writing the rest
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lyst.stores, "backup", disk_full)
    import time

    time.sleep(1.1)  # a new stamp
    try:
        continuity.take(lyst)
        raise AssertionError("expected the backup to fail")
    except OSError:
        pass
    root = lyst.settings.home / "backups"
    assert not list(root.glob(".partial-*"))
    assert [b["path"] for b in continuity.backups(lyst.settings)] == [good["path"]]
    st = continuity.status(lyst.settings)
    assert "No space left" in st["last_error"] and st["last_success"] == good["taken_at"]


def test_restore_drill_verifies_and_reports_measured_objectives(seeded):
    from lyst import continuity

    lyst, owner = seeded
    b = continuity.take(lyst)
    d = continuity.drill(lyst.settings, b["path"])
    assert d["ok"] and d["tables"]["control.duckdb"] > 10 and d["tables"]["workspaces/northwind.duckdb"] > 10
    rep = continuity.report(lyst)
    assert rep["objectives"]["rpo_hours"] is not None and rep["objectives"]["rpo_met"]
    assert rep["objectives"]["rto_seconds"] == d["seconds"] and rep["objectives"]["rto_met"]
    # a damaged backup fails the drill with the reason
    f = next(p for p in (lyst.settings.home / "backups").rglob("northwind.duckdb"))
    data = bytearray(f.read_bytes())
    data[len(data) // 2] ^= 0xFF
    f.write_bytes(bytes(data))
    bad = continuity.drill(lyst.settings, b["path"])
    assert not bad["ok"] and "checksum" in bad["problems"][0]


def test_backups_never_contain_the_master_key(seeded):
    from lyst import continuity

    lyst, owner = seeded
    b = continuity.take(lyst)
    key = bytes.fromhex("ab" * 32)
    for f in __import__("pathlib").Path(b["path"]).rglob("*"):
        if f.is_file():
            blob = f.read_bytes()
            assert key not in blob and ("ab" * 32).encode() not in blob
            assert f.name not in continuity.KEY_FILES
    # a backup that someone put a key file into is refused by the drill
    (__import__("pathlib").Path(b["path"]) / ".lyst-key").write_text("ab" * 32)
    d = continuity.drill(lyst.settings, b["path"])
    assert not d["ok"] and "key material" in d["problems"][0]


def test_scheduled_backups_respect_the_interval_and_retention(seeded, monkeypatch):
    import time

    from lyst import continuity

    lyst, owner = seeded
    monkeypatch.setenv("LYST_BACKUP_EVERY_HOURS", "24")
    monkeypatch.setenv("LYST_BACKUP_KEEP", "2")
    first = continuity.scheduled(lyst)
    assert first["ok"] and first["drill"]["ok"]
    assert continuity.scheduled(lyst) is None  # within the interval
    from datetime import UTC, datetime, timedelta

    for _ in range(2):
        time.sleep(1.1)
        continuity.scheduled(lyst, now=datetime.now(UTC) + timedelta(hours=25))
        st = continuity.status(lyst.settings)
        continuity._save_status(lyst.settings, last_success=(datetime.now(UTC) - timedelta(hours=30)).isoformat())
        assert st["last_success"]
    assert len(continuity.backups(lyst.settings)) == 2  # the oldest was pruned


def test_a_failed_migration_leaves_the_database_as_it_was(tmp_path):
    from lyst.store.db import Database

    db = Database(tmp_path / "x.duckdb", name="x", master_key=b"k" * 32, encrypt=False)
    db.migrate([(1, "CREATE TABLE a (x INTEGER)")])
    try:
        db.migrate([(1, "CREATE TABLE a (x INTEGER)"), (2, "CREATE TABLE b (y INTEGER); CREATE TABLE a (z INTEGER)")])
        raise AssertionError("expected the migration to fail")
    except Exception as exc:  # noqa: BLE001
        assert "already exists" in str(exc).lower() or "catalog" in str(exc).lower()
    assert db.scalar("SELECT max(version) FROM _migrations") == 1
    assert db.scalar("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'b'") == 0  # no half-applied migration
    db.close()


def test_continuity_endpoints_are_for_installation_admins(seeded):
    lyst, owner = seeded
    other = lyst.guard.create_user("o@n.test", "O")
    lyst.guard.add_member(owner, "northwind", other.user_id, "admin")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        ho = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
        hx = {"Authorization": f"Bearer {lyst.guard.issue_token(other)}"}
        b = c.post("/v1/installation/backup", headers=ho).json()
        name = b["path"].rsplit("/", 1)[-1]
        assert c.post(f"/v1/installation/backups/{name}/drill", headers=ho).json()["ok"]
        assert c.get("/v1/installation/continuity", headers=ho).json()["objectives"]["rto_met"]
        assert c.get("/v1/installation/continuity", headers=hx).status_code == 403
        assert c.post("/v1/installation/backups/..%2F..%2Fetc/drill", headers=ho).status_code == 404
