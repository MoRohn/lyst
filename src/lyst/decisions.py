# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Decision support: framing, multi-criteria comparison under uncertainty, allocation, and reviewable decision records.

Lyst helps a person decide; it does not decide for them. A decision record moves draft → analyzed → submitted
(an approval request to the workspace's admins) → approved or rejected by a person, then is reviewed against the
outcome it predicted. Nothing here executes an action.

* **Framing** (:func:`frame`) turns a sentence into the structure a decision needs (objective, alternatives,
  constraints, criteria, assumptions, required evidence) and lists what is still missing. It also says whether the
  sentence is an analytical question or a decision that needs someone's authorization.
* **Comparison** (:func:`analyze`): alternatives scored on weighted criteria (min-max normalized, direction-aware);
  constraints remove infeasible alternatives with the reason; a value may be uncertain (``{"low", "base", "high"}``,
  triangular), in which case a seeded Monte Carlo gives each alternative's probability of ranking first and its score
  interval; weight sensitivity finds, per criterion, the weight at which the top choice changes; cost-benefit and a
  risk-adjusted score are included when the criteria allow.
* **Allocation** (:func:`allocate`): choose or size opportunities under a budget with SciPy's mixed-integer solver
  (``scipy.optimize.milp``, HiGHS), optional minimum and maximum per item and mutually exclusive groups.
* **Recommendation contract** (:func:`recommendation`): every recommendation carries supporting evidence, key
  assumptions, expected impact, uncertainty, alternatives, risks, implementation considerations and monitoring.
  When the top two are not separated beyond the uncertainty, or evidence is missing, the recommendation says so
  ("no clear winner" / "insufficient evidence") instead of naming a winner with false certainty.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from .security import Principal

STATUSES = ("draft", "analyzed", "submitted", "approved", "rejected", "reviewed")
CONSEQUENTIAL = re.compile(r"\b(should we|decide|choose|select|approve|cut|close|hire|fire|lay ?off|invest|divest|"
                           r"launch|cancel|terminate|reduce headcount|allocate|which (?:option|alternative))\b", re.I)


class DecisionError(ValueError):
    pass


# ----------------------------------------------------------------------------- framing

_ALTS = re.compile(r"(?:between|among|options?:?|alternatives?:?)\s+(?P<list>[^?.;]+)", re.I)
_CONSTRAINT = re.compile(r"(?:within|under|no more than|at most|without exceeding|budget of|limit of)\s+"
                         r"(?P<v>\$?\s?[\d.,]+\s*(?:k|m|million|thousand|%)?)", re.I)
_OBJECTIVE = re.compile(r"\b(reduce|cut|lower|increase|grow|improve|maximi[sz]e|minimi[sz]e)\s+(?P<what>[a-z][a-z \-]{2,40}?)"
                        r"(?=\s+(?:by|while|without|within|under|between|among|in|for|across)\b|[?.,;]|$)", re.I)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def frame(question: str) -> dict[str, Any]:
    """The decision structure a sentence implies, and what is missing before it can be analyzed."""
    q = " ".join(question.split())
    alternatives: list[str] = []
    if m := _ALTS.search(q):
        parts = re.split(r",\s*|\s+or\s+|\s+and\s+", m.group("list"))
        parts = [re.split(r"\s+(?:to|in order to|so that|within|under|without|by)\s+", p, maxsplit=1)[0] for p in parts]
        alternatives = [p.strip(" .") for p in parts if p.strip(" .")]
    objective = None
    if m := _OBJECTIVE.search(q):
        objective = f"{m.group(1).lower()} {m.group('what').strip()}"
    constraints = [c.group("v").strip() for c in _CONSTRAINT.finditer(q)]
    targets = _PCT.findall(q)
    consequential = bool(CONSEQUENTIAL.search(q))
    missing = []
    if not objective:
        missing.append("the objective (what should improve, and how it is measured)")
    if len(alternatives) < 2:
        missing.append("at least two alternatives to compare (doing nothing counts)")
    missing += ["criteria with weights (cost, impact, risk, time to value…)",
                "evidence for each alternative's expected values (data, quotes, benchmarks)"]
    return {
        "question": q,
        "kind": "decision" if consequential else "analysis",
        "requires_authorization": consequential,
        "objective": objective,
        "alternatives": alternatives,
        "constraints": constraints,
        "targets_pct": [float(t) for t in targets],
        "suggested_criteria": [
            {"name": "cost", "direction": "min", "weight": 0.3},
            {"name": "impact", "direction": "max", "weight": 0.4},
            {"name": "risk", "direction": "min", "weight": 0.2},
            {"name": "time_to_value", "direction": "min", "weight": 0.1},
        ],
        "required_evidence": ["current baseline of the objective metric", "expected effect of each alternative with a range",
                              "cost of each alternative", "risks and their likelihood"],
        "missing": missing,
        "note": "A decision needs a person's authorization; Lyst prepares the comparison and records the choice." if consequential
        else "This reads as an analytical question; no decision record is needed unless an action follows.",
    }


# ----------------------------------------------------------------------------- comparison


def _triple(v: Any) -> tuple[float, float, float]:
    """A value as (low, likely, high): a number, {"low", "base" (or "likely"), "high"}, or [low, likely, high].
    Anything else is a DecisionError (a 400), never a crash."""
    try:
        if isinstance(v, dict):
            b = float(v["base"] if "base" in v else v["likely"])
            return float(v.get("low", b)), b, float(v.get("high", b))
        if isinstance(v, list | tuple):
            if len(v) == 3:
                return float(v[0]), float(v[1]), float(v[2])
            if len(v) == 1:
                f = float(v[0])
                return f, f, f
            raise DecisionError("a range is [low, likely, high]")
        if isinstance(v, bool):
            raise DecisionError("a value must be a number")
        f = float(v)
        return f, f, f
    except (TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, DecisionError):
            raise
        raise DecisionError(f"{v!r} is not a number, [low, likely, high] or {{low, base, high}}") from exc


def _validate_spec(spec: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    alts, crit = spec.get("alternatives") or [], spec.get("criteria") or []
    if len(alts) < 2:
        raise DecisionError("compare at least two alternatives (doing nothing counts as one)")
    if not crit:
        raise DecisionError("name at least one criterion")
    names = [a.get("name") for a in alts]
    if len(set(names)) != len(names) or not all(names):
        raise DecisionError("every alternative needs a distinct name")
    total = sum(float(c.get("weight", 0)) for c in crit)
    if total <= 0:
        raise DecisionError("criteria weights must add up to more than zero")
    for c in crit:
        if c.get("direction", "max") not in ("max", "min"):
            raise DecisionError(f"{c['name']}: direction is max or min")
        if float(c.get("weight", 0)) < 0:
            raise DecisionError(f"{c['name']}: weights cannot be negative")
        for a in alts:
            if c["name"] not in (a.get("values") or {}):
                raise DecisionError(f"{a['name']} has no value for {c['name']}")
            lo, b, hi = _triple(a["values"][c["name"]])
            if not lo <= b <= hi:
                raise DecisionError(f"{a['name']} / {c['name']}: low <= base <= high is required")
    return alts, [{**c, "weight": float(c.get("weight", 0)) / total} for c in crit]


def _scores(matrix: np.ndarray, crit: list[dict], weights: np.ndarray) -> np.ndarray:
    """matrix: (..., alternatives, criteria). Min-max normalized per criterion across alternatives, direction-aware."""
    lo = matrix.min(axis=-2, keepdims=True)
    hi = matrix.max(axis=-2, keepdims=True)
    span = np.where(hi - lo == 0, 1.0, hi - lo)
    norm = (matrix - lo) / span
    flip = np.array([c.get("direction", "max") == "min" for c in crit])
    norm = np.where(flip, 1 - norm, norm)
    norm = np.where(hi - lo == 0, 1.0, norm)  # a criterion that does not differ gives everyone full marks
    return (norm * weights).sum(axis=-1)


def _feasible(alt: dict, constraints: list[dict]) -> list[str]:
    reasons = []
    ops = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, ">": lambda a, b: a > b,
           "==": lambda a, b: a == b}
    for c in constraints:
        if c.get("op") not in ops:
            raise DecisionError("constraint op is one of <=, >=, <, >, ==")
        _lo, base, _hi = _triple(alt["values"].get(c["criterion"], float("nan")))
        if not ops[c["op"]](base, float(c["value"])):
            reasons.append(f"{c['criterion']} {base:,.4g} violates {c['op']} {float(c['value']):,.4g}")
    return reasons


def analyze(spec: dict[str, Any], *, draws: int = 5000, seed: int = 11) -> dict[str, Any]:
    """Weighted comparison with constraints, uncertainty, weight sensitivity, cost-benefit and risk adjustment."""
    alts, crit = _validate_spec(spec)
    constraints = spec.get("constraints") or []
    infeasible = {a["name"]: r for a in alts if (r := _feasible(a, constraints))}
    feasible = [a for a in alts if a["name"] not in infeasible]
    if len(feasible) == 0:
        return {"feasible": [], "infeasible": infeasible, "ranking": [],
                "summary": "No alternative meets every constraint.", "criteria": crit}
    names = [a["name"] for a in feasible]
    w = np.array([c["weight"] for c in crit])
    trip = np.array([[_triple(a["values"][c["name"]]) for c in crit] for a in feasible])  # (A, C, 3)
    base_m = trip[:, :, 1]
    base_scores = _scores(base_m, crit, w) if len(feasible) > 1 else np.ones(1)
    order = list(np.argsort(-base_scores, kind="stable"))
    uncertain = bool((trip[:, :, 0] != trip[:, :, 2]).any())
    sim = None
    if uncertain and len(feasible) > 1:
        rng = np.random.default_rng(seed)
        lo, mode, hi = trip[..., 0], trip[..., 1], trip[..., 2]
        same = hi == lo
        safe_hi = np.where(same, lo + 1e-12, hi)
        draws_m = rng.triangular(np.broadcast_to(lo, (draws, *lo.shape)), np.broadcast_to(np.clip(mode, lo, safe_hi), (draws, *lo.shape)),
                                 np.broadcast_to(safe_hi, (draws, *lo.shape)))
        draws_m = np.where(same, lo, draws_m)
        s = _scores(draws_m, crit, w)  # (draws, A)
        first = np.bincount(s.argmax(axis=1), minlength=len(feasible)) / draws
        sim = {"draws": draws, "seed": seed,
               "p_best": {names[i]: round(float(first[i]), 4) for i in range(len(feasible))},
               "score_p5": {names[i]: round(float(np.percentile(s[:, i], 5)), 4) for i in range(len(feasible))},
               "score_p95": {names[i]: round(float(np.percentile(s[:, i], 95)), 4) for i in range(len(feasible))}}
    # weight sensitivity: scale one criterion's weight (renormalizing the rest) and find where the leader changes
    flips = []
    if len(feasible) > 1:
        leader = order[0]
        for j, c in enumerate(crit):
            hits = []
            for f in np.linspace(0, 1, 101):
                others = w.sum() - w[j]
                ww = np.array([f if k == j else (w[k] / others * (1 - f) if others else 0) for k in range(len(w))])
                sc = _scores(base_m, crit, ww)
                if int(np.argmax(sc)) != leader:
                    hits.append((abs(f - w[j]), float(f), names[int(np.argmax(sc))]))
            near = min(hits) if hits else None
            flips.append({"criterion": c["name"], "current_weight": round(float(w[j]), 3),
                          "flips_at": round(near[1], 2) if near else None, "new_leader": near[2] if near else None})
    ranking = []
    cidx = {c["name"]: j for j, c in enumerate(crit)}
    for rank, i in enumerate(order, 1):
        a = feasible[i]
        row = {"rank": rank, "name": a["name"], "score": round(float(base_scores[i]), 4),
               "values": {c["name"]: float(base_m[i, j]) for j, c in enumerate(crit)}, "risks": a.get("risks", [])}
        if "benefit" in cidx and "cost" in cidx:
            b, co = base_m[i, cidx["benefit"]], base_m[i, cidx["cost"]]
            row["net_benefit"] = float(b - co)
            row["benefit_cost_ratio"] = float(b / co) if co else None
        if "risk" in cidx and sim:
            spread = sim["score_p95"][a["name"]] - sim["score_p5"][a["name"]]
            row["risk_adjusted_score"] = round(float(base_scores[i]) - 0.5 * spread, 4)
        ranking.append(row)
    return {"criteria": crit, "constraints": constraints, "infeasible": infeasible, "feasible": names,
            "ranking": ranking, "uncertainty": sim, "weight_sensitivity": flips}


def recommendation(spec: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """A recommendation that meets the contract, or an explicit statement of why there is none."""
    ranking = analysis.get("ranking") or []
    evidence = spec.get("evidence") or []
    out = {
        "requires_human_decision": True,
        "evidence": evidence,
        "assumptions": spec.get("assumptions") or [],
        "alternatives": [{"name": r["name"], "score": r["score"]} for r in ranking],
        "risks": {r["name"]: r.get("risks", []) for r in ranking},
        "implementation": spec.get("implementation") or ["Name an owner and a start date before acting.",
                                                         "Start with the smallest reversible step where one exists."],
        "monitoring": spec.get("monitoring") or (
            [{"metric": spec["outcome_metric"], "expect": spec.get("expected_impact"), "review_after_days": spec.get("review_after_days", 90)}]
            if spec.get("outcome_metric") else []),
    }
    if not ranking:
        return {**out, "status": "none", "choice": None, "statement": "No alternative meets every constraint; relax a "
                "constraint or add an alternative."}
    if not evidence:
        return {**out, "status": "insufficient_evidence", "choice": None,
                "statement": "The comparison runs on stated values only. Attach evidence for the values before relying on it.",
                "expected_impact": spec.get("expected_impact"), "uncertainty": analysis.get("uncertainty")}
    top = ranking[0]
    sim = analysis.get("uncertainty")
    clear = True
    if len(ranking) > 1:
        second = ranking[1]
        if sim:
            p = sim["p_best"].get(top["name"], 0)
            clear = p >= 0.6 and sim["score_p5"][top["name"]] > sim["score_p5"][second["name"]]
            unc = f"{top['name']} ranks first in {p:.0%} of {sim['draws']:,} simulated outcomes."
        else:
            clear = top["score"] - second["score"] >= 0.05
            unc = "Values are point estimates; no uncertainty was stated."
    else:
        unc = "Only one alternative is feasible."
    fragile = [f for f in analysis.get("weight_sensitivity") or [] if f.get("flips_at") is not None
               and abs(f["flips_at"] - f["current_weight"]) <= 0.1]
    if fragile:
        unc += " The choice changes with a small change in the weight of " + ", ".join(f["criterion"] for f in fragile) + "."
    if not clear:
        return {**out, "status": "no_clear_winner", "choice": None, "uncertainty": unc,
                "statement": f"{top['name']} and {ranking[1]['name']} are not separated beyond the uncertainty; "
                             "decide on the criteria that matter most, or gather evidence that narrows the ranges.",
                "expected_impact": spec.get("expected_impact")}
    return {**out, "status": "recommended", "choice": top["name"], "uncertainty": unc,
            "statement": f"{top['name']} scores highest on the weighted criteria ({top['score']:.2f}).",
            "expected_impact": spec.get("expected_impact")}


# ----------------------------------------------------------------------------- allocation


def allocate(items: list[dict[str, Any]], budget: float, *, groups: dict[str, int] | None = None,
             integer: bool = True) -> dict[str, Any]:
    """Maximize total value under a budget. Each item: name, cost, value, optional min_units / max_units (default
    0..1, i.e. take it or not) and group (at most ``groups[group]`` items from a group). Solved by HiGHS through
    ``scipy.optimize.milp``; the result says whether it is optimal and what binds."""
    from scipy.optimize import Bounds, LinearConstraint, milp  # noqa: PLC0415

    if not items:
        raise DecisionError("nothing to allocate")
    if budget < 0:
        raise DecisionError("budget cannot be negative")
    n = len(items)
    cost = np.array([float(i["cost"]) for i in items])
    value = np.array([float(i["value"]) for i in items])
    if (cost < 0).any():
        raise DecisionError("costs cannot be negative")
    lo = np.array([float(i.get("min_units", 0)) for i in items])
    hi = np.array([float(i.get("max_units", 1)) for i in items])
    rows, lb, ub = [cost], [-np.inf], [budget]
    for g, cap in (groups or {}).items():
        rows.append(np.array([1.0 if i.get("group") == g else 0.0 for i in items]))
        lb.append(-np.inf)
        ub.append(cap)
    res = milp(c=-value, constraints=LinearConstraint(np.vstack(rows), lb, ub), bounds=Bounds(lo, hi),
               integrality=np.ones(n) if integer else np.zeros(n))
    if not res.success:
        return {"status": "infeasible", "detail": res.message, "chosen": [], "total_cost": 0, "total_value": 0}
    x = np.round(res.x, 6)
    chosen = [{"name": items[i]["name"], "units": float(x[i]), "cost": float(cost[i] * x[i]), "value": float(value[i] * x[i])}
              for i in range(n) if x[i] > 0]
    spent = float(cost @ x)
    left_out = [items[i]["name"] for i in range(n) if x[i] == 0]
    return {"status": "optimal", "chosen": chosen, "left_out": left_out, "total_cost": spent,
            "total_value": float(value @ x), "budget": budget, "budget_left": budget - spent,
            "budget_binding": budget - spent < float(cost[cost > 0].min(initial=np.inf)) if (cost > 0).any() else False,
            "method": "mixed-integer linear program (scipy.optimize.milp, HiGHS)"}


# ----------------------------------------------------------------------------- decision records


class DecisionService:
    def __init__(self, stores, guard, runtime=None) -> None:
        self.stores, self.guard, self.runtime = stores, guard, runtime

    def _row(self, ws: str, did: str) -> dict[str, Any]:
        rows = self.stores.workspace(ws).query("SELECT * FROM decisions WHERE id = ?", [did])
        if not rows:
            raise KeyError(did)
        r = rows[0]
        for k in ("spec", "analysis", "recommendation", "outcome"):
            r[k] = json.loads(r[k]) if r[k] else None
        r["kind"] = "decision_record"  # a structured decision analysis, not a comment marked as a decision
        return r

    def create(self, actor: Principal, ws: str, title: str, spec: dict[str, Any], *,
               task_id: str | None = None) -> dict[str, Any]:
        """Record and analyze a decision. It stays a draft for people to review; nothing is executed."""
        self.guard.require(actor, ws, "decision.create")
        analysis = analyze(spec)
        rec = recommendation(spec, analysis)
        did = "d_" + uuid.uuid4().hex[:10]
        self.stores.workspace(ws).execute(
            "INSERT INTO decisions (id, title, question, spec, analysis, recommendation, status, task_id, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 'analyzed', ?, ?)",
            [did, title, spec.get("question"), json.dumps(spec), json.dumps(analysis), json.dumps(rec), task_id, actor.user_id])
        self.guard.audit(actor, ws, "decision.create", did, "ok", {"status": rec["status"]})
        return self._row(ws, did)

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        for r in self.stores.workspace(ws).query("SELECT id FROM decisions WHERE status = 'submitted'"):
            self.sync(ws, r["id"])
        return self.stores.workspace(ws).query(
            "SELECT id, title, status, recommendation->>'$.status' AS recommendation, recommendation->>'$.choice' AS choice, "
            "chosen, approval_id, task_id, 'decision_record' AS kind, created_by, created_at, decided_at, review_at "
            "FROM decisions ORDER BY created_at DESC")

    def get(self, actor: Principal, ws: str, did: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        return self.sync(ws, did)

    def submit(self, actor: Principal, ws: str, did: str, chosen: str, rationale: str = "") -> dict[str, Any]:
        """Ask the workspace's approvers to authorize ``chosen``. A person may choose differently from the
        recommendation; the record keeps both."""
        self.guard.require(actor, ws, "decision.create")
        d = self._row(ws, did)
        if d["status"] not in ("analyzed", "rejected"):
            raise DecisionError(f"decision is {d['status']}; only an analyzed or rejected decision can be submitted")
        if chosen not in [a["name"] for a in d["spec"]["alternatives"]]:
            raise DecisionError(f"{chosen} is not one of the alternatives")
        aid = self.runtime.request_approval(ws, actor.user_id, "decision.approve",
                                            f"Approve '{d['title']}': {chosen}", {"decision_id": did, "chosen": chosen})
        self.stores.workspace(ws).execute(
            "UPDATE decisions SET status = 'submitted', chosen = ?, rationale = ?, approval_id = ? WHERE id = ?",
            [chosen, rationale, aid, did])
        self.guard.audit(actor, ws, "decision.submit", did, "ok", {"chosen": chosen})
        return self._row(ws, did)

    def sync(self, ws: str, did: str) -> dict[str, Any]:
        """Bring a submitted decision in line with its approval (approved/rejected by a person)."""
        d = self._row(ws, did)
        if d["status"] != "submitted" or not d["approval_id"]:
            return d
        a = self.stores.control.query("SELECT status, decided_by, decided_at FROM approvals WHERE id = ?", [d["approval_id"]])
        if a and a[0]["status"] in ("approved", "rejected"):
            days = int((d["spec"] or {}).get("review_after_days", 90))
            review = (datetime.now(UTC) + timedelta(days=days)) if a[0]["status"] == "approved" else None
            self.stores.workspace(ws).execute(
                "UPDATE decisions SET status = ?, decided_by = ?, decided_at = ?, review_at = ? WHERE id = ?",
                [a[0]["status"], a[0]["decided_by"], a[0]["decided_at"], review, did])
        return self._row(ws, did)

    def review(self, actor: Principal, ws: str, did: str, observed: float, note: str = "") -> dict[str, Any]:
        """Post-decision review: compare the observed outcome with the expected impact."""
        self.guard.require(actor, ws, "decision.create")
        d = self.sync(ws, did)
        if d["status"] not in ("approved", "reviewed"):
            raise DecisionError("only an approved decision is reviewed against its outcome")
        expected = (d["spec"] or {}).get("expected_impact")
        exp_val = expected.get("value") if isinstance(expected, dict) else expected
        verdict = None
        if isinstance(exp_val, int | float):
            verdict = ("met" if (observed >= exp_val if exp_val >= 0 else observed <= exp_val)
                       else "partly met" if (observed > 0) == (exp_val > 0) else "not met")
        outcome = {"observed": observed, "expected": expected, "verdict": verdict, "note": note,
                   "reviewed_by": actor.user_id, "reviewed_at": date.today().isoformat()}
        self.stores.workspace(ws).execute("UPDATE decisions SET status = 'reviewed', outcome = ? WHERE id = ?",
                                          [json.dumps(outcome), did])
        self.guard.audit(actor, ws, "decision.review", did, "ok", {"verdict": verdict})
        return self._row(ws, did)


__all__ = ["DecisionError", "DecisionService", "allocate", "analyze", "frame", "recommendation"]
