# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Backup and restore, retention, tracing spans."""

import json

import duckdb
import pytest

from lyst import observability as obs
from lyst.store.db import derive_key

from .conftest import ask


def test_backup_is_consistent_encrypted_and_restorable(seeded, tmp_path, monkeypatch):
    lyst, owner = seeded
    t = ask(lyst, owner, "What was revenue last quarter?")
    lyst.artifacts.build(owner, t, "md")
    dest = tmp_path / "bk"
    m = lyst.stores.backup(dest)
    assert set(m["files"]) == {"control.duckdb", "workspaces/northwind.duckdb"} and (dest / "artifacts").exists()
    raw = (dest / "workspaces" / "northwind.duckdb").read_bytes()
    assert b"North America" not in raw  # still encrypted
    con = duckdb.connect()
    with pytest.raises(duckdb.Error):
        con.execute(f"ATTACH '{dest / 'workspaces' / 'northwind.duckdb'}' AS a (READ_ONLY)")
    con.execute(f"ATTACH '{dest / 'workspaces' / 'northwind.duckdb'}' AS a (ENCRYPTION_KEY '{derive_key(bytes.fromhex('ab' * 32), 'ws:northwind')}', READ_ONLY)")
    assert con.execute("SELECT count(*) FROM a.datasets").fetchall()[0][0] == 4
    assert con.execute("SELECT count(*) FROM a.analyses").fetchall()[0][0] == 1
    con.close()
    # the live database keeps working after the backup (the writer transaction was re-opened)
    assert ask(lyst, owner, "What was revenue last quarter?")["status"] == "done"
    # restore into a fresh home with the same master key, through the CLI
    from lyst.app import Lyst
    from lyst.cli import restore
    from lyst.config import load_settings

    home2 = tmp_path / "home2"
    monkeypatch.setenv("LYST_HOME", str(home2))
    restore(dest, yes=True)
    other = Lyst.build(load_settings(home2))
    try:
        assert other.stores.control.scalar("SELECT count(*) FROM workspaces") == 1
        user = other.guard.user_by_email("avery@northwind.test")
        r = other.data.query(user, "northwind", "select count(*) from pnl")
        assert r.rows == [[396]]
        assert other.artifacts.list(user, "northwind")
    finally:
        other.close()


def test_restore_refuses_a_backup_from_another_key(seeded, tmp_path, monkeypatch):
    lyst, owner = seeded
    dest = tmp_path / "bk"
    lyst.stores.backup(dest)
    from lyst.cli import restore

    monkeypatch.setenv("LYST_MASTER_KEY", "cd" * 32)
    monkeypatch.setenv("LYST_HOME", str(tmp_path / "home3"))
    with pytest.raises(SystemExit, match="does not open with the current master key"):
        restore(dest, yes=True)


def test_retention_prunes_old_rows_but_keeps_recent_and_audit(seeded):
    lyst, owner = seeded
    ask(lyst, owner, "What was revenue last quarter?")
    c = lyst.stores.control
    c.execute("UPDATE task_events SET occurred_at = now() - INTERVAL 200 DAY")
    c.execute("UPDATE audit_log SET occurred_at = now() - INTERVAL 200 DAY")
    removed = lyst.stores.prune(event_days=90, audit_days=400)
    assert removed["task_events"] > 0 and removed["audit_log"] == 0
    assert c.scalar("SELECT count(*) FROM task_events") == 0 and c.scalar("SELECT count(*) FROM audit_log") > 0
    ask(lyst, owner, "What was revenue last quarter?")
    assert lyst.stores.prune(event_days=90, audit_days=400)["task_events"] == 0  # fresh rows stay


def test_spans_are_emitted_for_tasks_steps_and_http(seeded):
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from lyst.api import create_app

    lyst, owner = seeded
    exporter = InMemorySpanExporter()
    obs.add_span_exporter(exporter)
    ask(lyst, owner, "What was revenue last quarter?")
    names = [s.name for s in exporter.get_finished_spans()]
    assert "task" in names and "step:understand" in names and "step:deliver" in names
    task_span = next(s for s in exporter.get_finished_spans() if s.name == "task")
    assert task_span.attributes["lyst.status"] == "done"
    step = next(s for s in exporter.get_finished_spans() if s.name == "step:understand")
    assert step.attributes["lyst.outcome"] == "done" and step.parent is not None
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        c.get("/v1/workspaces/northwind/datasets", headers=h)
    http = [s for s in exporter.get_finished_spans() if s.name == "http"]
    assert http and http[-1].attributes["http.route"] == "/v1/workspaces/{ws}/datasets" and http[-1].attributes["http.status_code"] == 200
    assert json.dumps(dict(http[-1].attributes))
