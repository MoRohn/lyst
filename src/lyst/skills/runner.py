# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Runs one skill in a separate, locked-down Python process. Invoked as
``python -I -m lyst.skills.runner`` with a JSON job on stdin; prints a JSON report on stdout.

Inside the child: the import hook admits only an allow-list of modules, builtins that reach the
filesystem, the network or the interpreter (open, exec, eval, compile, __import__ of anything else,
input, breakpoint) are removed, CPU time and file size are capped, and the parent kills the process
at its wall-clock timeout. This is process isolation plus an allow-list, not a kernel sandbox; see
docs/SKILLS.md for what it does and does not stop.
"""

from __future__ import annotations

import builtins
import json
import math
import sys

ALLOWED_MODULES = {"math", "statistics", "numpy", "pandas", "scipy", "scipy.stats", "decimal", "fractions",
                   "itertools", "functools", "collections", "datetime", "re", "json"}


def _limit_resources(cpu_seconds: int) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except (ImportError, ValueError, OSError):
        pass


def _approx(a, b, tol: float) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_approx(a[k], b[k], tol) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_approx(x, y, tol) for x, y in zip(a, b, strict=True))
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=tol, abs_tol=tol)
    return a == b


def main() -> None:
    job = json.loads(sys.stdin.read())
    _limit_resources(int(job.get("cpu_seconds", 5)))
    import numpy as np  # noqa: F401 - loaded before the hook so pandas' own imports work
    import pandas as pd

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        caller = (globals or {}).get("__name__", "")
        if caller == "skill" and name.split(".")[0] not in {m.split(".")[0] for m in ALLOWED_MODULES}:
            raise ImportError(f"module {name} is not allowed in a skill")
        return real_import(name, globals, locals, fromlist, level)

    safe_builtins = {k: v for k, v in vars(builtins).items()
                     if k not in {"open", "exec", "eval", "compile", "input", "breakpoint", "help", "exit", "quit",
                                  "memoryview", "globals", "locals", "vars", "__loader__", "__spec__"}}
    safe_builtins["__import__"] = guarded_import
    namespace = {"__name__": "skill", "__builtins__": safe_builtins}
    report = {"cases": [], "error": None}
    try:
        exec(compile(job["source"], "<skill>", "exec"), namespace)  # noqa: S102 - the point of the sandbox
        fn = namespace.get("run")
        if not callable(fn):
            raise ValueError("a skill must define run(df, **params)")
    except BaseException as exc:  # noqa: BLE001
        report["error"] = f"load: {type(exc).__name__}: {exc}"
        print(json.dumps(report))
        return
    for case in job["cases"]:
        df = pd.DataFrame(case.get("rows", []), columns=case.get("columns"))
        entry = {"name": case.get("name", ""), "kind": case.get("kind", "representative")}
        try:
            out = fn(df, **case.get("params", {}))
            out = json.loads(json.dumps(out, default=float))
            entry["output"] = out
            if "expect" in case:
                entry["passed"] = _approx(out, case["expect"], float(case.get("tolerance", 1e-9)))
            else:
                entry["passed"] = isinstance(out, dict)  # adversarial: must return a result, not crash
        except BaseException as exc:  # noqa: BLE001
            entry["passed"] = bool(case.get("expect_error"))
            entry["error"] = f"{type(exc).__name__}: {exc}"
        report["cases"].append(entry)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
