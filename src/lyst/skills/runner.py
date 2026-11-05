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


# Library entry points that read files or fetch URLs. They are replaced in the child before the skill
# runs, so an allow-listed module cannot be used to do what the removed `open` would have done.
_IO_ENTRY_POINTS = {
    "pandas": ("read_csv", "read_table", "read_fwf", "read_json", "read_html", "read_xml", "read_excel",
               "read_parquet", "read_feather", "read_orc", "read_pickle", "read_hdf", "read_sas", "read_spss",
               "read_stata", "read_sql", "read_sql_query", "read_sql_table", "read_clipboard", "read_gbq"),
    "numpy": ("load", "loadtxt", "genfromtxt", "fromfile", "memmap", "fromregex", "save", "savez",
              "savez_compressed", "savetxt"),
}
_FORBIDDEN_FROM = frozenset({"os", "sys", "io", "subprocess", "socket", "pickle", "ctypeslib", "f2py",
                             "testing", "system", "popen"})


def _limit_resources(cpu_seconds: int) -> str | None:
    """Apply the caps; returns why they could not all be applied, or None when they were."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except (ImportError, ValueError, OSError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _refused(what: str):
    def refuse(*_a, **_k):
        raise PermissionError(f"{what} is not allowed in a skill")
    return refuse


def _close_io() -> None:
    """Take file and network access away from the libraries a skill may import."""
    import socket

    for mod_name, names in _IO_ENTRY_POINTS.items():
        mod = sys.modules.get(mod_name)
        for name in names:
            if mod is not None and hasattr(mod, name):
                setattr(mod, name, _refused(f"{mod_name}.{name}"))
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "socketpair", "fromfd"):
        setattr(socket, name, _refused(f"socket.{name}"))
    socket.socket.connect = _refused("network access")  # type: ignore[method-assign]
    socket.socket.connect_ex = _refused("network access")  # type: ignore[method-assign]
    socket.socket.bind = _refused("network access")  # type: ignore[method-assign]
    import io

    builtins.open = _refused("open")  # type: ignore[assignment]
    io.open = _refused("open")  # type: ignore[assignment]


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
    report: dict = {"cases": [], "error": None, "limits": None}
    try:
        job = json.loads(sys.stdin.read())
        if not isinstance(job, dict) or not isinstance(job.get("source"), str) \
                or not isinstance(job.get("cases"), list):
            raise ValueError("a job needs a source string and a list of cases")
        cpu_seconds = int(job.get("cpu_seconds", 5))
    except (ValueError, TypeError) as exc:
        report["error"] = f"job: {type(exc).__name__}: {exc}"
        print(json.dumps(report))
        return
    # None means every cap is in place; otherwise the reason, so the parent can see an uncapped run.
    report["limits"] = _limit_resources(cpu_seconds)
    if report["limits"]:
        print(f"skill sandbox: resource limits not applied: {report['limits']}", file=sys.stderr)
    import numpy as np  # noqa: F401 - loaded before the hook so pandas' own imports work
    import pandas as pd

    real_import = builtins.__import__
    roots = {m.split(".")[0] for m in ALLOWED_MODULES}
    namespace: dict = {}

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        # Keyed on the identity of the skill's own globals, which the skill cannot forge (it can rebind
        # __name__ but not swap the dict its code runs in).
        if globals is namespace:
            if level or name.split(".")[0] not in roots:
                raise ImportError(f"module {name} is not allowed in a skill")
            for item in fromlist or ():
                if item.startswith("_") or item.startswith("read_") or item in _FORBIDDEN_FROM:
                    raise ImportError(f"importing {item} from {name} is not allowed in a skill")
        return real_import(name, globals, locals, fromlist, level)

    safe_builtins = {k: v for k, v in vars(builtins).items()
                     if k not in {"open", "exec", "eval", "compile", "input", "breakpoint", "help", "exit", "quit",
                                  "memoryview", "globals", "locals", "vars", "__loader__", "__spec__"}}
    safe_builtins["__import__"] = guarded_import
    namespace.update({"__name__": "skill", "__builtins__": safe_builtins})
    _close_io()
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
        if not isinstance(case, dict):
            report["cases"].append({"name": "", "kind": "invalid", "passed": False,
                                    "error": "a case must be an object"})
            continue
        df = pd.DataFrame(case.get("rows", []), columns=case.get("columns"))
        entry = {"name": case.get("name", ""), "kind": case.get("kind", "representative")}
        try:
            out = fn(df, **case.get("params", {}))
            out = json.loads(json.dumps(out, default=float))
            entry["output"] = out
            if case.get("expect_error"):
                entry["passed"] = False  # the case exists to prove the skill refuses this input
                entry["error"] = "expected an error, but the skill returned a result"
            elif "expect" in case:
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
