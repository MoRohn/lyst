# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""New scenario B: a sentence becomes a verified, refreshable reporting dashboard."""

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from lyst import demo
from lyst.security import AccessDenied
from lyst.studio import refresh_interval, wants_dashboard

from .conftest import ask


def test_request_parsing():
    assert wants_dashboard("Create an automated procurement performance reporting system")
    assert wants_dashboard("Build a revenue dashboard by region")
    assert not wants_dashboard("Why did revenue fall?")
    assert refresh_interval("refreshed weekly") == 7 * 86400 and refresh_interval("updated each day") == 86400
    assert refresh_interval("no cadence") is None


def test_b_dashboard_from_a_sentence_is_a_dash_app(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Create an automated operating margin and revenue reporting dashboard by region, refreshed weekly")
    assert t["status"] == "done", t["error"]
    r = t["result"]
    d = lyst.studio.list(owner, "northwind")[0]
    assert r["status"] == "complete" and "verified on every page" in r["answer"], json.dumps(d["checks"], indent=1)
    # the answer opens the dashboard itself, with its trends as charts; nobody is told to run a command
    assert {k: r["open"][k] for k in ("kind", "id", "title")} == {"kind": "dashboard", "id": d["id"], "title": d["title"]}
    assert [c["title"] for c in r["charts"]] == ["Operating margin by month", "Revenue by month"]
    said = " ".join([r["answer"], r["brief"], *r["actions"]])
    assert "pip install" not in said and "python app.py" not in said and "cd /" not in said
    folder = Path(lyst.stores.workspace("northwind").scalar("SELECT path FROM dashboards WHERE id = ?", [d["id"]]))
    names = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and ".cache" not in p.parts and "__pycache__" not in p.parts}
    assert {"app.py", "theme.toml", "requirements.txt", "DESIGN.md", "README.md", "manifest.json", "lyst_dash/settings.py",
            "lyst_dash/data.py", "lyst_dash/ui.py", "pages/overview.py", "pages/breakdown.py", "pages/method.py",
            "tests/test_app.py", "data/parts_operating_margin.parquet", "data/parts_revenue.parquet"} <= names
    man = json.loads((folder / "manifest.json").read_text())
    assert man["target"] == "dash" and [m["name"] for m in man["metrics"]] == ["operating_margin", "revenue"] and man["dimension"] == "region"
    assert all(c["passed"] for c in man["checks"]) and {c["name"] for c in man["checks"]} >= {"app_syntax", "dash_check"}
    # the parts extracts recompute the governed numbers exactly (a ratio from its numerator and denominator)
    parts = pd.read_parquet(folder / "data" / "parts_operating_margin.parquet")
    latest = parts[parts["period"] == parts["period"].max()]
    assert latest["num"].sum() / latest["den"].sum() == pytest.approx(next(m["latest_value"] for m in man["metrics"] if m["name"] == "operating_margin"), rel=1e-9)
    emea = parts[parts["region"] == "EMEA"].groupby("period")[["num", "den"]].sum()
    df = demo.pnl()
    e = df[df.region == "EMEA"].groupby("month")[["operating_income", "revenue"]].sum()
    assert (emea["num"] / emea["den"]).iloc[-1] == pytest.approx(e.operating_income.iloc[-1] / e.revenue.iloc[-1], rel=1e-9)
    design = (folder / "DESIGN.md").read_text()
    assert "## Pages" in design and "click to filter" in design and "Tableau dashboards" in design
    assert "no purples" in design.lower()
    # the app's own tests pass (dashkit check + manifest values), in a separate process
    import subprocess
    import sys

    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(folder / "tests")], capture_output=True,
                         text=True, timeout=300, cwd=folder)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    # extracts hold the governed numbers
    s = pd.read_parquet(folder / "operating_margin_series.parquet").sort_values("period")
    assert s["period"].iloc[-1] == "2026-09-01" and 0.30 < s["value"].iloc[-1] < 0.35
    readme = (folder / "README.md").read_text()
    assert "## Metrics" in readme and "Operating margin (v1)" in readme and "```sql" in readme
    # the app reads nothing but its extracts
    sources = "\n".join(p.read_text() for p in folder.rglob("*.py"))
    assert "duckdb" not in sources and "read_parquet" in (folder / "lyst_dash" / "data.py").read_text()
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
    man2 = json.loads((folder / "manifest.json").read_text())
    assert man2["target"] == "dash" and all(c["passed"] for c in man2["checks"])
    d2 = lyst.studio.list(owner, "northwind")[0]
    assert d2["refresh_count"] == 1 and (folder / "manifest.json").stat().st_mtime_ns > before
    # bundle and lineage
    z = zipfile.ZipFile(__import__("io").BytesIO(lyst.studio.bundle(owner, "northwind", d["id"])))
    assert "app.py" in z.namelist() and "manifest.json" in z.namelist()
    down = lyst.knowledge.lineage(owner, "northwind", "metric", "revenue")["downstream"]
    assert any(x["kind"] == "artifact" and x["name"] == d["id"] for x in down)


