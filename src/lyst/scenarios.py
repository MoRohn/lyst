# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario engine: driver models, what-if, sensitivity, Monte Carlo and best/base/worst cases.

A **model** is a small, typed calculation graph:

* ``inputs``: named values. Each has a base value that is either an assumption typed by a person or **observed**:
  bound to a governed metric (``{"metric": "revenue", "period": "last quarter"}``) or a measure on a dataset
  (``{"dataset": "pnl", "column": "opex", "agg": "sum", "period": "last quarter"}``). Observed values are computed
  through the SQL guard (the person's own permissions and row policies) and carry their period, dataset version and
  data-through date. An input may declare a plausible range (``low``/``high``) and a distribution for uncertainty
  (``normal`` with ``sd``, ``triangular`` or ``uniform`` over the range).
* ``formulas``: ``name = expression`` over other names. Expressions are arithmetic only (``+ - * / **``, parentheses,
  ``min max abs round``, numbers): parsed with ``ast`` and evaluated by a small interpreter, never ``eval``. The graph
  is ordered topologically; unknown names and cycles are refused when the model is defined.
* ``outputs``: the nodes a person cares about (with ``higher_is_better`` and a format).

A **scenario** changes any node, input or formula: ``{"labor_costs": {"change_pct": 6}, "revenue": {"change_pct": 3}}``
or ``{"labor_share": {"value": 0.6}}``. Only the nodes downstream of what changed are recomputed (the result lists
them), so moving one assumption does not rerun unrelated calculations or re-query data.

Every result is labelled **hypothetical** and keeps the observed base apart from the scenario values. Validation
checks ranges, plausibility of changes, non-finite results (division by zero), and how old the observed data is.
Monte Carlo is vectorized with NumPy and seeded, so the same model, scenario and seed give the same distribution.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import operator
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

from .security import Principal

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
FUNCS = {"min": np.minimum, "max": np.maximum, "abs": np.abs, "round": np.round}
BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
          ast.Pow: operator.pow}
DISTRIBUTIONS = ("normal", "triangular", "uniform")
STALE_DAYS = 120  # observed data older than this (by data-through date) is flagged


class ModelError(ValueError):
    pass


# ----------------------------------------------------------------------------- expressions


def _check(node: ast.AST, names: set[str]) -> None:
    if isinstance(node, ast.Expression):
        return _check(node.body, names)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return None
    if isinstance(node, ast.Name):
        names.add(node.id)
        return None
    if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
        _check(node.left, names)
        return _check(node.right, names)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        return _check(node.operand, names)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords
            and 1 <= len(node.args) <= 2):
        for a in node.args:
            _check(a, names)
        return None
    raise ModelError(f"unsupported expression element: {ast.dump(node)[:60]}")


def parse_expr(text: str) -> tuple[ast.Expression, set[str]]:
    if len(text) > 500:
        raise ModelError("formula too long")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ModelError(f"formula {text!r} is not valid: {exc.msg}") from exc
    names: set[str] = set()
    _check(tree, names)
    return tree, names - set(FUNCS)


def _eval(node: ast.AST, env: dict[str, Any]):
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, env), _eval(node.right, env)
        if isinstance(node.op, ast.Div):
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.divide(left, right)
        return BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.Call):
        return FUNCS[node.func.id](*[_eval(a, env) for a in node.args])
    raise ModelError("unsupported expression")  # unreachable after _check


# ----------------------------------------------------------------------------- the model


