# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's dashboard kit: the building blocks of the Dash apps Studio generates.

`theme` (one source of colour and type, light and dark), `icons` (a bundled Lucide subset drawn as CSS masks),
`components` (cards, KPI rows, findings, grids, the shell), `figures` (Plotly factories with one look),
`state` (global filters, click-to-filter, URL state), `app` (the app factory and frame), `mcp` and `checks`
(the app as an MCP server, and the in-process check that every page and callback runs).

A generated app does not import Lyst: `vendor(folder)` copies this package and the analytics modules it uses
into `<folder>/lystkit/`, so the app runs anywhere with `pip install -r requirements.txt && python app.py`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

__all__ = ["KIT_FILES", "vendor"]

_HERE = Path(__file__).resolve().parent
_ANALYTICS = _HERE.parent / "analytics"
ANALYTICS_MODULES = ("fmt", "insights", "drivers", "timeseries", "stats", "decomp")
KIT_FILES = ("app.py", "checks.py", "components.py", "figures.py", "icons.py", "mcp.py", "state.py", "theme.py")

_INIT = '''# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""lystkit: Lyst's dashboard kit and analytics modules, vendored beside this app so it runs on its own."""
'''


def vendor(folder: str | Path) -> list[str]:
    """Copy the kit into `<folder>/lystkit/` (dashkit + analytics). Returns the written paths, relative to folder."""
    folder = Path(folder)
    root = folder / "lystkit"
    if root.exists():
        shutil.rmtree(root)
    kit, ana = root / "dashkit", root / "analytics"
    kit.mkdir(parents=True)
    ana.mkdir(parents=True)
    written = []
    (root / "__init__.py").write_text(_INIT)
    written.append("lystkit/__init__.py")
    (kit / "__init__.py").write_text(_INIT.replace("lystkit:", "lystkit.dashkit:"))
    written.append("lystkit/dashkit/__init__.py")
    for name in KIT_FILES:
        shutil.copy2(_HERE / name, kit / name)
        written.append(f"lystkit/dashkit/{name}")
    for sub in ("static", "catalog"):
        shutil.copytree(_HERE / sub, kit / sub, ignore=shutil.ignore_patterns("__pycache__"))
        written += [f"lystkit/dashkit/{sub}/{p.name}" for p in sorted((kit / sub).iterdir())]
    (ana / "__init__.py").write_text(_INIT.replace("lystkit:", "lystkit.analytics:"))
    written.append("lystkit/analytics/__init__.py")
    for name in ANALYTICS_MODULES:
        shutil.copy2(_ANALYTICS / f"{name}.py", ana / f"{name}.py")
        written.append(f"lystkit/analytics/{name}.py")
    return written
