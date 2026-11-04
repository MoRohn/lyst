# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Experiment registry: compare analytical methods on the workspace's own data, honestly, and keep the record.

An experiment states a question ("which forecast method should we trust for monthly revenue?"), a kind and a spec.
Running it records one run per method with its inputs (metric version, dataset versions, the exact series), the
environment it ran on, its scores and a digest of each, so any run can be reproduced and a different result traced
to changed data, a changed environment or a changed method.

Kinds:

* ``forecast``: rolling-origin backtests of several forecasting methods on an approved metric. Scores per method:
  MAE, RMSE, MAPE, sMAPE, MASE (against the seasonal naive forecast) , bias and interval coverage. The comparison
  names a winner only when it beats the runner-up on paired errors (Wilcoxon signed-rank, 5%); otherwise it says
  there is no clear winner and recommends the simpler method.
* ``eval``: the evaluation bank run as an experiment arm (one run per configuration, such as before and after a
  model change), compared with ``lyst.evals.compare`` when the regression gate is installed.

``decide`` records the chosen method with a note; it is advice for people and for the analyst, not a switch.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
import warnings
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from . import environment
from .analytics import timeseries as ts
from .security import PERMISSIONS, Guard, Principal
from .store import Stores

PERMISSIONS.setdefault("experiment.run", "editor")
PERMISSIONS.setdefault("experiment.decide", "admin")

# simplest first: when two methods are within noise, the earlier one is the safer default
COMPLEXITY = ["naive", "seasonal_naive", "mean", "drift", "linear_trend", "ets"]
DEFAULT_METHOD = "ets"  # what the analyst's forecast step uses for series of eight points or more
METRICS = ("mase", "mae", "rmse", "mape", "smape", "bias", "coverage")


def _interval(base: np.ndarray, resid: np.ndarray, horizon: int, level: float, *, grow: bool = True) -> tuple:
    from scipy.stats import norm  # noqa: PLC0415

    z = norm.ppf(0.5 + level / 2)
    sd = float(np.std(resid)) if len(resid) > 1 else 0.0
    width = z * sd * (np.sqrt(np.arange(1, horizon + 1)) if grow else np.ones(horizon))
    return base, base - width, base + width


def _naive(y: pd.Series, h: int, sp: int, level: float):
    v = y.to_numpy(float)
    return _interval(np.repeat(v[-1], h), np.diff(v), h, level)


def _seasonal_naive(y: pd.Series, h: int, sp: int, level: float):
    yhat, lo, hi, _ = ts._snaive(y, h, sp, 1 - level)  # noqa: SLF001 - the analyst's own baseline, reused
    return yhat, lo, hi


def _mean(y: pd.Series, h: int, sp: int, level: float):
    v = y.to_numpy(float)
    return _interval(np.repeat(v.mean(), h), v - v.mean(), h, level, grow=False)


def _drift(y: pd.Series, h: int, sp: int, level: float):
    v = y.to_numpy(float)
    slope = (v[-1] - v[0]) / max(len(v) - 1, 1)
    base = v[-1] + slope * np.arange(1, h + 1)
    return _interval(base, np.diff(v) - slope, h, level)


def _linear_trend(y: pd.Series, h: int, sp: int, level: float):
    v = y.to_numpy(float)
    t = np.arange(len(v))
    b, a = np.polyfit(t, v, 1)
    base = a + b * np.arange(len(v), len(v) + h)
    return _interval(base, v - (a + b * t), h, level, grow=False)


def _ets(y: pd.Series, h: int, sp: int, level: float):
    yhat, lo, hi, _ = ts._ets(y, h, sp, 1 - level)  # noqa: SLF001
    return yhat, lo, hi


METHODS: dict[str, Callable] = {"naive": _naive, "seasonal_naive": _seasonal_naive, "mean": _mean, "drift": _drift,
                                "linear_trend": _linear_trend, "ets": _ets}