class Model:
    """A validated driver model. ``spec`` is the stored JSON."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.inputs: dict[str, dict[str, Any]] = {}
        self.formulas: dict[str, tuple[ast.Expression, set[str], str]] = {}
        for item in spec.get("inputs") or []:
            n = item.get("name", "")
            if not NAME.match(n):
                raise ModelError(f"input name {n!r} must be lower_snake_case")
            if n in self.inputs:
                raise ModelError(f"{n} is defined twice")
            if "value" not in item and "source" not in item:
                raise ModelError(f"input {n} needs a value or a data source")
            if item.get("distribution") and item["distribution"] not in DISTRIBUTIONS:
                raise ModelError(f"{n}: distribution must be one of {', '.join(DISTRIBUTIONS)}")
            self.inputs[n] = item
        for item in spec.get("formulas") or []:
            n = item.get("name", "")
            if not NAME.match(n):
                raise ModelError(f"formula name {n!r} must be lower_snake_case")
            if n in self.inputs or n in self.formulas:
                raise ModelError(f"{n} is defined twice")
            tree, refs = parse_expr(item["expr"])
            self.formulas[n] = (tree, refs, item["expr"])
        known = set(self.inputs) | set(self.formulas)
        for n, (_t, refs, expr) in self.formulas.items():
            missing = refs - known
            if missing:
                raise ModelError(f"{n} = {expr} uses undefined {', '.join(sorted(missing))}")
        self.order = self._topo()
        self.outputs = [o if isinstance(o, dict) else {"name": o} for o in (spec.get("outputs") or [])]
        for o in self.outputs:
            if o["name"] not in known:
                raise ModelError(f"output {o['name']} is not defined")
        if not self.outputs:
            raise ModelError("a model needs at least one output")
        self.children: dict[str, set[str]] = {n: set() for n in known}
        for n, (_t, refs, _e) in self.formulas.items():
            for r in refs:
                self.children[r].add(n)

    def _topo(self) -> list[str]:
        order: list[str] = []
        state: dict[str, int] = {}

        def visit(n: str, path: list[str]) -> None:
            if state.get(n) == 2:
                return
            if state.get(n) == 1:
                raise ModelError("circular formulas: " + " -> ".join(path + [n]))
            state[n] = 1
            if n in self.formulas:
                for r in sorted(self.formulas[n][1]):
                    visit(r, path + [n])
            state[n] = 2
            order.append(n)

        for n in list(self.inputs) + list(self.formulas):
            visit(n, [])
        return order

    @property
    def names(self) -> list[str]:
        return self.order

    def label(self, n: str) -> str:
        for item in (self.spec.get("inputs") or []) + (self.spec.get("formulas") or []):
            if item["name"] == n:
                return item.get("label") or n.replace("_", " ")
        return n.replace("_", " ")

    def downstream(self, names: set[str]) -> set[str]:
        out, stack = set(names), list(names)
        while stack:
            for c in self.children.get(stack.pop(), ()):
                if c not in out:
                    out.add(c)
                    stack.append(c)
        return out

    def evaluate(self, base: dict[str, Any], overrides: dict[str, Any] | None = None, *,
                 previous: dict[str, Any] | None = None, changed: set[str] | None = None) -> tuple[dict[str, Any], list[str]]:
        """Values of every node. ``base`` holds the input base values (scalars or NumPy arrays for simulation).
        With ``previous`` values and the ``changed`` node names, only their downstream nodes are recomputed."""
        overrides = overrides or {}
        for n in overrides:
            if n not in self.inputs and n not in self.formulas:
                raise ModelError(f"{n} is not part of this model")
        todo = set(self.order) if previous is None or changed is None else self.downstream(changed)
        env: dict[str, Any] = dict(previous or {})
        computed: list[str] = []
        for n in self.order:
            if n not in todo:
                continue
            v = base[n] if n in self.inputs else _eval(self.formulas[n][0], env)
            env[n] = apply_change(v, overrides.get(n))
            computed.append(n)
        return env, computed


def apply_change(value, change):
    if change is None:
        return value
    if isinstance(change, int | float):
        return float(change)
    if "value" in change:
        return float(change["value"])
    if "change_pct" in change:
        return value * (1 + float(change["change_pct"]) / 100)
    if "change_abs" in change:
        return value + float(change["change_abs"])
    raise ModelError("a change is {value}, {change_pct} or {change_abs}")


def bounds(item: dict[str, Any], base_value: float) -> tuple[float | None, float | None]:
    """An input's plausible range: absolute ``low``/``high``, or ``range_pct`` [-10, 15] relative to its base value
    (useful for observed inputs, whose base is only known when the data is read)."""
    if item.get("range_pct"):
        lo_pct, hi_pct = item["range_pct"]
        a, b = base_value * (1 + lo_pct / 100), base_value * (1 + hi_pct / 100)
        return min(a, b), max(a, b)
    return item.get("low"), item.get("high")


def _num(v) -> float | None:
    f = float(v)
    return f if math.isfinite(f) else None


# ----------------------------------------------------------------------------- analysis functions (pure)


def validate(model: Model, base: dict[str, float], values: dict[str, Any], overrides: dict[str, Any],
             observed: dict[str, dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    """Checks on assumptions, parameter ranges, changes, results and data freshness. Each is {check, status, detail}."""
    today = today or datetime.now(UTC).date()
    out: list[dict[str, Any]] = []
    for n, item in model.inputs.items():
        v = values.get(n)
        lo, hi = bounds(item, float(base[n]))
        if lo is not None and hi is not None and lo > hi:
            out.append({"check": f"range:{n}", "status": "fail", "detail": f"{n}: low {lo} is above high {hi}"})
        elif v is not None and ((lo is not None and v < lo) or (hi is not None and v > hi)):
            out.append({"check": f"range:{n}", "status": "warn",
                        "detail": f"{model.label(n)} = {v:,.4g} is outside its plausible range [{lo}, {hi}]"})
    for n, ch in overrides.items():
        pct = ch.get("change_pct") if isinstance(ch, dict) else None
        if pct is not None and abs(float(pct)) > 50:
            out.append({"check": f"change:{n}", "status": "warn",
                        "detail": f"a {pct}% change to {model.label(n)} is large; check it is intended"})
    for n in model.order:
        v = values.get(n)
        if v is not None and not math.isfinite(float(v)):
            out.append({"check": f"finite:{n}", "status": "fail",
                        "detail": f"{model.label(n)} is not a finite number (division by zero or overflow)"})
    for n, meta in observed.items():
        through = meta.get("data_through")
        if through:
            age = (today - date.fromisoformat(through)).days
            if age > STALE_DAYS:
                out.append({"check": f"freshness:{n}", "status": "warn",
                            "detail": f"{model.label(n)} comes from data through {through} ({age} days old)"})
    if not out:
        out.append({"check": "all", "status": "pass", "detail": "ranges, changes, results and data freshness checked"})
    return out


def compare(model: Model, base_values: dict[str, Any], scen_values: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for o in model.outputs:
        n = o["name"]
        b, s = _num(base_values[n]), _num(scen_values[n])
        delta = None if b is None or s is None else s - b
        rows.append({"name": n, "label": model.label(n), "format": o.get("format"), "base": b, "scenario": s,
                     "delta": delta, "delta_pct": (delta / abs(b) * 100) if delta is not None and b else None,
                     "favourable": None if delta is None or delta == 0 else
                     ((delta > 0) == o.get("higher_is_better", True))})
    return rows


def sensitivity(model: Model, base: dict[str, float], output: str, overrides: dict[str, Any] | None = None,
                swing_pct: float = 10.0) -> list[dict[str, Any]]:
    """Tornado: each input moved to its range ends (or base ±swing_pct%) with the rest held; ranked by swing."""
    overrides = overrides or {}
    centre, _ = model.evaluate(base, overrides)
    c = float(centre[output])
    rows = []
    for n, item in model.inputs.items():
        b = float(centre[n])
        lo, hi = bounds(item, float(base[n]))
        lo = b * (1 - swing_pct / 100) if lo is None else lo
        hi = b * (1 + swing_pct / 100) if hi is None else hi
        outs = []
        for v in (lo, hi):
            vals, _ = model.evaluate(base, {**overrides, n: {"value": v}})
            outs.append(float(vals[output]))
        rows.append({"input": n, "label": model.label(n), "low": lo, "high": hi, "output_at_low": _num(outs[0]),
                     "output_at_high": _num(outs[1]), "swing": abs(outs[1] - outs[0]),
                     "elasticity": ((outs[1] - outs[0]) / c) / ((hi - lo) / b) if c and b and hi != lo else None})
    rows.sort(key=lambda r: -r["swing"])
    return rows


def _sample(item: dict[str, Any], base_value: float, n: int, rng: np.random.Generator) -> np.ndarray | None:
    dist = item.get("distribution")
    if not dist:
        return None
    lo, hi = bounds(item, base_value)
    if dist == "normal":
        sd = (item.get("sd_pct") or 0) / 100 * abs(base_value) or item.get("sd") or ((hi - lo) / 4 if lo is not None and hi is not None else None)
        if not sd:
            raise ModelError(f"{item['name']}: a normal distribution needs sd or a range")
        return rng.normal(base_value, sd, n)
    if lo is None or hi is None:
        raise ModelError(f"{item['name']}: a {dist} distribution needs low and high")
    if dist == "uniform":
        return rng.uniform(lo, hi, n)
    mode = min(max(base_value, lo), hi)
    return rng.triangular(lo, mode, hi, n) if hi > lo else np.full(n, lo)


def simulate(model: Model, base: dict[str, float], overrides: dict[str, Any] | None = None, *, n: int = 10_000,
             seed: int = 7, thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    """Monte Carlo over the inputs that declare a distribution. Same model, scenario and seed: same numbers."""
    if not 100 <= n <= 200_000:
        raise ModelError("simulations run between 100 and 200,000 draws")
    overrides = overrides or {}
    rng = np.random.default_rng(seed)
    centre, _ = model.evaluate(base, overrides)
    draws: dict[str, Any] = {}
    uncertain = []
    for name, item in model.inputs.items():
        s = _sample(item, float(centre[name]), n, rng) if name not in overrides else None
        draws[name] = s if s is not None else float(centre[name])
        if s is not None:
            uncertain.append(name)
    if not uncertain:
        raise ModelError("no input declares a distribution; add one (normal, triangular or uniform) to simulate")
    vals, _ = model.evaluate(draws, {k: v for k, v in overrides.items() if k not in model.inputs})
    out = {}
    for o in model.outputs:
        arr = np.asarray(vals[o["name"]], dtype=float) * np.ones(n)
        arr = arr[np.isfinite(arr)]
        stats = {"mean": float(arr.mean()), "sd": float(arr.std()), "p5": float(np.percentile(arr, 5)),
                 "p50": float(np.percentile(arr, 50)), "p95": float(np.percentile(arr, 95)),
                 "valid_draws": int(arr.size)}
        hist, edges = np.histogram(arr, bins=30)
        stats["histogram"] = {"counts": hist.tolist(), "edges": [float(e) for e in edges]}
        if thresholds and o["name"] in thresholds:
            t = thresholds[o["name"]]
            stats["p_below"] = {"threshold": t, "probability": float((arr < t).mean())}
        out[o["name"]] = stats
    return {"draws": n, "seed": seed, "uncertain_inputs": uncertain, "outputs": out}


def cases(model: Model, base: dict[str, float], output: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Best, base and worst case for ``output``: every input with a range set to the end that helps (or hurts) the
    output, judged by the sign of its effect and the output's higher_is_better."""
    overrides = overrides or {}
    hib = next((o.get("higher_is_better", True) for o in model.outputs if o["name"] == output), True)
    best, worst = dict(overrides), dict(overrides)
    for r in sensitivity(model, base, output, overrides):
        item = model.inputs[r["input"]]
        lo, hi = bounds(item, float(base[r["input"]]))
        if lo is None or hi is None or r["input"] in overrides:
            continue
        up_helps = ((r["output_at_high"] or 0) > (r["output_at_low"] or 0)) == hib
        best[r["input"]] = {"value": hi if up_helps else lo}
        worst[r["input"]] = {"value": lo if up_helps else hi}
    res = {}
    for label, ov in (("worst", worst), ("base", overrides), ("best", best)):
        vals, _ = model.evaluate(base, ov)
        res[label] = {"value": _num(vals[output]),
                      "assumptions": {k: v["value"] for k, v in ov.items() if isinstance(v, dict) and "value" in v
                                      and k in model.inputs}}
    return {"output": output, **res}