def test_streamlit_target_on_request(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Build a revenue dashboard by region in Streamlit")["result"]
    assert "verified on every page" in r["answer"] and r["open"]["kind"] == "dashboard"
    assert "streamlit run" not in " ".join(r["actions"])
    d = lyst.studio.list(owner, "northwind")[0]
    folder = Path(lyst.stores.workspace("northwind").scalar("SELECT path FROM dashboards WHERE id = ?", [d["id"]]))
    man = json.loads((folder / "manifest.json").read_text())
    assert man["target"] == "streamlit" and {c["name"] for c in man["checks"]} >= {"headless_render"}
    assert "read_parquet" in (folder / "app.py").read_text() and not (folder / "pages").exists()


def test_non_additive_metric_is_refused_honestly(seeded):
    from lyst.semantic import Measure, MetricSpec

    lyst, owner = seeded
    lyst.semantic.define(owner, "northwind", MetricSpec(name="avg_ticket", title="Average ticket", dataset="pnl",
                                                        measure=Measure(agg="avg", column="revenue"), time_column="month",
                                                        dimensions=["region"], format="currency"))
    with pytest.raises(ValueError, match="not additive"):
        lyst.studio.design(owner, "northwind", ["avg_ticket"], dimension="region", title=None, every_seconds=None)


def test_failed_refresh_keeps_the_old_extracts_and_marks_stale(seeded, monkeypatch):
    lyst, owner = seeded
    ask(lyst, owner, "Build a revenue dashboard by region in Streamlit")
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


class Crash(BaseException):
    pass


def test_g_interrupted_dashboard_project_resumes_without_duplicates(seeded, monkeypatch):
    """New scenario G: a crash right after the build step did its work must not produce a second dashboard."""
    from lyst.runtime import TaskRuntime

    lyst, owner = seeded
    real = lyst.services.registry.run
    state = {"crashed": False}

    def crash_after_build(name, ctx, args):
        out = real(name, ctx, args)
        if name == "build_dashboard" and not state["crashed"]:
            state["crashed"] = True
            raise Crash()  # the process dies after files and rows were written, before the checkpoint commit
        return out

    monkeypatch.setattr(lyst.services.registry, "run", crash_after_build)
    tid = lyst.runtime.submit(owner, "northwind", "Build a revenue dashboard by region, refreshed weekly")
    with pytest.raises(Crash):
        lyst.runtime.run_once()
    lyst.stores.control.execute("UPDATE tasks SET lease_until = now() - INTERVAL 1 SECOND WHERE id = ?", [tid])
    assert TaskRuntime(lyst.stores, lyst.services, worker_id="w2").run_once() == tid
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "done" and t["result"]["status"] == "complete"
    assert len(lyst.studio.list(owner, "northwind")) == 1
    assert len(lyst.runtime.schedules(owner, "northwind")) == 1  # one refresh schedule, not two
    out = json.loads(lyst.stores.control.scalar("SELECT output FROM task_steps WHERE task_id = ? AND capability = 'build_dashboard'", [tid]))
    assert out.get("resumed") is True
