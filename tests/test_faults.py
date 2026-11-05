# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Fault injection across this lane: a failure part-way leaves nothing half-done and says what happened."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from lyst.portability import PackError

PASS = "correct horse battery staple"


def test_one_table_failing_does_not_lose_the_others(seeded, monkeypatch):
    lyst, owner = seeded
    content = b"a,b\n1,2\n3,4\n\n\nc,d\n5,6\n7,8\n"
    plan = lyst.ingest.inspect(owner, "northwind", content, filename="two.csv")
    assert len(plan["tables"]) == 2
    real = lyst.data.ingest_arrow
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("disk said no")
        return real(*a, **kw)

    monkeypatch.setattr(lyst.ingest.data, "ingest_arrow", flaky)
    out = lyst.ingest.commit(owner, "northwind", plan["id"], tables=[{"key": t["key"], "include": True} for t in plan["tables"]])
    assert len(out["datasets"]) == 1 and out["errors"][0]["error"] == "disk said no"
    assert lyst.ingest.get(owner, "northwind", plan["id"])["status"] == "committed"


def test_ingest_never_loads_anything_when_every_table_fails(seeded, monkeypatch):
    lyst, owner = seeded
    plan = lyst.ingest.inspect(owner, "northwind", b"a,b\n1,2\n", filename="x.csv")
    monkeypatch.setattr(lyst.ingest.data, "ingest_arrow", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(ValueError, match="no table could be loaded"):
        lyst.ingest.commit(owner, "northwind", plan["id"])
    assert lyst.ingest.get(owner, "northwind", plan["id"])["status"] == "staged"  # still there to retry


def test_export_that_fails_leaves_no_pack_or_temp_files(seeded, monkeypatch):
    lyst, owner = seeded
    import lyst.portability as port

    monkeypatch.setattr(port, "seal", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        lyst.portability.export(owner, "northwind", passphrase=PASS)
    folder = lyst.settings.home / "exports"
    assert not list(folder.glob("northwind/*.lystpack")) and not any((folder / ".tmp").iterdir())


def test_import_that_fails_part_way_is_rolled_back(seeded, monkeypatch):
    lyst, owner = seeded
    out = lyst.portability.export(owner, "northwind", passphrase=PASS)
    blob = lyst.portability.export_path(owner, "northwind", out["id"]).read_bytes()
    real = lyst.portability._load  # noqa: SLF001

    def half(actor, z, manifest, ws, **kw):
        real(actor, z, manifest, ws, **kw)
        raise RuntimeError("power cut")

    monkeypatch.setattr(lyst.portability, "_load", half)
    with pytest.raises(RuntimeError):
        lyst.portability.import_pack(owner, blob, passphrase=PASS, workspace_id="half_done")
    assert not lyst.portability._exists("half_done")  # noqa: SLF001
    assert not lyst.stores.settings.workspace_db("half_done").exists()
    assert lyst.stores.control.scalar("SELECT count(*) FROM memberships WHERE workspace_id = 'half_done'") == 0
    monkeypatch.undo()
    assert lyst.portability.import_pack(owner, blob, passphrase=PASS, workspace_id="half_done")["rows"] > 0  # the id is free again


def test_webhook_transport_errors_are_recorded_and_retried(seeded, monkeypatch):
    monkeypatch.setenv("LYST_WEBHOOK_ALLOW_PRIVATE", "1")
    lyst, owner = seeded

    def down(request):
        raise httpx.ConnectError("connection refused")

    lyst.webhooks.client = httpx.Client(transport=httpx.MockTransport(down))
    sub = lyst.webhooks.create(owner, "northwind", "https://hooks.example.test/", types_=["dataset.*"])
    lyst.data.ingest(owner, "northwind", "t1", b"a\n1\n", filename="t1.csv")
    assert lyst.webhooks.run()["failed"] == 1
    d = lyst.webhooks.deliveries(owner, "northwind", sub["id"])[0]
    assert d["status"] == "pending" and d["last_status"] is None and "ConnectError" in d["last_error"]


def test_a_failing_event_subscriber_does_not_stop_the_others(seeded):
    lyst, owner = seeded
    lyst.events.collect()  # the seed's own history first
    got = []
    lyst.events.subscribers += [lambda e: 1 / 0, got.append]
    lyst.data.ingest(owner, "northwind", "t2", b"a\n1\n", filename="t2.csv")
    lyst.events.collect()
    assert [e["type"] for e in got] == ["dataset.version_created"]


def test_sdk_retries_reads_through_a_dropped_connection(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
    from lyst_client import Lyst

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ReadError("reset by peer")
        return httpx.Response(200, json={"status": "ok"})

    c = Lyst("http://x", http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)))
    c.retries = 3
    import lyst_client.client as cc

    monkeypatch.setattr(cc.time, "sleep", lambda s: None)  # no real waiting in the test
    assert c.health() == {"status": "ok"} and len(calls) == 3


def test_workbench_output_floods_are_cut(seeded):
    from lyst import sandbox

    if sandbox.backend()[0] is None:
        pytest.skip("no OS sandbox on this host")
    lyst, owner = seeded
    r = lyst.workbench.python(owner, "northwind", "for i in range(200000):\n    print('x' * 50)\nresult = 1")
    assert r["status"] == "ok" and len(r["stdout"]) <= 20_000


def test_api_contract_check_runs_clean_from_the_command_line():
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, str(root / "scripts" / "api_contract.py"), "--check"], capture_output=True, text=True,
                         check=False, cwd=root)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "BREAKING" not in out.stdout


def test_corrupt_inputs_never_raise_server_errors(seeded):
    lyst, owner = seeded
    samples = [(b"PK\x03\x04" + b"\0" * 200, "x.xlsx"), (b"%PDF-1.7\n%garbage", "x.pdf"), (b"\xff\xfe" + "a,b\n1,2\n".encode("utf-16-le"), "u.csv"),
               (b"{\"a\": [1, 2", "x.json"), (b"PAR1garbage", "x.parquet"), (b"<html><table><tr><td>1", "x.html"),
               (io.BytesIO(b"PK\x05\x06" + b"\0" * 18).getvalue(), "empty.docx")]
    for content, name in samples:
        try:
            lyst.ingest.inspect(owner, "northwind", content, filename=name)
        except (ValueError, PackError):
            pass  # a plain refusal is the right outcome; anything else would be a server error