# ----------------------------------------------------------------------------- natural-language what-if

_UP = r"(?:increase|increases|rise|rises|grow|grows|go(?:es)? up|up)"
_DOWN = r"(?:decrease|decreases|fall|falls|drop|drops|decline|declines|go(?:es)? down|down|shrink|shrinks|cut)"
_CHANGE = re.compile(rf"(?P<subj>[a-z][a-z \-]*?)\s+(?:(?:were|was|is|are)\s+)?(?:to\s+)?(?P<dir>{_UP}|{_DOWN})\s+(?:by\s+)?"
                     r"(?P<num>\d+(?:\.\d+)?)\s*(?:%|percent|pct)", re.I)
_TARGET = re.compile(r"(?:happens?|happen) to (?:the |our )?(?P<t>[a-z][a-z \-]*?)(?:\s+if\b|\s+when\b|\?|$)", re.I)


def _match(model: Model, phrase: str) -> str | None:
    words = re.sub(r"[^a-z ]", " ", phrase.lower()).split()
    stop = {"the", "our", "and", "while", "if", "when", "what", "costs", "cost", "s"}
    best, score = None, 0.0
    for n in model.names:
        cands = {n.replace("_", " "), model.label(n).lower()}
        for item in (model.spec.get("inputs") or []) + (model.spec.get("formulas") or []):
            if item["name"] == n:
                cands |= {s.lower() for s in item.get("synonyms", [])}
        for cand in cands:
            if cand in phrase.lower():
                s = 1.0 + len(cand) / 100
            else:
                cw = set(cand.split()) - stop
                s = len(cw & set(words)) / len(cw) if cw else 0
            if s > score:
                best, score = n, s
    return best if score >= 0.5 else None


