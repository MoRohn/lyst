# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Running a person's Python on governed data without giving it the engine.

The code runs in a separate interpreter inside an operating-system sandbox, never in the engine process:

* **macOS**: ``sandbox-exec`` with a deny-by-default profile. Reads are allowed only from the system, the Python
  installation, the environment's packages and the job's own folder; writes only to the job folder; no network.
  The Lyst home (encrypted databases), the key file, the user's home and every other folder are unreadable.
* **Linux**: ``bwrap`` (bubblewrap) with every namespace unshared (no network), the root bound read-only, the Lyst
  home and the user's credential folders (.ssh, .aws, .config, keyrings ...) hidden behind empty tmpfs mounts and
  the job folder bound read-write. Written to bubblewrap's documented flags; not exercised on this Mac.
* Otherwise the Python workbench is unavailable and says why; ``LYST_SANDBOX=off`` turns it off everywhere.

Inside: CPU-seconds and file-size limits (and address space on Linux), a wall-clock timeout, a scrubbed
environment (no ``LYST_*`` variables, no key, ``HOME`` is the job folder), one thread per numeric library. Inputs
arrive as Parquet files already filtered by the caller's column and row policies; the code sees them as pandas
DataFrames. Whatever it assigns to ``result`` (a DataFrame, a number, a string, a list or dict) comes back.

The sandbox is a containment layer for an editor's own analysis code, not a boundary for hostile code from
strangers: only workspace editors may use it, every run is audited, and administrators can switch it off.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

RUNNER = r'''
import io, json, os, resource, sys, traceback, contextlib
job = sys.argv[1]
limits = json.load(open(os.path.join(job, "limits.json")))
resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu"], limits["cpu"] + 1))
resource.setrlimit(resource.RLIMIT_FSIZE, (limits["fsize"], limits["fsize"]))
if sys.platform.startswith("linux"):
    resource.setrlimit(resource.RLIMIT_AS, (limits["memory"], limits["memory"]))
import pandas as pd
import numpy as np
data = {}
for name in json.load(open(os.path.join(job, "inputs.json"))):
    data[name] = pd.read_parquet(os.path.join(job, "in", name + ".parquet"))
scope = {"pd": pd, "np": np, "data": data, **data, "__name__": "__workbench__"}
out, err = io.StringIO(), io.StringIO()
status, error = "ok", None
code = open(os.path.join(job, "code.py")).read()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    try:
        exec(compile(code, "workbench", "exec"), scope)
    except BaseException as exc:
        status = "error"
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        error = "".join(line for line in tb if 'File "workbench"' in line or not line.startswith("  File")).strip()[-4000:]
res = scope.get("result")
meta = {"status": status, "error": error, "stdout": out.getvalue()[-limits["output"]:],
        "stderr": err.getvalue()[-limits["output"]:], "result_kind": None}
if status == "ok" and res is not None:
    if isinstance(res, pd.Series):
        res = res.to_frame()
    if isinstance(res, pd.DataFrame):
        # a groupby key, a set index or row labels (describe()) are data; a row number left by sorting is not
        labels = any(n is not None for n in res.index.names) or not pd.api.types.is_integer_dtype(res.index)
        frame = res.reset_index() if labels else res.reset_index(drop=True)
        frame.columns = [str(c) for c in frame.columns]
        frame.to_parquet(os.path.join(job, "result.parquet"), index=False)
        meta["result_kind"] = "table"
    else:
        try:
            meta["result"] = json.loads(json.dumps(res, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
            meta["result_kind"] = "value"
        except (TypeError, ValueError):
            meta["result"], meta["result_kind"] = repr(res)[:2000], "value"
json.dump(meta, open(os.path.join(job, "meta.json"), "w"))
'''


@dataclass
class Limits:
    timeout: float = 60.0
    cpu_seconds: int = 60
    memory_mb: int = 2048
    file_mb: int = 256
    output_chars: int = 20_000


@dataclass
class Outcome:
    status: str  # ok | error | timeout | killed
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    result_kind: str | None = None
    result: Any = None
    table: pd.DataFrame | None = None
    seconds: float = 0.0
    backend: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def backend() -> tuple[str | None, str]:
    """Which OS sandbox this host offers, or why none is used."""
    mode = os.environ.get("LYST_SANDBOX", "auto")
    if mode == "off":
        return None, "the Python workbench is switched off on this installation (LYST_SANDBOX=off)"
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec", "macOS sandbox profile: no network, reads limited to the system, Python and the job folder"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap", "bubblewrap: no network, read-only root, Lyst home and credential folders hidden"
    return None, ("no operating-system sandbox is available on this host (macOS sandbox-exec or Linux bubblewrap), so "
                  "the Python workbench is off; install bubblewrap to enable it")


