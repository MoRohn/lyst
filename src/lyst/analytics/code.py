# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Source-code analysis on files held as data (a ``path`` and a ``source`` column), never on the engine's own disk.

What it does, and the open-source practice each part follows:

* **Python, from the syntax tree** (``ast``): McCabe cyclomatic complexity per function (decision points + 1, the
  counting radon and mccabe use; above 10 is flagged), function length, nesting depth, and security patterns modelled
  on Bandit's checks (eval/exec B307/B102, pickle and marshal loads B301/B302, ``yaml.load`` without a safe loader
  B506, ``subprocess`` with ``shell=True`` B602, ``os.system`` B605, SQL built by string formatting B608, hard-coded
  passwords B105, ``assert`` used for checks B101, weak hashes B303/B324, ``requests`` with ``verify=False`` B501).
* **Solidity, from the source text**: patterns modelled on Slither detectors (reentrancy: an external call before a
  state write, ``tx.origin`` for authorization, unchecked low-level calls, ``delegatecall`` to a variable, unprotected
  ``selfdestruct``, a floating pragma, ``block.timestamp`` in conditions).
* **Any language**: lines of code, comments, TODO/FIXME markers and duplicated blocks (hashes of normalized six-line
  windows).
* **Hotspots**: with a change-history table (path, changes, defects), files ranked by changes x complexity, the
  measure used in behavioural code analysis.
* **Tests**: ``run_tests`` writes the files into Lyst's operating-system sandbox (``sandbox.py``: no network, no
  access to the Lyst home, CPU and wall-clock limits) and runs the test functions there, returning each test's
  outcome and the traceback of each failure. Nothing from the files runs in the engine process.

Findings are evidence for a person to review, cited by file and line. A pattern match is not a proven vulnerability.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter
from typing import Any

import pandas as pd

from .methods import MethodError, _table

# -- Python -------------------------------------------------------------------------------------------------------

_DECISIONS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.With, ast.AsyncWith,
              ast.Assert, ast.comprehension)
SECURITY_RULES = {
    "B307": ("use of eval", "high"), "B102": ("use of exec", "high"), "B301": ("pickle/marshal load of untrusted data", "medium"),
    "B506": ("yaml.load without a safe loader", "medium"), "B602": ("subprocess call with shell=True", "high"),
    "B605": ("os.system starts a shell", "high"), "B608": ("SQL built by string formatting (injection)", "medium"),
    "B105": ("hard-coded password", "low"), "B101": ("assert used for a runtime check", "low"),
    "B303": ("weak hash (md5/sha1) for security", "medium"), "B501": ("TLS verification disabled", "high"),
}


def _complexity(fn: ast.AST) -> int:
    c = 1
    for node in ast.walk(fn):
        if isinstance(node, _DECISIONS):
            c += 1
        elif isinstance(node, ast.BoolOp):
            c += len(node.values) - 1
        elif isinstance(node, ast.match_case) if hasattr(ast, "match_case") else False:
            c += 1
    return c


def _depth(node: ast.AST, d: int = 0) -> int:
    nested = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)
    return max([d] + [_depth(c, d + 1 if isinstance(c, nested) else d) for c in ast.iter_child_nodes(node)])


