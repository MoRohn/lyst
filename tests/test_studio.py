# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""New scenario B: a sentence becomes a verified, refreshable reporting dashboard."""

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from lyst.security import AccessDenied
from lyst.studio import refresh_interval, wants_dashboard

from .conftest import ask


def test_request_parsing():
    assert wants_dashboard("Create an automated procurement performance reporting system")
    assert wants_dashboard("Build a revenue dashboard by region")
    assert not wants_dashboard("Why did revenue fall?")
    assert refresh_interval("refreshed weekly") == 7 * 86400 and refresh_interval("updated each day") == 86400
    assert refresh_interval("no cadence") is None


def test_b_dashboard_from_a_sentence(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Create an automated operating margin and revenue reporting dashboard by region, refreshed weekly")
    assert t["status"] == "done", t["error"]
    r = t["result"]
    assert r["status"] == "complete" and "verified by a headless render" in r["answer"]
    d = lyst.studio.list(owner, "northwind")[0]
    folder = Path(lyst.stores.workspace("northwind").scalar("SELECT path FROM dashboards WHERE id = ?", [d["id"]]))
    assert {p.name for p in folder.iterdir()} == {"app.py", "manifest.json", "README.md", "operating_margin_series.parquet",
                                                  "operating_margin_by_region.parquet", "revenue_series.parquet", "revenue_by_region.parquet"}
    man = json.loads((folder / "manifest.json").read_text())
    assert [m["name"] for m in man["metrics"]] == ["operating_margin", "revenue"] and man["dimension"] == "region"
    assert all(c["passed"] for c in man["checks"]) and {c["name"] for c in man["checks"]} >= {"app_syntax", "headless_render"}
    # extracts hold the governed numbers
    s = pd.read_parquet(folder / "operating_margin_series.parquet").sort_values("period")
    assert s["period"].iloc[-1] == "2026-09-01" and 0.30 < s["value"].iloc[-1] < 0.35
    readme = (folder / "README.md").read_text()
    assert "## Metrics" in readme and "Operating margin (v1)" in readme and "```sql" in readme
    # the app reads nothing but its extracts
    app = (folder / "app.py").read_text()
    assert "duckdb" not in app and "read_parquet" in app and "manifest.json" in app
    # refresh schedule proposed, not yet active
    sched = lyst.runtime.schedules(owner, "northwind")[0]
    assert sched["enabled"] is False and json.loads(sched["request"])["question"] == f"refresh dashboard {d['id']}"
    assert "Approve the refresh schedule" in r["actions"][0]
    # approve, tick, and the refresh task runs through the runtime
    admin = lyst.guard.create_user("adm@n.test", "Adm")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    lyst.runtime.decide(admin, sched["approval_id"], True)
    before = (folder / "manifest.json").stat().st_mtime_ns
    started = lyst.runtime.tick_schedules(datetime.now(UTC) + timedelta(days=8))
    assert len(started) == 1
    lyst.runtime.drain()
    rt = lyst.runtime.get(owner, started[0])
    assert rt["status"] == "done" and rt["result"]["answer"].endswith("published.")
    d2 = lyst.studio.list(owner, "northwind")[0]
    assert d2["refresh_count"] == 1 and (folder / "manifest.json").stat().st_mtime_ns > before
    # bundle and lineage
    z = zipfile.ZipFile(__import__("io").BytesIO(lyst.studio.bundle(owner, "northwind", d["id"])))
    assert "app.py" in z.namelist() and "manifest.json" in z.namelist()
    down = lyst.knowledge.lineage(owner, "northwind", "metric", "revenue")["downstream"]
    assert any(x["kind"] == "artifact" and x["name"] == d["id"] for x in down)


def test_failed_refresh_keeps_the_old_extracts_and_marks_stale(seeded, monkeypatch):
    lyst, owner = seeded
    ask(lyst, owner, "Build a revenue dashboard by region")
    d = lyst.studio.list(owner, "northwind")[0]
    folder = Path(lyst.stores.workspace("northwind").scalar("SELECT path FROM dashboards WHERE id = ?", [d["id"]]))
    good = (folder / "revenue_series.parquet").read_bytes()
    real = lyst.studio._materialize

    def boom(*a, **k):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(lyst.studio, "_materialize", boom)
    out = lyst.studio.refresh(owner, "northwind", d["id"])
    assert out["status"] == "stale" and "source unavailable" in out["checks"][0]["detail"]
    assert (folder / "revenue_series.parquet").read_bytes() == good
    monkeypatch.setattr(lyst.studio, "_materialize", real)
    assert lyst.studio.refresh(owner, "northwind", d["id"])["status"] == "published"


def test_viewer_cannot_build_but_can_read(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    ask(lyst, owner, "Build a revenue dashboard by region")
    assert lyst.studio.list(viewer, "northwind") and lyst.studio.manifest(viewer, "northwind", lyst.studio.list(viewer, "northwind")[0]["id"])
    with pytest.raises(AccessDenied):
        lyst.studio.refresh(viewer, "northwind", lyst.studio.list(viewer, "northwind")[0]["id"])
    r = ask(lyst, viewer, "Build a revenue dashboard by region")["result"]
    assert r["status"] == "needs_input" and "editor role" in r["answer"]
    assert len(lyst.studio.list(owner, "northwind")) == 1


def test_rejects_metrics_without_a_shared_dimension(seeded):
    lyst, owner = seeded
    with pytest.raises(ValueError, match="share no dimension"):
        lyst.studio.design(owner, "northwind", ["revenue", "tickets"], dimension=None, title=None, every_seconds=None)
