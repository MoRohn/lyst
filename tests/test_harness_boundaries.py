# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Rules written twice: once as guidance in AGENTS.md, once here as a check an agent cannot talk its way past."""

import ast
from pathlib import Path

from lyst.store import schema

SRC = Path(__file__).resolve().parents[1] / "src" / "lyst"
UI_MAY_IMPORT = ("lyst.ui", "lyst.semantic")  # the UI is an HTTP client (decision 0003); semantic is plain models
UI_NEVER = ("duckdb", "lyst.store", "lyst.runtime", "lyst.app", "lyst.data", "lyst.security")


def _imports(path: Path) -> list[str]:
    """Every module a file imports, with relative imports resolved against its package."""
    tree = ast.parse(path.read_text(), str(path))
    rel = path.relative_to(SRC.parent).with_suffix("")
    pkg = list(rel.parts[:-1])  # a module and a package __init__ both resolve against their folder
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - node.level + 1]
                out.append(".".join(base + ([node.module] if node.module else [])))
            elif node.module:
                out.append(node.module)
    return out


def test_ui_code_never_reaches_the_database_or_the_engine():
    """GUIDE: the UI talks to the engine over HTTP only. CHECK: no UI module imports the store, runtime or data."""
    bad = []
    for f in (SRC / "ui").rglob("*.py"):
        for mod in _imports(f):
            if mod.startswith("lyst.") and not mod.startswith(UI_MAY_IMPORT):
                bad.append(f"{f.relative_to(SRC)} imports {mod}")
            elif mod.split(".")[0] == "duckdb" or mod.startswith(UI_NEVER):
                bad.append(f"{f.relative_to(SRC)} imports {mod}")
    assert bad == []


def test_backend_never_imports_the_ui():
    """GUIDE: backend threads never touch src/lyst/ui. CHECK: no module outside it imports lyst.ui."""
    bad = [f"{f.relative_to(SRC)} imports {mod}" for f in SRC.rglob("*.py") if "ui" not in f.relative_to(SRC).parts[:1]
           for mod in _imports(f) if mod == "lyst.ui" or mod.startswith("lyst.ui.")]
    assert bad == []


def test_migration_versions_are_unique_and_lanes_stay_in_range():
    """GUIDE: each lane appends migrations in its own range. CHECK: no version is reused, and the harness lane's
    tables live in 100-109."""
    for name, migs in (("control", schema.CONTROL), ("workspace", schema.WORKSPACE)):
        versions = [v for v, _ in migs]
        assert len(versions) == len(set(versions)), f"{name} reuses a migration version"
    for v, sql in schema.CONTROL + schema.WORKSPACE:
        if "harness_rules" in sql or "task_receipts" in sql:
            assert 100 <= v <= 109
        if "event_outbox" in sql or "event_inbox" in sql:
            assert 110 <= v <= 119
        if "answer_feedback" in sql:
            assert 120 <= v <= 129


def test_the_run_loop_ends_tasks_only_through_fail_and_finish():
    """GUIDE: failures go through _fail/_finish (fenced on the lease). CHECK: the run loop and the step runner hold no
    raw UPDATE of a task's status."""
    tree = ast.parse((SRC / "runtime" / "__init__.py").read_text())
    loop = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in ("_run_task", "_run_step")]
    assert len(loop) == 2
    for fn in loop:
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "UPDATE tasks SET status" not in node.value, f"{fn.name} writes a task's status directly"


def test_agents_map_points_at_files_that_exist():
    """The map is small and only useful if it is true: every path it names exists."""
    import re

    root = SRC.parents[1]
    text = (root / "AGENTS.md").read_text()
    paths = set(re.findall(r"`((?:src|docs|tests|scripts)/[\w./-]+)`", text))
    assert paths, "AGENTS.md names no paths"
    missing = sorted(p for p in paths if not (root / p.rstrip("/")).exists())
    assert missing == []
    assert len(text.splitlines()) <= 120, "AGENTS.md is a map, not a manual"
