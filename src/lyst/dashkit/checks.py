# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Checks that run without a browser: `lint` (static rules) and `check` (in-process: the app imports, every page
renders, ids are unique, every callback's outputs and inputs exist, every page URL answers, and every callback
runs with its layout defaults through the app's own MCP server, the way the first page load would)."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Issue", "check", "lint", "load_app", "reset_dash_globals"]

_GENERIC_ID = re.compile(r"^(dropdown|graph|input|button|div|table|slider|store|text|chart|fig)[-_]?\d+$", re.I)
_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|(?i:api[_-]?key)\s*=\s*['\"][^'\"]{12,})")
_READERS = {"read_csv", "read_parquet", "read_excel", "read_json", "read_table", "read_sql"}
_VENDORED = ("lystkit",)
_NOT_APP = {"tests", "build", ".cache", "__pycache__", "data", ".venv", "venv", "node_modules", *_VENDORED}
_NOT_RELOADED = {".venv", "venv", "site-packages", "dist-packages", *_VENDORED}


@dataclass
class Issue:
    level: str          # ERROR | WARN
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.level:5} {self.path}:{self.line} [{self.rule}] {self.message}"


def _own_module(f: str | None, app_dir: Path) -> bool:
    """A module of the app itself (reloaded by `load_app`): under the app folder, but not a virtual environment inside
    it or the kit vendored next to the app."""
    if not f:
        return False
    p = Path(f).resolve()
    if not p.is_relative_to(app_dir):
        return False
    return not (set(p.relative_to(app_dir).parts) & _NOT_RELOADED)


def _py_files(app_dir: Path) -> list[Path]:
    return sorted(p for p in app_dir.rglob("*.py") if not (set(p.relative_to(app_dir).parts) & _NOT_APP))


def _is_callback_decorator(d: ast.expr) -> tuple[bool, dict]:
    call = d if isinstance(d, ast.Call) else None
    fn = call.func if call else d
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    if name != "callback":
        return False, {}
    kw = {k.arg: k.value for k in (call.keywords if call else []) if k.arg}
    return True, kw


def _const(node) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        return None