def _call_name(node: ast.Call) -> str:
    f = node.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def _python_findings(path: str, tree: ast.AST) -> list[dict[str, Any]]:
    out = []

    def add(rule: str, node: ast.AST, detail: str = "") -> None:
        title, sev = SECURITY_RULES[rule]
        out.append({"path": path, "line": getattr(node, "lineno", 0), "rule": rule, "severity": sev,
                    "finding": title + (f": {detail}" if detail else "")})

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            if name == "eval":
                add("B307", node)
            elif name == "exec":
                add("B102", node)
            elif name in ("pickle.loads", "pickle.load", "marshal.loads", "marshal.load", "cPickle.loads"):
                add("B301", node, name)
            elif name == "yaml.load" and "Loader" not in kw:
                add("B506", node)
            elif name.startswith("subprocess.") and isinstance(kw.get("shell"), ast.Constant) and kw["shell"].value is True:
                add("B602", node, name)
            elif name in ("os.system", "os.popen"):
                add("B605", node, name)
            elif name in ("hashlib.md5", "hashlib.sha1"):
                add("B303", node, name)
            elif name.split(".")[-1] in ("get", "post", "put", "request") and isinstance(kw.get("verify"), ast.Constant) \
                    and kw["verify"].value is False:
                add("B501", node, name)
            elif name.split(".")[-1] in ("execute", "executemany") and node.args:
                q = node.args[0]
                if isinstance(q, ast.JoinedStr) or (isinstance(q, ast.BinOp) and isinstance(q.op, (ast.Add, ast.Mod))) \
                        or (isinstance(q, ast.Call) and isinstance(q.func, ast.Attribute) and q.func.attr == "format"):
                    add("B608", node, name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and re.search(r"(password|passwd|pwd|secret|token)$", t.id, re.I) \
                        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.value.value:
                    add("B105", node, t.id)
        elif isinstance(node, ast.Assert) and not path.split("/")[-1].startswith("test"):
            add("B101", node)
    return out


# -- Solidity -----------------------------------------------------------------------------------------------------

SOLIDITY_RULES = [
    ("tx-origin", "high", re.compile(r"require\s*\(\s*tx\.origin\s*==|tx\.origin\s*==\s*owner"), "tx.origin used for authorization"),
    ("unchecked-lowlevel", "medium", re.compile(r"^\s*[\w.\[\]]+\.call(\{[^}]*\})?\([^;]*\);", re.M),
     "low-level call whose return value is ignored"),
    ("controlled-delegatecall", "high", re.compile(r"\.delegatecall\s*\("), "delegatecall (check the destination is trusted)"),
    ("suicidal", "high", re.compile(r"\bselfdestruct\s*\("), "selfdestruct (check it is access-controlled)"),
    ("solc-version", "low", re.compile(r"pragma solidity\s*\^"), "floating pragma"),
    ("timestamp", "low", re.compile(r"(if|require)\s*\([^)]*block\.timestamp"), "block.timestamp in a condition"),
]
_SOL_FN = re.compile(r"function\s+(\w+)\s*\([^)]*\)[^{]*\{", re.M)


def _solidity_findings(path: str, src: str) -> list[dict[str, Any]]:
    out = []
    for rule, sev, rx, title in SOLIDITY_RULES:
        for m in rx.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            text = src[m.start():src.find("\n", m.start())]
            if rule == "suicidal" and re.search(r"onlyOwner|require\s*\(\s*msg\.sender\s*==", src[max(0, m.start() - 300):m.start()]):
                continue
            if rule == "unchecked-lowlevel" and re.search(r"(bool|success|ok)\s*[,)=]", text):
                continue
            out.append({"path": path, "line": line, "rule": rule, "severity": sev, "finding": title})
    # reentrancy: within a function, an external value call happens before a write to a state mapping or variable
    for m in _SOL_FN.finditer(src):
        start, depth, i = m.end(), 1, m.end()
        while i < len(src) and depth:
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        body = src[start:i]
        call = re.search(r"\.call\{value:|\.call\.value\(|\.transfer\(|\.send\(", body)
        if call:
            after = body[call.end():]
            if re.search(r"\b\w+\s*\[[^\]]+\]\s*(=|-=|\+=)|\b\w+\s*(-=|\+=)", after):
                out.append({"path": path, "line": src.count("\n", 0, start + call.start()) + 1, "rule": "reentrancy-eth",
                            "severity": "high", "finding": f"reentrancy in {m.group(1)}: external call before the state update"})
    return out


# -- any language -------------------------------------------------------------------------------------------------


def _duplicates(files: dict[str, str], window: int = 6) -> list[dict[str, Any]]:
    seen: dict[str, tuple[str, int]] = {}
    dups = []
    for path, src in files.items():
        lines = [re.sub(r"\s+", " ", ln.strip()) for ln in src.splitlines()]
        for i in range(len(lines) - window + 1):
            block = lines[i:i + window]
            if sum(1 for b in block if len(b) > 3) < window:
                continue
            h = hashlib.sha1("\n".join(block).encode()).hexdigest()  # noqa: S324 - fingerprint, not security
            if h in seen and seen[h][0] != path:
                dups.append({"path": path, "line": i + 1, "duplicate_of": f"{seen[h][0]}:{seen[h][1]}"})
            seen.setdefault(h, (path, i + 1))
    return dups


def analyze(files: pd.DataFrame, *, path: str = "path", source: str = "source", history: pd.DataFrame | None = None,
            focus: str = "all") -> dict[str, Any]:
    """Static analysis of source files held as rows. ``focus``: all, security, maintainability."""
    if files.empty:
        raise MethodError("no source files to analyse")
    src = {str(r[path]): str(r[source]) for _, r in files.iterrows()}
    functions, findings, parse_errors = [], [], []
    loc = Counter()
    for p, text in src.items():
        lines = text.splitlines()
        loc["lines"] += len(lines)
        loc["todo"] += sum(1 for ln in lines if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", ln))
        if p.endswith(".py"):
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                parse_errors.append(f"{p}:{exc.lineno}")
                continue
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    functions.append({"path": p, "function": fn.name, "line": fn.lineno, "complexity": _complexity(fn),
                                      "length": (fn.end_lineno or fn.lineno) - fn.lineno + 1, "depth": _depth(fn)})
            findings += _python_findings(p, tree)
        elif p.endswith(".sol"):
            findings += _solidity_findings(p, text)
    fn = pd.DataFrame(functions)
    complex_fns = fn[fn["complexity"] > 10].sort_values("complexity", ascending=False) if len(fn) else fn
    dups = _duplicates(src)
    tables, out_findings = [], []
    sec = pd.DataFrame(findings)
    if len(sec):
        sev_rank = {"high": 0, "medium": 1, "low": 2}
        sec = sec.sort_values(["severity", "path", "line"], key=lambda c: c.map(sev_rank) if c.name == "severity" else c)
        tables.append(_table("Security findings (pattern matches to review)", sec))
        top = sec.iloc[0]
        out_findings.append(f"{len(sec)} security finding(s), {int((sec['severity'] == 'high').sum())} high; first: "
                            f"{top['finding']} at {top['path']}:{top['line']} ({top['rule']}).")
    if len(complex_fns):
        tables.append(_table("Functions above complexity 10", complex_fns))
        c0 = complex_fns.iloc[0]
        out_findings.append(f"{len(complex_fns)} function(s) above complexity 10; worst {c0['function']} in {c0['path']} "
                            f"({c0['complexity']}).")
    if dups:
        out_findings.append(f"{len(dups)} duplicated six-line block(s), e.g. {dups[0]['path']}:{dups[0]['line']} repeats "
                            f"{dups[0]['duplicate_of']}.")
        tables.append(_table("Duplicated blocks", pd.DataFrame(dups)))
    hotspot = None
    if history is not None and len(history) and len(fn):
        per_file = fn.groupby("path")["complexity"].sum().rename("complexity")
        h = history.set_index(history.columns[0]).join(per_file, how="left").fillna({"complexity": 0})
        h["hotspot_score"] = h.iloc[:, 0] * h["complexity"]
        h = h.sort_values("hotspot_score", ascending=False).reset_index()
        hotspot = h.iloc[0]
        tables.append(_table("Hotspots (changes x complexity)", h.round(2)))
        out_findings.append(f"Top hotspot: {hotspot.iloc[0]} (changed {int(hotspot.iloc[1])} times, total complexity "
                            f"{int(hotspot['complexity'])}): refactoring here pays back most.")
    if parse_errors:
        out_findings.append(f"{len(parse_errors)} file(s) did not parse: {', '.join(parse_errors[:3])}.")
    lead = out_findings[0] if out_findings else "No security patterns, complex functions or duplicated blocks were found."
    if focus == "maintainability" and hotspot is not None:
        lead = out_findings[-1] if not parse_errors else lead
    return {"summary": f"{len(src)} file(s), {loc['lines']:,} lines, {len(fn)} function(s). " + lead,
            "findings": out_findings + ([f"{loc['todo']} TODO/FIXME marker(s)."] if loc["todo"] else []),
            "tables": tables,
            "metrics": {"files": len(src), "lines": int(loc["lines"]), "functions": int(len(fn)),
                        "security_findings": int(len(sec)), "complex_functions": int(len(complex_fns)),
                        "duplicates": len(dups)},
            "assumptions": ["Security findings are pattern matches modelled on Bandit and Slither rules: review each "
                            "before treating it as a vulnerability.",
                            "Cyclomatic complexity counts decision points plus one, as radon and mccabe do."]}


TEST_RUNNER = r'''
import importlib.util, inspect, os, sys, traceback, json
root = os.path.join(os.getcwd(), "src_files")
sys.path.insert(0, root)
results = []
for dirpath, _, names in os.walk(root):
    if dirpath not in sys.path:
        sys.path.insert(0, dirpath)  # tests import the modules beside them
    for n in sorted(names):
        if not (n.startswith("test") and n.endswith(".py")):
            continue
        full = os.path.join(dirpath, n)
        spec = importlib.util.spec_from_file_location(n[:-3], full)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            results.append({"test": n, "outcome": "error", "trace": traceback.format_exc()[-1500:]})
            continue
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith("test"):
                continue
            try:
                fn()
                results.append({"test": f"{n}::{name}", "outcome": "passed", "trace": ""})
            except Exception:
                results.append({"test": f"{n}::{name}", "outcome": "failed", "trace": traceback.format_exc()[-1500:]})
result = results
'''


def run_tests(files: pd.DataFrame, *, path: str = "path", source: str = "source", timeout: float = 60.0) -> dict[str, Any]:
    """Run the files' test functions inside Lyst's OS sandbox and report each outcome with failure tracebacks."""
    from .. import sandbox  # noqa: PLC0415

    py = files[files[path].astype(str).str.endswith(".py")]
    if py.empty or not any(str(p).split("/")[-1].startswith("test") for p in py[path]):
        raise MethodError("no Python test files (test*.py) among the source files")
    lines = ["import os", "os.makedirs('src_files', exist_ok=True)"]
    for _, r in py.iterrows():
        rel = str(r[path]).lstrip("/").replace("..", "_")
        lines.append(f"os.makedirs(os.path.dirname(os.path.join('src_files', {rel!r})) or 'src_files', exist_ok=True)")
        lines.append(f"open(os.path.join('src_files', {rel!r}), 'w').write({str(r[source])!r})")
    code = "\n".join(lines) + "\n" + TEST_RUNNER
    kind, why = sandbox.backend()
    if kind is None:  # no OS sandbox on this host: report it, never run the code anywhere else
        raise MethodError(why)
    try:
        out = sandbox.run(code, {}, limits=sandbox.Limits(timeout=timeout))
    except RuntimeError as exc:
        raise MethodError(str(exc)) from None
    if out.status != "ok":
        raise MethodError(f"the tests did not run in the sandbox: {out.error or out.status}")
    res = out.result or []
    failed = [r for r in res if r["outcome"] != "passed"]
    first = failed[0] if failed else None
    last_line = (first["trace"].strip().splitlines() or [""])[-1] if first else ""
    return {"tests": res, "passed": len(res) - len(failed), "failed": len(failed), "backend": out.backend,
            "summary": f"{len(res)} test(s) ran in the sandbox: {len(res) - len(failed)} passed, {len(failed)} failed"
                       + (f"; first failure {first['test']}: {last_line}" if first else ".")}
