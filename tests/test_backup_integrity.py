# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Backup taken during an active workload, and the integrity checks restore must make (B1, B2)."""

import threading

import pytest

from .conftest import ask


def test_a_backup_taken_while_tasks_run_is_consistent_and_restorable(seeded, tmp_path, monkeypatch):
    lyst, owner = seeded
    stop = threading.Event()
    errors: list[BaseException] = []

    def load():
        while not stop.is_set():
            try:
                ask(lyst, owner, "Why did operating margin decline last quarter?")
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(exc)

    workers = [threading.Thread(target=load) for _ in range(3)]
    for w in workers:
        w.start()
    try:
        manifests = [lyst.stores.backup(tmp_path / f"bk{i}") for i in range(3)]
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=60)
    assert errors == [] and all(m["files"] for m in manifests)
    # the live installation kept every task consistent: no task is done without its steps
    c = lyst.stores.control
    assert c.scalar("SELECT count(*) FROM tasks t WHERE status = 'done' AND NOT EXISTS "
                    "(SELECT 1 FROM task_steps s WHERE s.task_id = t.id)") == 0
    from lyst.app import Lyst
    from lyst.cli import restore
    from lyst.config import load_settings

    monkeypatch.setenv("LYST_HOME", str(tmp_path / "restored"))
    restore(tmp_path / "bk2", yes=True)
    other = Lyst.build(load_settings(tmp_path / "restored"))
    try:
        oc = other.stores.control
        # a snapshot: every done task in the copy has all its steps done, and nothing is half-committed
        assert oc.scalar("SELECT count(*) FROM tasks t WHERE status = 'done' AND EXISTS "
                         "(SELECT 1 FROM task_steps s WHERE s.task_id = t.id AND s.status NOT IN ('done', 'skipped'))") == 0
        user = other.guard.user_by_email("avery@northwind.test")
        assert other.data.query(user, "northwind", "select count(*) from pnl").rows == [[396]]
        # the restored installation works
        assert ask(other, user, "What was revenue last quarter?")["status"] == "done"
    finally:
        other.close()


def test_restore_refuses_a_backup_whose_files_do_not_match_the_manifest(seeded, tmp_path, monkeypatch):
    """B1: the manifest recorded SHA-256 per file but restore never checked it, so a truncated or altered file
    was copied over a working installation."""
    lyst, owner = seeded
    dest = tmp_path / "bk"
    lyst.stores.backup(dest)
    f = dest / "workspaces" / "northwind.duckdb"
    data = bytearray(f.read_bytes())
    data[len(data) // 2] ^= 0xFF  # one flipped byte, as a bad disk or a tampered copy would leave
    f.write_bytes(bytes(data))
    from lyst.cli import restore

    monkeypatch.setenv("LYST_HOME", str(tmp_path / "home4"))
    with pytest.raises(SystemExit, match="checksum"):
        restore(dest, yes=True)
    assert not (tmp_path / "home4" / "control.duckdb").exists()  # nothing was copied


def test_restore_refuses_to_replace_files_an_engine_has_open(seeded, tmp_path, monkeypatch):
    """B2: restore deleted and replaced the database files of a running engine."""
    lyst, owner = seeded
    dest = tmp_path / "bk"
    lyst.stores.backup(dest)
    from lyst.cli import restore

    monkeypatch.setenv("LYST_HOME", str(lyst.settings.home))  # the live installation, still open
    with pytest.raises(SystemExit, match="in use"):
        restore(dest, yes=True)
    assert ask(lyst, owner, "What was revenue last quarter?")["status"] == "done"


def test_tracing_started_from_many_threads_at_once_still_exports(tmp_path):
    """O2: concurrent first calls to tracer() each built a provider; the module kept one that was not OpenTelemetry's
    global provider, so exporters attached afterwards (and the OTLP exporter) received no spans at all."""
    import subprocess
    import sys

    script = (
        "import threading\n"
        "from lyst import observability as obs\n"
        "from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter\n"
        "b = threading.Barrier(16)\n"
        "def go():\n"
        "    b.wait(); obs.tracer()\n"
        "ts = [threading.Thread(target=go) for _ in range(16)]\n"
        "[t.start() for t in ts]; [t.join() for t in ts]\n"
        "e = InMemorySpanExporter(); obs.add_span_exporter(e)\n"
        "with obs.tracer().start_as_current_span('probe'): pass\n"
        "print(len(e.get_finished_spans()))\n")
    results = [subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60).stdout.strip()
               for _ in range(5)]
    assert results == ["1"] * 5, results


def test_concurrent_jobs_across_workspaces_stay_in_their_workspace(lyst):
    """Scenario 2: three workspaces, each with its own data (revenue scaled 1x, 2x, 3x), asked at the same time on
    four workers. Every answer must come from its own workspace's data, and every analysis row must land in its
    own workspace database."""
    import pandas as pd

    from lyst import demo
    from lyst.runtime import TaskRuntime

    owners = {}
    for i, ws in enumerate(("alpha", "beta", "gamma"), start=1):
        o = lyst.guard.create_user(f"o{i}@{ws}.test", ws)
        demo.seed(lyst, o, ws)
        frame = demo.pnl()
        frame["revenue"] = frame["revenue"] * i
        lyst.data.ingest(o, ws, "pnl", demo.csv_bytes(frame), filename="pnl.csv")
        owners[ws] = (o, i)
    base = pd.to_datetime(demo.pnl()["month"])
    q3 = demo.pnl()[(base >= "2026-07-01") & (base < "2026-10-01")].revenue.sum()
    ids = {ws: [lyst.runtime.submit(o, ws, "What was revenue last quarter?") for _ in range(4)] for ws, (o, _) in owners.items()}
    workers = [TaskRuntime(lyst.stores, lyst.services, worker_id=f"w{i}") for i in range(4)]
    threads = [threading.Thread(target=w.drain) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    for ws, (o, scale) in owners.items():
        for tid in ids[ws]:
            t = lyst.runtime.get(o, tid)
            assert t["status"] == "done" and t["workspace_id"] == ws
            expected = q3 * scale
            assert f"${expected / 1e6:.2f}M" in t["result"]["answer"], (ws, t["result"]["answer"], expected)
        assert lyst.stores.workspace(ws).scalar("SELECT count(*) FROM analyses") >= 4
        # another workspace's owner cannot read these tasks
        other = next(x for w, (x, _) in owners.items() if w != ws)
        with pytest.raises(Exception):  # noqa: B017 - AccessDenied
            lyst.runtime.get(other, ids[ws][0])


def test_the_engine_takes_an_online_backup_for_installation_administrators_only(seeded):
    """B3: `lyst backup` could not run while the engine held the files (a lock traceback); the engine now takes it."""
    from fastapi.testclient import TestClient

    from lyst.api import create_app

    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        denied = c.post("/v1/installation/backup", headers={"Authorization": f"Bearer {lyst.guard.issue_token(viewer)}"})
        assert denied.status_code == 403
        lyst.stores.control.execute("INSERT INTO installation_admins (user_id, added_by, added_at) VALUES (?, 'test', now())",
                                    [owner.user_id])
        ok = c.post("/v1/installation/backup", headers={"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["path"].startswith(str(lyst.settings.home / "backups")) and "control.duckdb" in body["files"]