def lint(app_dir: str | Path) -> list[Issue]:
    """Static rules over the app's .py files (not tests, build scripts or the vendored kit)."""
    app_dir = Path(app_dir)
    issues: list[Issue] = []
    entry = app_dir / "app.py"
    if not entry.exists():
        return [Issue("ERROR", str(entry), 0, "entry", "no app.py")]
    for path in _py_files(app_dir):
        rel = str(path.relative_to(app_dir))
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)

        def add(level, node, rule, msg, rel=rel):
            issues.append(Issue(level, rel, getattr(node, "lineno", 0), rule, msg))

        for m in _SECRET.finditer(src):
            issues.append(Issue("ERROR", rel, src[:m.start()].count("\n") + 1, "secret", "a credential in code"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "run_server":
                add("ERROR", node, "run-server", "app.run_server is removed in Dash 3+: use app.run")
            if isinstance(node, ast.ImportFrom) and node.module == "dash.dependencies":
                add("ERROR", node, "dash-dependencies", "import Input, Output, State from dash, not dash.dependencies")
            if isinstance(node, ast.Global):
                add("ERROR", node, "global-state", "no mutable module state in an app: keep per-user state in dcc.Store or the URL")
            if isinstance(node, ast.keyword) and node.arg == "id":
                v = _const(node.value)
                if isinstance(v, str) and _GENERIC_ID.match(v):
                    add("WARN", node, "generic-id", f"id {v!r}: use a descriptive id (ids are tool inputs agents read)")
        for node in tree.body:
            for sub in ast.walk(node) if isinstance(node, ast.Assign | ast.Expr | ast.AnnAssign) else []:
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in _READERS:
                    add("ERROR", sub, "import-time-data", f"{sub.func.attr} at import time: load inside a cached function the callbacks call")
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for d in node.decorator_list:
                ok, kw = _is_callback_decorator(d)
                if not ok:
                    continue
                hidden = _const(kw.get("mcp_enabled")) is False
                background = _const(kw.get("background")) is True
                if not hidden and not ast.get_docstring(node):
                    add("WARN", node, "callback-docstring", f"{node.name}: no docstring (agents see it as the tool's description)")
                args = [a for a in node.args.args if a.arg not in ("set_progress", "self")]
                if not hidden and any(a.annotation is None for a in args):
                    add("WARN", node, "callback-types", f"{node.name}: annotate every argument (the MCP input schema)")
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "sleep"
                            and getattr(sub.func.value, "id", "") == "time" and not background):
                        add("ERROR", sub, "blocking-sleep", f"{node.name}: time.sleep in a callback blocks a worker")
    if "server = app.server" not in entry.read_text(encoding="utf-8"):
        issues.append(Issue("ERROR", "app.py", 0, "server", "expose `server = app.server` (gunicorn and hosts import it)"))
    return issues


# ------------------------------------------------------------------------------------ check

def reset_dash_globals() -> None:
    """Forget the previous app's process-wide Dash state: the page registry and `pages.*` modules, callbacks
    registered but not yet claimed by an app, decorated MCP tools, and the current-app context."""
    import dash._callback as cb  # noqa: PLC0415
    import dash._get_app as ga  # noqa: PLC0415
    import dash._pages as pg  # noqa: PLC0415
    from dash.mcp import _decorator as mcpd  # noqa: PLC0415

    pg.PAGE_REGISTRY.clear()
    for name in [m for m in sys.modules if m == "pages" or m.startswith("pages.")]:
        del sys.modules[name]
    cb.GLOBAL_CALLBACK_LIST.clear()
    cb.GLOBAL_CALLBACK_MAP.clear()
    cb.GLOBAL_INLINE_SCRIPTS.clear()
    mcpd.MCP_DECORATED_FUNCTIONS.clear()
    ga.app_context.set(None)


def load_app(app_dir: str | Path, *, fresh: bool = True):
    """Import `<app_dir>/app.py` (the app folder on sys.path) and return the module."""
    app_dir = Path(app_dir).resolve()
    if fresh:
        reset_dash_globals()
        for name, m in list(sys.modules.items()):
            if _own_module(getattr(m, "__file__", None), app_dir):
                del sys.modules[name]
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    name = f"_lk_app_{app_dir.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, app_dir / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ids(layout) -> list:
    from dash._layout_utils import traverse  # noqa: PLC0415

    return [c.id for c, _ in traverse(layout) if getattr(c, "id", None) is not None]


def _matches(pattern: dict, cid) -> bool:
    if not isinstance(cid, dict) or set(cid) != set(pattern):
        return False
    return all(isinstance(v, list) or cid.get(k) == v for k, v in pattern.items())


def check(app_dir: str | Path, *, call: bool = True, slow: float = 1.5, skip: set[str] | None = None) -> dict:
    """The in-process app check. Returns {"result": PASS|FAIL, "items": [...], "timings": {...}}."""
    import dash  # noqa: PLC0415
    from dash._utils import split_callback_id  # noqa: PLC0415

    from . import mcp as kit_mcp  # noqa: PLC0415

    os.environ.setdefault("LYSTKIT_MCP_IMAGES", "0")
    items: list[dict] = []

    def add(level, check_name, detail):
        items.append({"level": level, "check": check_name, "detail": detail})

    t0 = time.time()
    try:
        mod = load_app(app_dir)
    except Exception as err:  # noqa: BLE001
        add("FAIL", "import", f"{type(err).__name__}: {err}")
        return {"result": "FAIL", "items": items, "timings": {}}
    app = mod.app
    add("PASS", "import", f"app.py imported in {time.time() - t0:.1f}s")
    add("PASS" if getattr(mod, "server", None) is app.server else "FAIL", "server", "server = app.server")
    add("PASS" if "/_mcp" in str(app.routes) else "WARN", "mcp", "the app serves MCP at /_mcp" if "/_mcp" in str(app.routes) else "MCP is off")

    pages = list(dash.page_registry.values())
    for page in pages:
        lay = page.get("layout")
        try:
            (lay() if callable(lay) else lay)
            add("PASS", "page", f"{page['path']} renders")
        except Exception as err:  # noqa: BLE001
            add("FAIL", "page", f"{page['path']}: {type(err).__name__}: {err}")
    full = getattr(app, "validation_layout", None) or app.layout
    ids = _ids(full)
    seen, dups = set(), set()
    for i in ids:
        key = json.dumps(i, sort_keys=True) if isinstance(i, dict) else i
        (dups if key in seen else seen).add(key)
    add("PASS" if not dups else "FAIL", "unique ids", f"{len(seen)} ids" if not dups else f"duplicated: {sorted(dups)[:8]}")

    client = app.server.test_client()
    for page in pages:
        r = client.get(page["path"])
        add("PASS" if r.status_code == 200 else "FAIL", "url", f"GET {page['path']} -> {r.status_code}")
    r = client.get("/health")
    add("PASS" if r.status_code == 200 else "WARN", "health", f"GET /health -> {r.status_code}")

    str_ids = {i for i in ids if isinstance(i, str)}
    dict_ids = [i for i in ids if isinstance(i, dict)]
    missing = []
    for out_id, info in app.callback_map.items():
        parsed = split_callback_id(out_id)
        outs = parsed if isinstance(parsed, list) else [parsed]
        deps = [(o["id"], "output") for o in outs] + [(d["id"], "input") for d in info.get("inputs", []) + info.get("state", [])]
        for cid, role in deps:
            if isinstance(cid, str) and cid.startswith("{"):
                try:
                    cid = json.loads(cid)
                except ValueError:
                    pass
            if isinstance(cid, dict):
                wildcard = any(isinstance(v, list) for v in cid.values())
                if not wildcard and not any(_matches(cid, d) for d in dict_ids):
                    missing.append(f"{role} {json.dumps(cid)}")
            elif cid not in str_ids and not cid.startswith("_pages"):
                missing.append(f"{role} {cid}")
    n_cb = len(app.callback_map)
    add("PASS" if not missing else "FAIL", "callback ids",
        f"every output and input of {n_cb} callbacks exists in the app's layouts" if not missing else
        f"not in any layout (an empty optional part dropped by `if x:`?): {sorted(set(missing))[:8]}")

    timings = {}
    if call:
        rep = kit_mcp.probe(app, skip=skip)
        for row in rep["tools"]:
            res = row["called"]
            if isinstance(res, str) and res.startswith("ERROR"):
                add("FAIL", "callback", f"{row['name']}: {res[6:][:300]}")
        c = kit_mcp.Client.for_app(app)
        for t in c.tools():
            if t["name"] in (skip or set()) or t["name"] == "get_dash_component":
                continue
            args = kit_mcp._defaults(t)
            req = set((t.get("inputSchema") or {}).get("required") or [])
            if req - set(args):
                continue
            s = time.time()
            try:
                c.call_raw(t["name"], args)
            except Exception:  # noqa: BLE001, S112 - reported above
                continue
            timings[t["name"]] = round(time.time() - s, 3)
        slow_ones = {k: v for k, v in timings.items() if v > slow}
        n_err = sum(1 for i in items if i["check"] == "callback")
        add("PASS" if not n_err else "FAIL", "callbacks", f"{len(timings)} ran with their layout defaults via MCP" + (f"; {n_err} failed" if n_err else ""))
        if slow_ones:
            add("WARN", "slow callbacks", ", ".join(f"{k} {v:.1f}s" for k, v in sorted(slow_ones.items(), key=lambda x: -x[1])))
        add("PASS" if rep["typed_share"] >= 1 else "WARN", "mcp types", f"{rep['typed_share']:.0%} of tools have typed inputs")
    result = "FAIL" if any(i["level"] == "FAIL" for i in items) else "PASS"
    return {"result": result, "items": items, "timings": timings}