def _read_roots(hidden: list[str] | None = None) -> list[str]:
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(os.path.realpath(sys.executable))}
    for p in sys.path:
        if p and os.path.isdir(p):
            roots.add(p)
    real = {os.path.realpath(r) for r in roots} | roots
    # the interpreter's install folder as a whole (uv and pyenv keep lib-dynload beside a versioned symlink)
    for r in list(real):
        parts = Path(r).parts
        for marker in ("python", "pyenv", "Python.framework"):
            if marker in parts:
                real.add(str(Path(*parts[: parts.index(marker) + 1])))
    # never a folder that contains the Lyst home (encrypted databases, key file) or is the user's home itself
    guard = [os.path.realpath(h) for h in (hidden or []) if h] + [os.path.realpath(os.path.expanduser("~"))]
    safe = []
    for r in real:
        rr = os.path.realpath(r)
        if any(g == rr or g.startswith(rr.rstrip("/") + "/") for g in guard):
            continue
        safe.append(r)
    return sorted(safe)


def _darwin_profile(job: Path, hidden: list[str]) -> str:
    q = lambda p: '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
    reads = " ".join(f"(subpath {q(r)})" for r in _read_roots(hidden) + [str(job), os.path.realpath(job)])
    system = " ".join(f"(subpath {q(p)})" for p in ("/System", "/usr", "/bin", "/private/etc", "/dev", "/Library/Apple",
                                                     "/private/var/db", "/Library/Preferences"))
    return f"""(version 1)
(deny default)
(allow process-fork)
(allow process-exec)
(allow sysctl-read)
(allow mach-lookup)
(allow signal (target self))
(allow file-read-metadata)
(allow file-read* (literal "/") {system} {reads})
(allow file-write* (subpath {q(str(job))}) (subpath {q(os.path.realpath(job))}) (literal "/dev/null"))
(deny network*)
"""


def _command(kind: str, job: Path, hidden: list[str]) -> list[str]:
    py = [sys.executable, "-I", "-B", str(job / "runner.py"), str(job)]
    if kind == "sandbox-exec":
        (job / "profile.sb").write_text(_darwin_profile(job, hidden))
        return ["sandbox-exec", "-f", str(job / "profile.sb"), *py]
    cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--ro-bind", "/", "/", "--dev", "/dev",
           "--proc", "/proc", "--tmpfs", "/tmp"]  # noqa: S108 - a fresh tmpfs inside the sandbox
    home = os.path.expanduser("~")
    secret_dirs = [os.path.join(home, d) for d in (".ssh", ".aws", ".gnupg", ".config", ".netrc", ".docker", ".kube",
                                                   ".local/share/keyrings", ".password-store")]
    for h in hidden + secret_dirs:
        if h and os.path.isdir(h):
            cmd += ["--tmpfs", h]
    cmd += ["--bind", str(job), str(job), "--chdir", str(job)]
    return cmd + py


def run(code: str, inputs: dict[str, pd.DataFrame], *, limits: Limits | None = None,
        hidden: list[str] | None = None) -> Outcome:
    """Run ``code`` with ``inputs`` in the host's sandbox. Raises RuntimeError when no sandbox is available."""
    kind, why = backend()
    if kind is None:
        raise RuntimeError(why)
    limits = limits or Limits()
    base = Path(tempfile.mkdtemp(prefix="lyst-wb-"))
    job = base / "job"
    (job / "in").mkdir(parents=True)
    try:
        for name, frame in inputs.items():
            frame.to_parquet(job / "in" / f"{name}.parquet", index=False)
        (job / "inputs.json").write_text(json.dumps(list(inputs)))
        (job / "code.py").write_text(code)
        (job / "runner.py").write_text(RUNNER)
        (job / "limits.json").write_text(json.dumps({"cpu": limits.cpu_seconds, "memory": limits.memory_mb * 1024 * 1024,
                                                     "fsize": limits.file_mb * 1024 * 1024, "output": limits.output_chars}))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(job), "TMPDIR": str(job), "LANG": "C.UTF-8", "PYTHONHASHSEED": "0",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "MPLBACKEND": "Agg",
               "PYTHONDONTWRITEBYTECODE": "1"}
        cmd = _command(kind, job, hidden or [])
        start = time.perf_counter()
        try:
            proc = subprocess.run(cmd, cwd=job, env=env, capture_output=True, timeout=limits.timeout, check=False)  # noqa: S603
        except subprocess.TimeoutExpired:
            return Outcome("timeout", error=f"stopped after {limits.timeout:.0f} seconds (the workbench time limit)",
                           seconds=round(time.perf_counter() - start, 3), backend=kind)
        seconds = round(time.perf_counter() - start, 3)
        meta_path = job / "meta.json"
        if not meta_path.exists():
            tail = (proc.stderr or b"").decode(errors="replace")[-2000:]
            reason = "the CPU time limit" if proc.returncode in (-24, 152, 137, -9) else f"exit status {proc.returncode}"
            return Outcome("killed", stderr=tail, error=f"the code was stopped by the sandbox ({reason})", seconds=seconds,
                           backend=kind)
        meta = json.loads(meta_path.read_text())
        out = Outcome(meta["status"], stdout=meta["stdout"], stderr=meta["stderr"], error=meta["error"],
                      result_kind=meta["result_kind"], result=meta.get("result"), seconds=seconds, backend=kind)
        if meta["result_kind"] == "table":
            out.table = pd.read_parquet(job / "result.parquet")
        return out
    finally:
        shutil.rmtree(base, ignore_errors=True)
