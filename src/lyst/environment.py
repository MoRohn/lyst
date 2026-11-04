# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What a computation ran on: interpreter, platform and the versions of the packages that decide its numbers.

``fingerprint()`` is recorded with every experiment run and reproduction so a different result can be traced to a
different environment rather than guessed at. ``diff(a, b)`` names what changed.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

# packages whose versions can move a number Lyst reports
NUMERIC = ("lyst", "duckdb", "numpy", "pandas", "pyarrow", "scipy", "statsmodels", "sqlglot", "openpyxl", "pypdf")


def _version(pkg: str) -> str | None:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


def _lock_digest() -> str | None:
    for parent in Path(__file__).resolve().parents:
        lock = parent / "uv.lock"
        if lock.exists():
            return hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
    return None


@lru_cache(maxsize=1)
def fingerprint() -> dict[str, Any]:
    packages = {p: _version(p) for p in NUMERIC}
    env = {"python": platform.python_version(), "implementation": platform.python_implementation(),
           "platform": f"{sys.platform}-{platform.machine()}", "packages": packages, "lock": _lock_digest()}
    env["digest"] = hashlib.sha256(json.dumps({k: env[k] for k in ("python", "packages")}, sort_keys=True).encode()).hexdigest()[:16]
    return env


def diff(a: dict[str, Any] | None, b: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What differs between two fingerprints that could change a result (platform and lock are informational)."""
    if not a or not b:
        return []
    out = []
    if a.get("python") != b.get("python"):
        out.append({"what": "python", "was": a.get("python"), "now": b.get("python")})
    pa, pb = a.get("packages", {}), b.get("packages", {})
    for p in sorted(set(pa) | set(pb)):
        if pa.get(p) != pb.get(p):
            out.append({"what": p, "was": pa.get(p), "now": pb.get(p)})
    return out