def parse_what_if(model: Model, question: str) -> dict[str, Any]:
    """Changes and target output from a sentence such as "What happens to operating margin if labor costs increase
    6% while revenue grows 3%?". Unmatched phrases are returned, never guessed."""
    q = question.replace("’", "'")
    overrides, unmatched = {}, []
    tail = q.split(" if ", 1)[-1] if " if " in q.lower() else q
    for m in _CHANGE.finditer(tail):
        subj = re.sub(r"^(?:and|while|with|but|if|when)\s+", "", m.group("subj").strip(), flags=re.I)
        node = _match(model, subj)
        pct = float(m.group("num")) * (1 if re.fullmatch(_UP, m.group("dir"), re.I) else -1)
        if node:
            overrides[node] = {"change_pct": pct}
        else:
            unmatched.append(subj)
    target = None
    if m := _TARGET.search(q):
        target = _match(model, m.group("t"))
    return {"overrides": overrides, "target": target or model.outputs[0]["name"], "unmatched": unmatched}


WHAT_IF = re.compile(r"\bwhat(?:'s| is| would)? (?:happens?|happen|be the (?:impact|effect))\b|\bwhat[- ]if\b|"
                     r"\bif\b[^?]*\b(?:increase|grow|rise|fall|drop|decrease|decline|go up|go down|cut)s?\b[^?]*\d+(?:\.\d+)?\s*(?:%|percent)",
                     re.I)


