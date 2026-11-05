# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Sandboxed Python and guarded SQL over governed data: contained, policy-filtered, recorded, reproducible."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from lyst import sandbox
from lyst.api import create_app
from lyst.security import AccessDenied

needs_sandbox = pytest.mark.skipif(sandbox.backend()[0] is None, reason="no OS sandbox on this host")


@needs_sandbox
def test_python_on_governed_inputs_and_reproduction(seeded):
    lyst, owner = seeded
    wb = lyst.workbench
    code = ("by = pnl.groupby('region', as_index=False)['revenue'].sum()\n"
            "print('regions', len(by))\n"
            "result = by.sort_values('revenue', ascending=False)\n")
    run = wb.python(owner, "northwind", code, inputs={"pnl": "SELECT region, revenue FROM pnl"})
    assert run["status"] == "ok", run["error"]
    assert run["result_kind"] == "table" and run["result"]["columns"] == ["region", "revenue"]
    assert "regions" in run["stdout"] and run["backend"] == sandbox.backend()[0]
    assert run["inputs"]["pnl"]["datasets"] == {"pnl": 1} and run["inputs"]["pnl"]["rows"] > 0
    assert run["environment"]["packages"]["pandas"]
    rep = wb.reproduce(owner, "northwind", run["id"])
    assert rep["same"] is True and rep["observed_changed"] is False
    saved = wb.save(owner, "northwind", run["id"], "revenue_by_region")
    assert saved["derived_from"] == ["pnl"] and saved["rows"] == len(run["result"]["preview"])
    cat = {d["name"]: d for d in lyst.data.catalog(owner, "northwind")}
    assert cat["revenue_by_region"]["source"] == f"workbench:{run['id']}"
    # values come back too; errors come back as errors with the line
    v = wb.python(owner, "northwind", "result = {'n': int(len(pnl)), 'mean': float(pnl.revenue.mean())}",
                  inputs={"pnl": "SELECT revenue FROM pnl"})
    assert v["result_kind"] == "value" and v["result"]["value"]["n"] > 0
    bad = wb.python(owner, "northwind", "x = 1\nraise ValueError('nope')")
    assert bad["status"] == "error" and "nope" in bad["error"]


@needs_sandbox
def test_the_sandbox_contains_the_code(seeded, tmp_path):
    lyst, owner = seeded
    wb = lyst.workbench
    home = str(lyst.settings.home)
    probe = f"""
import os, socket
out = {{}}
for p in [{home!r} + '/control.duckdb', os.path.expanduser('~') + '/.ssh/known_hosts', {str(tmp_path / 'secret.txt')!r}]:
    try:
        open(p, 'rb').read(1); out[p] = 'READ'
    except Exception as e:
        out[p] = type(e).__name__
try:
    socket.create_connection(('1.1.1.1', 443), timeout=2); out['net'] = 'OPEN'
except Exception as e:
    out['net'] = type(e).__name__
try:
    open('/tmp/lyst-escape-test', 'w').write('x'); out['write'] = 'WROTE'
except Exception as e:
    out['write'] = type(e).__name__
out['env'] = sorted(k for k in os.environ if k.startswith('LYST'))
result = out
"""
    (tmp_path / "secret.txt").write_text("s")
    r = wb.python(owner, "northwind", probe)
    assert r["status"] == "ok", r["error"]
    seen = r["result"]["value"]
    assert "READ" not in seen.values() and seen["net"] != "OPEN" and seen["write"] != "WROTE" and seen["env"] == []
    t = wb.python(owner, "northwind", "while True:\n    pass", timeout=3)
    assert t["status"] in ("timeout", "killed")


def test_permissions_and_switch_off(seeded, monkeypatch):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.workbench.python(viewer, "northwind", "result = 1")
    assert lyst.workbench.sql(viewer, "northwind", "SELECT count(*) AS n FROM pnl")["rows"][0][0] > 0
    monkeypatch.setenv("LYST_SANDBOX", "off")
    with pytest.raises(ValueError, match="switched off"):
        lyst.workbench.python(owner, "northwind", "result = 1")
    assert lyst.workbench.status()["python"]["available"] is False
    monkeypatch.delenv("LYST_SANDBOX")
    with pytest.raises(ValueError, match="identifier"):
        lyst.workbench.python(owner, "northwind", "result = 1", inputs={"Bad Name": "SELECT 1"})
    # a host without an OS sandbox (a CI runner without bubblewrap) refuses Python and says why, never runs it unboxed
    monkeypatch.setattr(sandbox, "backend", lambda: (None, "no operating-system sandbox is available on this host"))
    with pytest.raises(ValueError, match="no operating-system sandbox"):
        lyst.workbench.python(owner, "northwind", "result = 1")
    assert lyst.workbench.status()["python"]["available"] is False


@needs_sandbox
def test_column_policies_reach_the_sandbox(seeded):
    lyst, owner = seeded
    editor = lyst.guard.create_user("ed@northwind.test", "Ed")
    lyst.guard.add_member(owner, "northwind", editor.user_id, "editor")
    lyst.data.set_column_policy(owner, "northwind", "pnl", "revenue", "confidential", "admin")
    from lyst.data import QueryRejected

    with pytest.raises(QueryRejected):
        lyst.workbench.python(editor, "northwind", "result = len(pnl)", inputs={"pnl": "SELECT revenue FROM pnl"})


@needs_sandbox
def test_workbench_api(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        env = c.get("/v1/environment", headers=h).json()
        assert env["python"]["available"] and env["environment"]["python"] == ".".join(map(str, sys.version_info[:3]))
        r = c.post("/v1/workspaces/northwind/workbench/python", headers=h,
                   json={"code": "result = pnl.describe()", "inputs": {"pnl": "SELECT revenue FROM pnl"}})
        assert r.status_code == 200 and r.json()["result_kind"] == "table"
        assert r.json()["result"]["columns"] == ["index", "revenue"]  # describe()'s row labels are kept
        rid = r.json()["id"]
        assert c.get("/v1/workspaces/northwind/workbench/runs", headers=h).json()[0]["id"] == rid
        assert c.post(f"/v1/workspaces/northwind/workbench/runs/{rid}/reproduce", headers=h).json()["same"] is True
        assert c.post("/v1/workspaces/northwind/workbench/sql", headers=h, json={"sql": "SELECT 1 AS one"}).json()["rows"] == [[1]]