def backtest(y: pd.Series, method: str, *, horizon: int, folds: int, sp: int, level: float = 0.8) -> dict[str, Any]:
    """Rolling-origin evaluation: refit at each origin, forecast ``horizon`` ahead, score against what happened."""
    fn = METHODS[method]
    n = len(y)
    step = max(1, horizon // 2)
    origins = sorted({n - horizon - i * step for i in range(folds)})
    min_train = max(8, 2 * sp + 2) if method == "ets" else max(3, sp + 1 if method == "seasonal_naive" else 3)
    origins = [o for o in origins if o >= min_train]
    if not origins:
        raise ValueError(f"the series has {n} points; {method} needs at least {min_train + horizon} for one backtest")
    errors, abs_pct, sym, inside, per_fold = [], [], [], [], []
    for o in origins:
        train, test = y.iloc[:o], y.iloc[o:o + horizon].to_numpy(float)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                yhat, lo, hi = fn(train, len(test), sp, level)
            except Exception as exc:  # noqa: BLE001 - a method that cannot fit is a result, recorded as such
                raise ValueError(f"{method} could not be fitted at origin {o}: {exc}") from exc
        yhat, lo, hi = (np.asarray(a, float)[: len(test)] for a in (yhat, lo, hi))
        e = test - yhat
        errors.extend(e.tolist())
        nz = test != 0
        abs_pct.extend((np.abs(e[nz] / test[nz])).tolist())
        denom = np.abs(test) + np.abs(yhat)
        sym.extend((2 * np.abs(e)[denom > 0] / denom[denom > 0]).tolist())
        inside.extend(((test >= lo) & (test <= hi)).tolist())
        per_fold.append({"origin": str(y.index[o].date()) if hasattr(y.index[o], "date") else str(y.index[o]),
                         "mae": float(np.mean(np.abs(e))), "points": int(len(test))})
    first_train = y.iloc[:origins[0]].to_numpy(float)
    k = sp if len(first_train) > sp else 1
    scale = float(np.mean(np.abs(first_train[k:] - first_train[:-k]))) if len(first_train) > k else float("nan")
    err = np.array(errors)
    mae = float(np.mean(np.abs(err)))
    metrics = {"mae": mae, "rmse": float(np.sqrt(np.mean(err ** 2))), "bias": float(np.mean(err)),
               "mape": float(np.mean(abs_pct)) if abs_pct else None, "smape": float(np.mean(sym)) if sym else None,
               "mase": mae / scale if scale and scale == scale else None, "coverage": float(np.mean(inside)),
               "folds": len(origins), "points": int(len(err))}
    return {"metrics": metrics, "errors": [float(x) for x in err], "folds": per_fold}


def compare_methods(runs: list[dict[str, Any]], primary: str = "mase") -> dict[str, Any]:
    """Rank, then test the leader against each other method on paired absolute errors."""
    from scipy.stats import wilcoxon  # noqa: PLC0415

    ok = [r for r in runs if r["status"] == "ok" and r["metrics"].get(primary) is not None]
    if not ok:
        return {"ranking": [], "winner": None, "verdict": "no method produced a score"}
    ranked = sorted(ok, key=lambda r: (r["metrics"][primary], COMPLEXITY.index(r["method"]) if r["method"] in COMPLEXITY else 99))
    best = ranked[0]
    rows = []
    for r in ranked:
        row = {"method": r["method"], "run_id": r["id"], primary: r["metrics"][primary],
               "coverage": r["metrics"].get("coverage"), "mae": r["metrics"]["mae"]}
        if r is not best:
            a, b = np.abs(best["detail"]["errors"]), np.abs(r["detail"]["errors"])
            if len(a) == len(b) and len(a) >= 6 and np.any(a != b):
                p = float(wilcoxon(a, b, alternative="less").pvalue)
            else:
                p = None
            row["p_value_vs_leader"] = p
            row["leader_better"] = p is not None and p < 0.05
        rows.append(row)
    clear = len(rows) > 1 and rows[1].get("leader_better", False)
    if len(rows) == 1:
        verdict, winner = f"only {best['method']} produced a score", best["method"]
    elif clear:
        verdict, winner = (f"{best['method']} is best on {primary.upper()} and beats {rows[1]['method']} on paired errors "
                           f"(p={rows[1]['p_value_vs_leader']:.3f})"), best["method"]
    else:
        tied = [r["method"] for r in rows if not r.get("leader_better", False)]
        simplest = min(tied, key=lambda m: COMPLEXITY.index(m) if m in COMPLEXITY else 99)
        verdict = (f"no clear winner: {', '.join(tied)} are within noise of each other on {primary.upper()}; the simplest, "
                   f"{simplest}, is the safer choice")
        winner = simplest
    calibrated = [r["method"] for r in rows if r.get("coverage") is not None and abs(r["coverage"] - 0.8) <= 0.15]
    return {"primary": primary, "ranking": rows, "winner": winner, "clear": clear, "verdict": verdict,
            "default": DEFAULT_METHOD, "default_rank": next((i + 1 for i, r in enumerate(rows) if r["method"] == DEFAULT_METHOD), None),
            "well_calibrated": calibrated}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _round(m: dict[str, Any]) -> dict[str, Any]:
    return {k: (float(f"{v:.9g}") if isinstance(v, float) else v) for k, v in m.items()}


class ExperimentService:
    def __init__(self, stores: Stores, guard: Guard, data, semantic, lyst_ref: Callable[[], Any] | None = None) -> None:
        self.stores = stores
        self.guard = guard
        self.data = data
        self.semantic = semantic
        self.lyst_ref = lyst_ref  # the eval kind runs the whole engine

    # -- registry ---------------------------------------------------------------------------

    def create(self, actor: Principal, ws: str, *, name: str, kind: str, spec: dict[str, Any],
               question: str | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "experiment.run", name)
        if kind == "forecast":
            m = self.semantic.get(actor, ws, spec.get("metric", ""))
            if not m.time_column:
                raise ValueError(f"{m.name} has no time column, so it cannot be forecast")
            grain = spec.get("grain", "month")
            if grain not in ts.FREQ:
                raise ValueError(f"grain must be one of {', '.join(ts.FREQ)}")
            methods = spec.get("methods") or list(METHODS)
            unknown = [x for x in methods if x not in METHODS]
            if unknown:
                raise ValueError(f"unknown method {unknown[0]}; available: {', '.join(METHODS)}")
            spec = {"metric": m.name, "grain": grain, "horizon": int(spec.get("horizon", 3)), "folds": int(spec.get("folds", 6)),
                    "methods": methods, "level": float(spec.get("level", 0.8)), "primary": spec.get("primary", "mase"),
                    "fill_missing": spec.get("fill_missing", "error")}
            if spec["primary"] not in METRICS:
                raise ValueError(f"primary must be one of {', '.join(METRICS)}")
            if not 1 <= spec["horizon"] <= 36 or not 1 <= spec["folds"] <= 24:
                raise ValueError("horizon must be 1-36 and folds 1-24")
        elif kind == "eval":
            spec = {"cases": spec.get("cases") or "demo", "label": spec.get("label", "")}
        else:
            raise ValueError("kind must be forecast or eval")
        eid = "exp_" + uuid.uuid4().hex[:10]
        self.stores.workspace(ws).execute(
            "INSERT INTO experiments (id, name, kind, question, spec, status, created_by) VALUES (?, ?, ?, ?, ?, 'open', ?)",
            [eid, name, kind, question, json.dumps(spec), actor.user_id])
        self.guard.audit(actor, ws, "experiment.create", eid, "ok", {"kind": kind, "name": name})
        return self.get(actor, ws, eid)

    def get(self, actor: Principal, ws: str, eid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read", eid)
        rows = self.stores.workspace(ws).query("SELECT * FROM experiments WHERE id = ?", [eid])
        if not rows:
            raise KeyError(eid)
        e = rows[0]
        e["spec"] = json.loads(e["spec"])
        e["decision"] = json.loads(e["decision"]) if e["decision"] else None
        e["runs"] = self.runs(actor, ws, eid)
        return e

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        rows = self.stores.workspace(ws).query(
            "SELECT e.id, e.name, e.kind, e.question, e.status, e.decision, e.created_by, e.created_at, e.updated_at, "
            "count(r.id) AS runs FROM experiments e LEFT JOIN experiment_runs r ON r.experiment_id = e.id "
            "GROUP BY ALL ORDER BY e.updated_at DESC")
        for r in rows:
            r["decision"] = json.loads(r["decision"]) if r["decision"] else None
        return rows

    def runs(self, actor: Principal, ws: str, eid: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read", eid)
        rows = self.stores.workspace(ws).query(
            "SELECT * FROM experiment_runs WHERE experiment_id = ? ORDER BY created_at, method", [eid])
        for r in rows:
            for k in ("params", "metrics", "detail", "inputs", "environment"):
                r[k] = json.loads(r[k]) if r[k] else None
        return rows

    def run_detail(self, actor: Principal, ws: str, run_id: str) -> dict[str, Any]:
        rows = self.stores.workspace(ws).query("SELECT experiment_id FROM experiment_runs WHERE id = ?", [run_id])
        if not rows:
            raise KeyError(run_id)
        return next(r for r in self.runs(actor, ws, rows[0]["experiment_id"]) if r["id"] == run_id)

    # -- running ----------------------------------------------------------------------------

    def _series(self, actor: Principal, ws: str, spec: dict[str, Any]) -> tuple[pd.Series, dict[str, Any]]:
        from .semantic import compile_metric  # noqa: PLC0415

        m = self.semantic.get(actor, ws, spec["metric"])
        sql = compile_metric(m, grain=spec["grain"])
        r = self.data.query(actor, ws, sql, max_rows=10_000)
        frame = r.frame()
        if frame.empty:
            raise ValueError(f"{m.name} has no data to forecast")
        y = pd.Series(frame["value"].astype(float).to_numpy(), index=pd.to_datetime(frame["period"]).dt.tz_localize(None))
        full = pd.date_range(y.index.min(), y.index.max(), freq=ts.FREQ[spec["grain"]])
        y = y.reindex(full)
        gaps = int(y.isna().sum())
        if gaps:
            if spec.get("fill_missing") == "zero":
                y = y.fillna(0.0)
            else:
                raise ValueError(f"{m.name} has {gaps} missing {spec['grain']}s; set fill_missing to 'zero' if they mean none")
        versions = {d["name"]: d["version"] for d in self.data.catalog(actor, ws) if d["name"] in r.datasets}
        metric_version = self.stores.workspace(ws).scalar("SELECT current_version FROM metrics WHERE name = ?", [m.name])
        inputs = {"metric": m.name, "metric_version": metric_version, "datasets": versions, "sql": r.executed_sql,
                  "grain": spec["grain"], "series": {"start": str(y.index[0].date()), "values": [float(v) for v in y.to_numpy()]}}
        inputs["series_digest"] = _digest(inputs["series"])
        return y, inputs

    def run(self, actor: Principal, ws: str, eid: str) -> dict[str, Any]:
        """Run every arm of the experiment now and return the comparison."""
        self.guard.require(actor, ws, "experiment.run", eid)
        e = self.get(actor, ws, eid)
        if e["status"] == "decided":
            raise ValueError("this experiment is decided; create a new one to test again")
        batch = "b_" + uuid.uuid4().hex[:8]
        if e["kind"] == "forecast":
            spec = e["spec"]
            y, inputs = self._series(actor, ws, spec)
            for method in spec["methods"]:
                self._forecast_run(actor, ws, eid, method, spec, y, inputs, batch=batch)
        else:
            self._eval_run(actor, ws, eid, e["spec"], batch=batch)
        self.stores.workspace(ws).execute("UPDATE experiments SET updated_at = now(), status = 'run' WHERE id = ?", [eid])
        self.guard.audit(actor, ws, "experiment.run", eid, "ok", {"batch": batch, "kind": e["kind"]})
        return self.compare(actor, ws, eid)

    def _forecast_run(self, actor, ws, eid, method, spec, y, inputs, *, batch: str, reproduces: str | None = None) -> str:
        sp = ts._SEASON[spec["grain"]]  # noqa: SLF001
        params = {"horizon": spec["horizon"], "folds": spec["folds"], "level": spec["level"], "season": sp, "batch": batch}
        env = environment.fingerprint()
        rid = "run_" + uuid.uuid4().hex[:10]
        start = time.perf_counter()
        try:
            out = backtest(y, method, horizon=spec["horizon"], folds=spec["folds"], sp=sp, level=spec["level"])
            status, error = "ok", None
            metrics = _round(out["metrics"])
            detail = {"errors": out["errors"], "folds": out["folds"]}
        except ValueError as exc:
            status, error, metrics, detail = "failed", str(exc)[:300], None, None
        seconds = time.perf_counter() - start
        input_digest = _digest({"series": inputs["series_digest"], "method": method,
                                "params": {k: v for k, v in params.items() if k != "batch"}})
        self.stores.workspace(ws).execute(
            "INSERT INTO experiment_runs (id, experiment_id, method, params, status, metrics, detail, inputs, environment, "
            "input_digest, result_digest, seconds, error, reproduces, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [rid, eid, method, json.dumps(params), status, json.dumps(metrics) if metrics else None,
             json.dumps(detail) if detail else None, json.dumps(inputs), json.dumps(env), input_digest,
             _digest(metrics) if metrics else None, round(seconds, 4), error, reproduces, actor.user_id])
        return rid

    def _eval_run(self, actor, ws, eid, spec, *, batch: str) -> str:
        from . import evals  # noqa: PLC0415

        lyst = self.lyst_ref() if self.lyst_ref else None
        if lyst is None:
            raise ValueError("eval experiments need the running engine")
        bank = evals.demo_bank()
        if spec["cases"] != "demo":
            bank = [c for c in bank if c.id in set(spec["cases"])]
        env = environment.fingerprint()
        start = time.perf_counter()
        summary = evals.run(lyst, actor, ws, bank, label=spec.get("label") or batch)
        baseline_of = getattr(evals, "baseline_of", None)
        shaped = baseline_of(summary) if baseline_of else {
            "format": 1, "label": summary.get("label"), "model": summary.get("model"),
            "cases": {r["case"]: {"passed": r["passed"], "seconds": r["seconds"],
                                  "checks": {c["name"]: c["passed"] for c in r["checks"]}} for r in summary["results"]}}
        metrics = {"pass_rate": summary["pass_rate"], "passed": summary["passed"], "cases": summary["cases"],
                   "p95_seconds": summary["p95_seconds"]}
        rid = "run_" + uuid.uuid4().hex[:10]
        inputs = {"cases": [c.id for c in bank], "model": summary.get("model"), "eval_run": summary["run_id"]}
        self.stores.workspace(ws).execute(
            "INSERT INTO experiment_runs (id, experiment_id, method, params, status, metrics, detail, inputs, environment, "
            "input_digest, result_digest, seconds, created_by) VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?)",
            [rid, eid, summary.get("model") or "configured model", json.dumps({"batch": batch, "label": spec.get("label")}),
             json.dumps(metrics), json.dumps({"summary": shaped, "results": summary["results"]}, default=str), json.dumps(inputs),
             json.dumps(env),
             _digest(inputs["cases"]), _digest(shaped["cases"]), round(time.perf_counter() - start, 3), actor.user_id])
        return rid

    # -- comparing, reproducing, deciding ---------------------------------------------------

    def compare(self, actor: Principal, ws: str, eid: str, *, batch: str | None = None) -> dict[str, Any]:
        e = self.get(actor, ws, eid)
        runs = [r for r in e["runs"] if not r["reproduces"]]
        if not runs:
            return {"experiment": eid, "runs": 0, "verdict": "not run yet"}
        if e["kind"] == "forecast":
            batch = batch or runs[-1]["params"]["batch"]
            arm = [r for r in runs if r["params"].get("batch") == batch]
            out = compare_methods(arm, e["spec"]["primary"])
            out["failed"] = [{"method": r["method"], "error": r["error"]} for r in arm if r["status"] != "ok"]
            out["inputs"] = {k: v for k, v in arm[0]["inputs"].items() if k != "series"}
            out["points"] = len(arm[0]["inputs"]["series"]["values"])
            return {"experiment": eid, "batch": batch, **out}
        # eval: the latest run against the first (the baseline arm)
        from . import evals  # noqa: PLC0415

        base, cur = runs[0], runs[-1]
        cmp = getattr(evals, "compare", None)
        if cmp and base is not cur and "results" in cur["detail"]:
            # compare(run summary with its results, baseline-shaped earlier run): the regression gate's own contract
            gate = cmp({"results": cur["detail"]["results"]}, base["detail"]["summary"])
        else:
            bc, cc = base["detail"]["summary"]["cases"], cur["detail"]["summary"]["cases"]
            gate = {"regressions": [{"case": k, "kind": "accuracy"} for k in cc if k in bc and bc[k]["passed"] and not cc[k]["passed"]],
                    "improvements": [k for k in cc if k in bc and not bc[k]["passed"] and cc[k]["passed"]]}
            gate["ok"] = not gate["regressions"]
        return {"experiment": eid, "baseline": base["id"], "current": cur["id"], "baseline_pass_rate": base["metrics"]["pass_rate"],
                "current_pass_rate": cur["metrics"]["pass_rate"], "gate": gate,
                "verdict": "no regressions" if gate.get("ok") else f"{len(gate.get('regressions', []))} regression(s)"}

    def reproduce(self, actor: Principal, ws: str, run_id: str) -> dict[str, Any]:
        """Recompute a run on its recorded inputs. Returns ``{same, differences, observed_changed, environment}``:
        ``same`` when every score matches; ``observed_changed`` when today's data no longer matches the recorded
        series (so a fresh run would differ for that reason alone)."""
        self.guard.require(actor, ws, "experiment.run", run_id)
        r = self.run_detail(actor, ws, run_id)
        e = self.get(actor, ws, r["experiment_id"])
        if e["kind"] != "forecast":
            raise ValueError("eval runs call a model and cannot be recomputed exactly; run the experiment again to compare")
        rec = r["inputs"]["series"]
        y = pd.Series(rec["values"], index=pd.date_range(rec["start"], periods=len(rec["values"]), freq=ts.FREQ[r["inputs"]["grain"]]))
        spec = {**e["spec"], "horizon": r["params"]["horizon"], "folds": r["params"]["folds"], "level": r["params"]["level"]}
        rid = self._forecast_run(actor, ws, e["id"], r["method"], spec, y, r["inputs"], batch=r["params"]["batch"], reproduces=run_id)
        new = self.run_detail(actor, ws, rid)
        differences = []
        for k in sorted(set(r["metrics"] or {}) | set(new["metrics"] or {})):
            a, b = (r["metrics"] or {}).get(k), (new["metrics"] or {}).get(k)
            if a != b and not (isinstance(a, float) and isinstance(b, float) and abs(a - b) <= 1e-9 * max(1.0, abs(a))):
                differences.append({"metric": k, "was": a, "now": b})
        try:
            _, now_inputs = self._series(actor, ws, e["spec"])
            observed_changed = now_inputs["series_digest"] != r["inputs"]["series_digest"]
        except ValueError:
            observed_changed = True
        out = {"run": run_id, "reproduction": rid, "same": not differences and new["status"] == r["status"],
               "differences": differences, "observed_changed": observed_changed,
               "environment": environment.diff(r["environment"], new["environment"])}
        self.guard.audit(actor, ws, "experiment.reproduce", run_id, "ok", {"same": out["same"], "observed_changed": observed_changed})
        return out

    def decide(self, actor: Principal, ws: str, eid: str, *, choice: str, note: str = "") -> dict[str, Any]:
        self.guard.require(actor, ws, "experiment.decide", eid)
        e = self.get(actor, ws, eid)
        if not e["runs"]:
            raise ValueError("run the experiment before deciding")
        arms = {r["method"] for r in e["runs"]}
        if choice not in arms:
            raise ValueError(f"{choice} is not an arm of this experiment; arms: {', '.join(sorted(arms))}")
        cmp = self.compare(actor, ws, eid)
        decision = {"choice": choice, "note": note, "by": actor.user_id, "evidence": {k: cmp.get(k) for k in ("verdict", "winner", "clear", "batch")},
                    "against_evidence": e["kind"] == "forecast" and cmp.get("winner") not in (None, choice)}
        self.stores.workspace(ws).execute("UPDATE experiments SET decision = ?, status = 'decided', updated_at = now() WHERE id = ?",
                                          [json.dumps(decision, default=str), eid])
        self.guard.audit(actor, ws, "experiment.decide", eid, "ok", {"choice": choice, "against_evidence": decision["against_evidence"]})
        return self.get(actor, ws, eid)


__all__ = ["METHODS", "ExperimentService", "backtest", "compare_methods"]