def wants_what_if(text: str) -> bool:
    return bool(WHAT_IF.search(text)) and bool(_CHANGE.search(text))


def _fmt(v: float | None, kind: str | None) -> str:
    if v is None:
        return "n/a"
    if kind == "percent":
        return f"{v * 100:.2f}%"
    if kind == "currency":
        a = abs(v)
        s = f"${a / 1e9:.2f}B" if a >= 1e9 else f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.1f}K" if a >= 1e3 else f"${a:,.0f}"
        return ("-" if v < 0 else "") + s
    return f"{v:,.4g}"


def summary(result: dict[str, Any]) -> str:
    """One plain sentence per output, the changes assumed, and the hypothetical label."""
    parts = []
    for o in result["outputs"]:
        if o["delta"] is None:
            parts.append(f"{o['label']} cannot be computed under this scenario")
            continue
        if o.get("format") == "percent":
            move = f"{o['delta'] * 100:+.2f} points"
        else:
            move = f"{_fmt(o['delta'], o.get('format'))}" + (f", {o['delta_pct']:+.1f}%" if o["delta_pct"] is not None else "")
            move = ("+" if o["delta"] > 0 else "") + move
        parts.append(f"{o['label']} would go from {_fmt(o['base'], o.get('format'))} to "
                     f"{_fmt(o['scenario'], o.get('format'))} ({move})")
    changes = ", ".join(f"{k.replace('_', ' ')} {v['change_pct']:+g}%" if isinstance(v, dict) and "change_pct" in v
                        else f"{k.replace('_', ' ')} = {v.get('value') if isinstance(v, dict) else v}"
                        for k, v in result["overrides"].items())
    return "; ".join(parts) + f". Assumed: {changes}. These are hypothetical figures, not observed results."


# ----------------------------------------------------------------------------- service


