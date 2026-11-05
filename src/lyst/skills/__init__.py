# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Governed skills: Observe → Propose → Test → Verify → Approve → Register → Monitor.

A skill is a small Python function ``run(df, **params) -> dict`` with a purpose, the governed SQL that
feeds it, trigger phrases, and test cases. It never touches a database, file or network itself: the
engine runs its input query through the SQL guard (so roles and column policies apply) and hands it
a DataFrame in a sandboxed process.

Lifecycle, enforced here:
* ``propose`` stores a new version as ``proposed`` after static checks.
* ``test`` runs the author's representative cases plus generated adversarial cases (empty input, nulls,
  a single row) in the sandbox; the version becomes ``tested`` or ``failed`` with the report kept.
* ``request_registration`` opens a ``skill.register`` approval. Only a tested version qualifies, and the
  proposer cannot approve it (``TaskRuntime.decide``).
* On approval the version becomes ``approved`` and the analyst can plan with it; earlier approved
  versions become ``superseded``. ``retire`` reverses a registration.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..data import DataService
from ..security import Guard, Principal
from ..store import Stores
from .runner import ALLOWED_MODULES

FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr",
                   "setattr", "delattr", "input", "breakpoint", "memoryview", "type", "object", "super"}
FORBIDDEN_ATTRS = re.compile(
    r"^(_.*|read_.*|to_(csv|parquet|pickle|sql|excel|json|html|clipboard|feather|hdf|stata|latex|xml|markdown)|"
    r"eval|io|os|sys|subprocess|socket|pickle|load|loads|save|savez|fromfile|tofile|memmap|system|popen|"
    r"DataFrame\.from_.*|ctypeslib|f2py|testing)$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


class SkillRejected(ValueError):
    pass


def static_check(source: str) -> list[str]:
    """Reasons a source is refused before it ever runs."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]
    problems: list[str] = []
    roots = {m.split(".")[0] for m in ALLOWED_MODULES}
    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for mod in mods:
                if mod.split(".")[0] not in roots:
                    problems.append(f"import of {mod} is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"use of {node.id} is not allowed")
        elif isinstance(node, ast.Attribute) and FORBIDDEN_ATTRS.match(node.attr):
            problems.append(f"attribute .{node.attr} is not allowed")
        elif isinstance(node, ast.Global | ast.Nonlocal):
            problems.append("global state is not allowed")
        elif isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
    if not has_run:
        problems.append("define run(df, **params)")
    return sorted(set(problems))


def adversarial_cases(columns: list[str]) -> list[dict[str, Any]]:
    """Inputs every skill must survive: it may return a result or a clear error, never crash the run."""
    return [
        {"name": "empty input", "kind": "adversarial", "columns": columns, "rows": []},
        {"name": "all nulls", "kind": "adversarial", "columns": columns, "rows": [[None] * len(columns)] * 3},
        {"name": "single row", "kind": "adversarial", "columns": columns, "rows": [[1] * len(columns)]},
    ]


def run_sandboxed(source: str, cases: list[dict[str, Any]], *, timeout_s: float = 20,
                  cpu_seconds: int = 10) -> dict[str, Any]:
    job = json.dumps({"source": source, "cases": cases, "cpu_seconds": cpu_seconds}, default=str)
    src_root = str(Path(__file__).resolve().parents[2])
    with tempfile.TemporaryDirectory() as cwd:
        env = {"PYTHONPATH": src_root, "PATH": "/usr/bin:/bin", "HOME": cwd, "LC_ALL": "C.UTF-8"}
        try:
            proc = subprocess.run([sys.executable, "-I", "-c",
                                   f"import sys; sys.path.insert(0, {src_root!r}); "
                                   "from lyst.skills.runner import main; main()"],
                                  input=job, capture_output=True, text=True, timeout=timeout_s, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {timeout_s}s", "cases": []}
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"error": f"sandbox exited {proc.returncode}: {proc.stderr.strip()[-400:]}", "cases": []}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "sandbox returned no report", "cases": []}


class SkillService:
    def __init__(self, stores: Stores, guard: Guard, data: DataService) -> None:
        self.stores = stores
        self.guard = guard
        self.data = data

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    def propose(self, actor: Principal, ws: str, *, name: str, purpose: str, source: str, input_sql: str,
                tests: list[dict[str, Any]], triggers: list[str], params: dict[str, Any] | None = None) -> dict:
        self.guard.require(actor, ws, "skill.propose", name)
        if not _NAME.match(name):
            raise SkillRejected("a skill name is 3-41 lowercase letters, digits or underscores")
        if not tests:
            raise SkillRejected("a skill needs at least one test case with an expected result")
        problems = static_check(source)
        policy = self.data.policy_for(actor, ws)
        from ..data.sqlguard import check_read_query

        guarded = check_read_query(input_sql, policy)  # raises QueryRejected for a bad input query
        manifest = {"input_sql": input_sql, "reads": guarded.datasets, "triggers": [t.lower() for t in triggers],
                    "params": params or {}}
        db = self._db(ws)

        def tx(c):
            v = (c.execute("SELECT max(version) FROM skills WHERE name = ?", [name]).fetchone()[0] or 0) + 1
            c.execute("INSERT INTO skills (name, version, status, purpose, source, tests, permissions, test_report, "
                      "proposed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [name, v, "rejected" if problems else "proposed", purpose, source, json.dumps(tests),
                       json.dumps(manifest), json.dumps({"static": problems}), actor.user_id])
            return v

        version = db.write(tx)
        self.guard.audit(actor, ws, "skill.propose", f"{name}@{version}", "rejected" if problems else "ok",
                         {"problems": problems})
        return {"name": name, "version": version, "status": "rejected" if problems else "proposed", "problems": problems}

    def get(self, actor: Principal, ws: str, name: str, version: int | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read", name)
        sql = "SELECT * FROM skills WHERE name = ?" + (" AND version = ?" if version else " ORDER BY version DESC LIMIT 1")
        rows = self._db(ws).query(sql, [name, version] if version else [name])
        if not rows:
            raise KeyError(name)
        s = rows[0]
        for k in ("tests", "permissions", "test_report"):
            s[k] = json.loads(s[k]) if s[k] else None
        return s

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        return self._db(ws).query("SELECT name, version, status, purpose, proposed_by, approved_by, created_at "
                                  "FROM skills ORDER BY name, version DESC")

    def test(self, actor: Principal, ws: str, name: str, version: int) -> dict[str, Any]:
        self.guard.require(actor, ws, "skill.propose", name)
        s = self.get(actor, ws, name, version)
        if s["status"] not in ("proposed", "failed", "tested"):
            raise SkillRejected(f"{name} v{version} is {s['status']}")
        problems = static_check(s["source"])
        if problems:
            report = {"static": problems, "cases": [], "passed": False}
        else:
            sample = self.data.query(actor, ws, s["permissions"]["input_sql"], max_rows=1)
            cases = list(s["tests"]) + adversarial_cases(sample.columns)
            res = run_sandboxed(s["source"], cases)
            ok = not res.get("error") and res["cases"] and all(c["passed"] for c in res["cases"])
            report = {"static": [], "error": res.get("error"), "cases": res.get("cases", []), "passed": bool(ok),
                      "representative": sum(1 for c in res.get("cases", []) if c["kind"] == "representative"),
                      "adversarial": sum(1 for c in res.get("cases", []) if c["kind"] == "adversarial")}
        status = "tested" if report["passed"] else "failed"
        self._db(ws).execute("UPDATE skills SET status = ?, test_report = ? WHERE name = ? AND version = ?",
                             [status, json.dumps(report, default=str), name, version])
        self.guard.audit(actor, ws, "skill.test", f"{name}@{version}", status)
        return {"name": name, "version": version, "status": status, "report": report}

    def request_registration(self, actor: Principal, ws: str, name: str, version: int, runtime) -> str:
        self.guard.require(actor, ws, "skill.propose", name)
        s = self.get(actor, ws, name, version)
        if s["status"] != "tested":
            raise SkillRejected("only a tested version can be registered")
        return runtime.request_approval(ws, s["proposed_by"], "skill.register",
                                        f"Register skill {name} v{version}: {s['purpose']}",
                                        {"name": name, "version": version})

    def on_approved(self, approver: Principal, ws: str, name: str, version: int) -> None:
        def tx(c):
            c.execute("UPDATE skills SET status = 'superseded' WHERE name = ? AND status = 'approved'", [name])
            c.execute("UPDATE skills SET status = 'approved', approved_by = ? WHERE name = ? AND version = ? "
                      "AND status = 'tested'", [approver.user_id, name, version])

        self._db(ws).write(tx)
        self.guard.audit(approver, ws, "skill.register", f"{name}@{version}", "ok")

    def retire(self, actor: Principal, ws: str, name: str) -> None:
        self.guard.require(actor, ws, "skill.approve", name)
        self._db(ws).execute("UPDATE skills SET status = 'retired' WHERE name = ? AND status = 'approved'", [name])
        self.guard.audit(actor, ws, "skill.retire", name, "ok")

    def approved(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        rows = self._db(ws).query("SELECT name, version, purpose, permissions FROM skills WHERE status = 'approved'")
        for r in rows:
            r["permissions"] = json.loads(r["permissions"])
        return rows

    def match(self, actor: Principal, ws: str, text: str) -> dict[str, Any] | None:
        t = " " + re.sub(r"[^a-z0-9 ]+", " ", text.lower()) + " "
        best = None
        for s in self.approved(actor, ws):
            for phrase in s["permissions"]["triggers"] + [s["name"].replace("_", " ")]:
                if f" {phrase} " in t and (best is None or len(phrase) > best[0]):
                    best = (len(phrase), s)
        return best[1] if best else None

    def discover(self, actor: Principal, ws: str, objective: str, registry=None, *, limit: int = 5) -> dict[str, Any]:
        """Approved skills (and built-in capabilities) ranked by what they do, with compatibility, permission, health
        and use checks; ``reuse`` names the skill to run instead of drafting a new one (see skills.discovery)."""
        self.guard.require(actor, ws, "workspace.read")
        from .discovery import discover  # noqa: PLC0415

        if registry is None:
            from ..analyst.registry import REGISTRY as registry  # noqa: PLC0415, N811
        return discover(self, registry, actor, ws, objective, limit=limit)

    def execute(self, actor: Principal, ws: str, name: str, *, task_id: str | None = None,
                params: dict[str, Any] | None = None) -> dict[str, Any]:
        rows = self._db(ws).query("SELECT version, source, permissions FROM skills WHERE name = ? AND status = 'approved'",
                                  [name])
        if not rows:
            raise KeyError(f"no approved skill named {name}")
        s = rows[0]
        manifest = json.loads(s["permissions"])
        q = self.data.query(actor, ws, manifest["input_sql"], task_id=task_id)
        res = run_sandboxed(s["source"], [{"name": "run", "columns": q.columns, "rows": q.rows,
                                           "params": {**manifest.get("params", {}), **(params or {})}}])
        if res.get("error") or not res["cases"] or res["cases"][0].get("error"):
            raise RuntimeError(f"skill {name} failed: {res.get('error') or res['cases'][0].get('error')}")
        return {"skill": name, "version": s["version"], "output": res["cases"][0]["output"],
                "evidence": [{"purpose": f"input to skill {name} v{s['version']}", "sql": q.sql,
                              "query_id": q.query_id, "rows": len(q.rows), "ms": q.ms, "datasets": q.datasets}]}


__all__ = ["SkillRejected", "SkillService", "adversarial_cases", "run_sandboxed", "static_check"]
