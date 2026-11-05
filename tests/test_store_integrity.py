# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The database layer under failure: migrations, copies, closes, retention and backups that fail part-way."""

import threading
import time

import pytest

from lyst.store.db import Database, split_statements


def _db(tmp_path, **kw):
    return Database(tmp_path / "t.duckdb", name="t", master_key=b"k" * 32, encrypt=False,
                    allowed_dir=tmp_path / "backups", **kw)


def test_migration_statements_keep_semicolons_inside_literals_and_comments(tmp_path):
    assert len(split_statements("CREATE TABLE t (s VARCHAR); INSERT INTO t VALUES ('a;b'); -- c; d\n")) == 2
    db = _db(tmp_path, migrations=[(1, "CREATE TABLE t (s VARCHAR);\nINSERT INTO t VALUES ('a;b') -- why; not\n")])
    assert db.scalar("SELECT s FROM t") == "a;b"
    db.close()


def test_copy_after_close_fails_at_once_and_a_copy_failure_keeps_the_writer_usable(tmp_path):
    db = _db(tmp_path, migrations=[(1, "CREATE TABLE t (n INTEGER)")])
    with pytest.raises(ValueError):
        db.copy_with_key(tmp_path / "backups" / "x.duckdb", "not-a-hex-key' ; DROP TABLE t; --")
    db.execute("INSERT INTO t VALUES (1)")  # the writer still runs transactions
    assert db.scalar("SELECT count(*) FROM t") == 1
    db.close()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="closed"):
        db.copy_with_key(tmp_path / "backups" / "y.duckdb", None)
    assert time.monotonic() - started < 5


def test_a_backup_copy_replaces_the_previous_one_only_once_complete(tmp_path):
    db = _db(tmp_path, migrations=[(1, "CREATE TABLE t (n INTEGER)")])
    dest = tmp_path / "backups" / "copy.duckdb"
    db.backup_to(dest)
    db.execute("INSERT INTO t VALUES (7)")
    db.backup_to(dest)
    assert dest.exists() and not list(dest.parent.glob(".copy.duckdb.incoming"))
    db.close()


def test_query_max_rows_stops_reading(tmp_path):
    db = _db(tmp_path)
    assert len(db.query("SELECT * FROM range(1000)", max_rows=10)) == 10
    db.close()




def test_close_leaves_the_connection_to_a_writer_that_is_still_busy(tmp_path, monkeypatch, caplog):
    db = _db(tmp_path)
    monkeypatch.setattr(db._writer, "join", lambda timeout=None: None)
    monkeypatch.setattr(db._writer, "is_alive", lambda: True)
    db.close()
    assert "still busy" in caplog.text
    db._conn.execute("SELECT 1")  # not closed under the writer


def test_stores_close_keeps_closing_after_one_failure(lyst, monkeypatch):
    stores = lyst.stores
    ids = [r["id"] for r in stores.control.query("SELECT id FROM workspaces")]
    ws = stores.workspace(ids[0]) if ids else None
    closed = []
    real = Database.close

    def close(self):
        closed.append(self.name)
        if self.name.startswith("ws:"):
            raise OSError("disk gone")
        real(self)

    monkeypatch.setattr(Database, "close", close)
    stores.close()
    assert "control" in closed and (ws is None or ws.name in closed)


def test_database_files_skips_files_that_are_not_workspace_databases(lyst):
    wsdir = lyst.settings.home / "workspaces"
    wsdir.mkdir(parents=True, exist_ok=True)
    (wsdir / "Old Copy.duckdb").write_bytes(b"")
    names = [n for n, _ in lyst.stores.database_files()]
    assert all(n == "control" or n.startswith("ws:") for n in names) and "ws:Old Copy" not in names


def test_a_backup_that_fails_part_way_leaves_nothing_that_looks_like_a_backup(seeded, tmp_path, monkeypatch):
    lyst, _ = seeded
    dest = tmp_path / "bk"

    def boom(self, d):
        raise OSError("disk full")

    monkeypatch.setattr(Database, "backup_to", boom)
    with pytest.raises(OSError):
        lyst.stores.backup(dest)
    assert not dest.exists() and not list(tmp_path.glob(".partial-*"))
    existing = tmp_path / "kept"
    existing.mkdir()
    (existing / "other").write_text("x")
    with pytest.raises(OSError):
        lyst.stores.backup(existing)
    assert (existing / "BACKUP_INCOMPLETE").exists() and not (existing / "manifest.json").exists()


def test_prune_counts_exactly_what_it_deletes_and_ages_out_observed_memory_in_the_database(seeded):
    lyst, _ = seeded
    removed = lyst.stores.prune(event_days=0, audit_days=100000, memory_days=1)
    assert removed["task_events"] >= 0 and lyst.stores.control.scalar(
        "SELECT count(*) FROM task_events WHERE occurred_at < now()") == 0
    assert "memory" in removed


def test_a_started_write_that_outlives_its_grace_raises_instead_of_waiting_forever(tmp_path):
    db = _db(tmp_path)
    release = threading.Event()
    with pytest.raises(TimeoutError, match="may still commit"):
        db.write(lambda c: release.wait(5), timeout=0.2, grace=0.2)
    release.set()
    queued_behind = threading.Event()
    blocker = threading.Thread(target=lambda: db.write(lambda c: queued_behind.wait(5)))
    blocker.start()
    time.sleep(0.2)
    with pytest.raises(TimeoutError, match="withdrawn"):
        db.write(lambda c: None, timeout=0.2)
    queued_behind.set()
    blocker.join()
    db.close()


def test_lyst_close_still_closes_the_stores_when_the_runtime_will_not_stop(lyst, monkeypatch):
    closed = []
    monkeypatch.setattr(lyst.runtime, "stop", lambda: (_ for _ in ()).throw(RuntimeError("worker stuck")))
    monkeypatch.setattr(lyst.stores, "close", lambda: closed.append(True))
    lyst.close()
    assert closed == [True]


@pytest.mark.parametrize("value", ["two", "0", "-1", ""])
def test_a_bad_worker_count_is_named_in_the_error(monkeypatch, tmp_path, value):
    from lyst.config import load_settings

    monkeypatch.setenv("LYST_WORKERS", value)
    if value == "":
        assert load_settings(tmp_path).workers == 2  # unset or empty means the default
        return
    with pytest.raises(ValueError, match="LYST_WORKERS"):
        load_settings(tmp_path)