class ScenarioService:
    """Stores models and saved scenarios in the workspace, resolves observed inputs through the SQL guard."""

    def __init__(self, stores, guard, data, semantic) -> None:
        self.stores, self.guard, self.data, self.semantic = stores, guard, data, semantic
        self._observed: dict[tuple, tuple[float, dict[str, Any]]] = {}

    # -- models ---------------------------------------------------------------------------

    def define(self, actor: Principal, ws: str, name: str, spec: dict[str, Any], *, note: str = "") -> dict[str, Any]:
        self.guard.require(actor, ws, "scenario.define")
        if not NAME.match(name):
            raise ModelError("model name must be lower_snake_case")
        Model(spec)  # validates
        db = self.stores.workspace(ws)

        def tx(c):
            v = c.execute("SELECT coalesce(max(version), 0) FROM scenario_models WHERE name = ?", [name]).fetchone()[0] + 1
            c.execute("INSERT INTO scenario_models (name, version, title, spec, note, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                      [name, v, spec.get("title") or name.replace("_", " ").capitalize(), json.dumps(spec), note,
                       actor.user_id])
            return v

        version = db.write(tx)
        self.guard.audit(actor, ws, "scenario.define", f"{name}@{version}", "ok")
        return {"name": name, "version": version}

    def models(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        rows = self.stores.workspace(ws).query(
            "SELECT name, max(version) AS version, arg_max(title, version) AS title, arg_max(spec, version) AS spec "
            "FROM scenario_models GROUP BY name ORDER BY name")
        out = []
        for r in rows:
            spec = json.loads(r["spec"])
            outputs = [o if isinstance(o, str) else o["name"] for o in spec.get("outputs", [])]
            out.append({"name": r["name"], "version": r["version"], "title": r["title"], "outputs": outputs,
                        "metric": spec.get("metric") or outputs[0],
                        "inputs": [{"name": i["name"], "label": i.get("label") or i["name"].replace("_", " "),
                                    "unit": i.get("unit"), "observed": "source" in i,
                                    "value": i.get("value"), "low": i.get("low"), "high": i.get("high"),
                                    "range_pct": i.get("range_pct"),
                                    "source": i.get("source")} for i in spec.get("inputs", [])]})
        return out

    def for_metric(self, actor: Principal, ws: str, metric: str) -> list[str]:
        """Driver models whose headline metric is ``metric`` (for an "Explore scenarios" action on an answer)."""
        return [m["name"] for m in self.models(actor, ws) if m["metric"] == metric or metric in m["outputs"]]

    def get(self, actor: Principal, ws: str, name: str, version: int | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        db = self.stores.workspace(ws)
        sql = "SELECT * FROM scenario_models WHERE name = ? " + ("AND version = ?" if version else "ORDER BY version DESC LIMIT 1")
        rows = db.query(sql, [name, version] if version else [name])
        if not rows:
            raise KeyError(f"scenario model {name}")
        r = rows[0]
        return {**r, "spec": json.loads(r["spec"])}

    # -- observed inputs --------------------------------------------------------------------

    def _resolve(self, actor: Principal, ws: str, item: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        from .analyst import periods  # noqa: PLC0415
        from .semantic import MetricSpec, compile_metric  # noqa: PLC0415

        src = item["source"]
        ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
        if "metric" in src:
            spec: MetricSpec = self.semantic.get(actor, ws, src["metric"])
            dataset, tcol = spec.dataset, spec.time_column
        else:
            dataset, tcol = src["dataset"], src.get("time_column")
            if not all(ident.match(x or "") for x in (dataset, src["column"])) or (tcol and not ident.match(tcol)):
                raise ModelError("dataset, column and time column must be plain identifiers")
            if src.get("agg", "sum") not in ("sum", "avg", "min", "max", "count"):
                raise ModelError("agg must be sum, avg, min, max or count")
        ver = self.stores.workspace(ws).scalar("SELECT current_version FROM datasets WHERE name = ?", [dataset])
        key = (ws, actor.user_id, json.dumps(src, sort_keys=True), ver)
        if key in self._observed:
            return self._observed[key]
        period, through = None, None
        if tcol:
            mx = self.data.query(actor, ws, f'SELECT max("{tcol}") FROM "{dataset}"').rows[0][0]
            through = mx.isoformat() if hasattr(mx, "isoformat") else str(mx)
            if src.get("period"):
                p = periods.parse(src["period"], mx if isinstance(mx, date) else date.fromisoformat(str(mx)[:10]))
                if p is None:
                    raise ModelError(f"period {src['period']!r} is not understood")
                period = {"label": p.label, "start": p.start.isoformat(), "end": p.end.isoformat()}
        if "metric" in src:
            sql = compile_metric(spec, start=period["start"] if period else None, end=period["end"] if period else None)
        else:
            where = f' WHERE "{tcol}" >= \'{period["start"]}\' AND "{tcol}" < \'{period["end"]}\'' if period else ""
            sql = f'SELECT {src.get("agg", "sum")}("{src["column"]}") FROM "{dataset}"{where}'
        r = self.data.query(actor, ws, sql)
        if not r.rows or r.rows[0][0] is None:
            raise ModelError(f"{item['name']}: the data returned no value for {period['label'] if period else 'all rows'}")
        meta = {"observed": True, "dataset": dataset, "dataset_version": ver, "period": period, "data_through": through,
                "sql": r.sql, "query_id": r.query_id}
        out = (float(r.rows[0][0]), meta)
        self._observed[key] = out
        return out

    def base(self, actor: Principal, ws: str, model: Model) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
        base, observed = {}, {}
        for n, item in model.inputs.items():
            if "source" in item:
                base[n], observed[n] = self._resolve(actor, ws, item)
            else:
                base[n] = float(item["value"])
        return base, observed

    # -- analysis ---------------------------------------------------------------------------

    def _load(self, actor, ws, name, version=None) -> tuple[dict[str, Any], Model]:
        row = self.get(actor, ws, name, version)
        return row, Model(row["spec"])

    def evaluate(self, actor: Principal, ws: str, name: str, overrides: dict[str, Any] | None = None, *,
                 version: int | None = None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        """What-if against the observed base. With ``previous`` (an earlier result of the same model version) only the
        nodes downstream of changed overrides are recomputed."""
        self.guard.require(actor, ws, "data.query")
        row, model = self._load(actor, ws, name, version)
        overrides = overrides or {}
        base, observed = self.base(actor, ws, model)
        base_values, _ = model.evaluate(base)
        prev_vals, changed = None, None
        if previous and previous.get("model") == {"name": name, "version": row["version"]}:
            before = previous.get("overrides") or {}
            changed = {k for k in set(before) | set(overrides) if before.get(k) != overrides.get(k)}
            prev_vals = previous.get("values")
        values, computed = model.evaluate(base, overrides, previous=prev_vals, changed=changed)
        return {
            "kind": "hypothetical",
            "note": "Scenario values are hypothetical: computed from the observed base and the stated changes, not observed results.",
            "model": {"name": name, "version": row["version"]},
            "overrides": overrides,
            "observed": {n: {"value": base[n], **observed[n]} for n in observed},
            "assumptions": {n: dict(zip(("value", "low", "high"), (float(i["value"]), *bounds(i, float(i["value"]))), strict=True))
                            for n, i in model.inputs.items() if "source" not in i},
            "values": {n: _num(v) for n, v in values.items()},
            "base_values": {n: _num(v) for n, v in base_values.items()},
            "outputs": compare(model, base_values, values),
            "recomputed": computed,
            "formulas": {n: e for n, (_t, _r, e) in model.formulas.items()},
            "validation": validate(model, base, values, overrides, observed),
        }

    def analyze(self, actor: Principal, ws: str, name: str, overrides: dict[str, Any] | None = None, *,
                output: str | None = None, draws: int = 10_000, seed: int = 7, version: int | None = None,
                thresholds: dict[str, float] | None = None) -> dict[str, Any]:
        """Sensitivity, best/base/worst and (when inputs declare distributions) Monte Carlo for one output."""
        self.guard.require(actor, ws, "data.query")
        row, model = self._load(actor, ws, name, version)
        base, _ = self.base(actor, ws, model)
        output = output or model.outputs[0]["name"]
        if output not in model.names:
            raise ModelError(f"{output} is not part of this model")
        res = {"kind": "hypothetical", "model": {"name": name, "version": row["version"]}, "output": output,
               "sensitivity": sensitivity(model, base, output, overrides), "cases": cases(model, base, output, overrides)}
        if any(i.get("distribution") for i in model.inputs.values()):
            res["simulation"] = simulate(model, base, overrides, n=draws, seed=seed, thresholds=thresholds)
        return res

    def match(self, actor: Principal, ws: str, question: str) -> str | None:
        """The workspace model that understands the changes in ``question`` best, or None."""
        best = None
        for m in self.models(actor, ws):
            parsed = parse_what_if(Model(self.get(actor, ws, m["name"])["spec"]), question)
            score = len(parsed["overrides"]) - len(parsed["unmatched"])
            if parsed["overrides"] and (best is None or score > best[0]):
                best = (score, m["name"])
        return best[1] if best else None

    def ask(self, actor: Principal, ws: str, question: str, name: str | None = None) -> dict[str, Any]:
        """Answer a what-if sentence against the named model, or the model whose nodes best match the sentence."""
        candidates = [name] if name else [m["name"] for m in self.models(actor, ws)]
        best = None
        for n in candidates:
            _row, model = self._load(actor, ws, n)
            parsed = parse_what_if(model, question)
            score = len(parsed["overrides"]) - len(parsed["unmatched"])
            if parsed["overrides"] and (best is None or score > best[0]):
                best = (score, n, parsed)
        if best is None:
            raise ModelError("no scenario model in this workspace matches the changes in that question")
        _s, n, parsed = best
        result = self.evaluate(actor, ws, n, parsed["overrides"])
        result["question"], result["target"], result["unmatched"] = question, parsed["target"], parsed["unmatched"]
        result["summary"] = summary(result)
        return result

    # -- saved scenarios ------------------------------------------------------------------

    def save(self, actor: Principal, ws: str, name: str, scenario: str, overrides: dict[str, Any], *,
             note: str = "", seed: int = 7) -> dict[str, Any]:
        """Save a named scenario as a new version, with its result and a digest that lets anyone reproduce it."""
        self.guard.require(actor, ws, "scenario.define")
        result = self.evaluate(actor, ws, name, overrides)
        digest = hashlib.sha256(json.dumps({"model": result["model"], "overrides": overrides,
                                            "observed": {k: v["value"] for k, v in result["observed"].items()}},
                                           sort_keys=True).encode()).hexdigest()[:16]
        db = self.stores.workspace(ws)

        def tx(c):
            v = c.execute("SELECT coalesce(max(version), 0) FROM scenarios WHERE model = ? AND name = ?",
                          [name, scenario]).fetchone()[0] + 1
            sid = "sc_" + uuid.uuid4().hex[:10]
            c.execute("INSERT INTO scenarios (id, model, model_version, name, version, overrides, result, digest, seed, note, "
                      "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [sid, name, result["model"]["version"], scenario, v, json.dumps(overrides), json.dumps(result),
                       digest, seed, note, actor.user_id])
            return sid, v

        sid, v = db.write(tx)
        self.guard.audit(actor, ws, "scenario.save", sid, "ok", {"model": name, "scenario": scenario, "version": v})
        return {"id": sid, "name": scenario, "version": v, "digest": digest, "result": result}

    def saved(self, actor: Principal, ws: str, name: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        return self.stores.workspace(ws).query(
            "SELECT id, name, version, model_version, overrides, digest, note, created_by, created_at FROM scenarios "
            "WHERE model = ? ORDER BY name, version", [name])

    def reproduce(self, actor: Principal, ws: str, scenario_id: str) -> dict[str, Any]:
        """Re-run a saved scenario on its model version. ``same`` says whether the outputs match the saved ones; if
        the observed data moved since, the difference is reported rather than hidden."""
        self.guard.require(actor, ws, "workspace.read")
        rows = self.stores.workspace(ws).query("SELECT * FROM scenarios WHERE id = ?", [scenario_id])
        if not rows:
            raise KeyError(scenario_id)
        s = rows[0]
        saved = json.loads(s["result"])
        now = self.evaluate(actor, ws, s["model"], json.loads(s["overrides"]), version=s["model_version"])
        diffs = []
        for a, b in zip(saved["outputs"], now["outputs"], strict=True):
            if (a["scenario"] is None) != (b["scenario"] is None) or (
                    a["scenario"] is not None and not math.isclose(a["scenario"], b["scenario"], rel_tol=1e-9, abs_tol=1e-12)):
                diffs.append({"output": a["name"], "saved": a["scenario"], "now": b["scenario"]})
        moved = [n for n in saved["observed"] if not math.isclose(saved["observed"][n]["value"], now["observed"][n]["value"],
                                                                  rel_tol=1e-12)]
        return {"id": scenario_id, "same": not diffs, "differences": diffs, "observed_changed": moved, "result": now}

    def compare_saved(self, actor: Principal, ws: str, ids: list[str]) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        db = self.stores.workspace(ws)
        out = []
        for sid in ids:
            rows = db.query("SELECT id, model, name, version, result FROM scenarios WHERE id = ?", [sid])
            if not rows:
                raise KeyError(sid)
            r = json.loads(rows[0]["result"])
            out.append({"id": sid, "name": rows[0]["name"], "version": rows[0]["version"], "model": rows[0]["model"],
                        "outputs": {o["name"]: o["scenario"] for o in r["outputs"]}})
        if len({o["model"] for o in out}) > 1:
            raise ModelError("compare scenarios of the same model")
        return {"kind": "hypothetical", "scenarios": out}


__all__ = ["Model", "ModelError", "ScenarioService", "cases", "compare", "parse_what_if", "sensitivity", "simulate",
           "summary", "validate", "wants_what_if"]
