# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Prescriptive helpers: scenario comparison under forecast uncertainty, and linear allocation.

* ``quarter_from_monthly``: turns monthly point forecasts and intervals into a period total with an
  interval (independent-month approximation, stated as such).
* ``staffing_scenarios``: Monte Carlo over the demand forecast (normal, sigma from the interval) for
  each scenario's capacity: expected shortfall, probability of any shortfall, utilisation and cost.
* ``minimum_staff``: the smallest headcount whose shortfall probability is under a target.
* ``allocate``: linear programme (SciPy ``linprog``, HiGHS) that assigns staff to channels at minimum
  cost subject to each channel's demand at a safety factor.

A recommendation from these is a comparison of options, never an action Lyst takes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog


@dataclass
class ScenarioResult:
    name: str
    agents: float
    capacity: float
    expected_demand: float
    p_shortfall: float
    expected_shortfall: float
    utilisation: float
    cost: float | None


def quarter_from_monthly(yhat: list[float], lo: list[float], hi: list[float], *, level: float = 0.8) -> dict[str, float]:
    z = {0.8: 1.2816, 0.9: 1.645, 0.95: 1.96}.get(level, 1.2816)
    sigmas = [(h - lo_) / (2 * z) for lo_, h in zip(lo, hi, strict=True)]
    total = float(sum(yhat))
    sigma = math.sqrt(sum(s * s for s in sigmas))
    return {"total": total, "lo": total - z * sigma, "hi": total + z * sigma, "sigma": sigma, "level": level}


def staffing_scenarios(demand_mean: float, demand_sigma: float, scenarios: list[dict], *, draws: int = 20_000,
                       seed: int = 0) -> list[ScenarioResult]:
    rng = np.random.default_rng(seed)
    demand = np.maximum(rng.normal(demand_mean, max(demand_sigma, 1e-9), draws), 0)
    out = []
    for sc in scenarios:
        capacity = float(sc["agents"]) * float(sc["capacity_per_agent"])
        shortfall = np.maximum(demand - capacity, 0)
        out.append(ScenarioResult(sc["name"], float(sc["agents"]), capacity, float(demand.mean()),
                                  float((shortfall > 0).mean()), float(shortfall.mean()),
                                  float(np.minimum(demand / capacity, 1).mean()) if capacity else float("nan"),
                                  float(sc["agents"]) * float(sc["cost_per_agent"]) if sc.get("cost_per_agent") is not None else None))
    return out


def minimum_staff(demand_mean: float, demand_sigma: float, capacity_per_agent: float, *, max_p_shortfall: float = 0.05) -> int:
    z = {0.01: 2.326, 0.05: 1.645, 0.1: 1.2816}.get(max_p_shortfall, 1.645)
    needed = (demand_mean + z * demand_sigma) / capacity_per_agent
    return int(math.ceil(needed))


def allocate(channels: list[dict], *, safety: float = 1.1) -> dict:
    """channels: [{name, demand, capacity_per_agent, cost_per_agent}] → agents per channel at minimum cost."""
    c = [ch["cost_per_agent"] for ch in channels]
    bounds = [(ch["demand"] * safety / ch["capacity_per_agent"], None) for ch in channels]
    res = linprog(c=c, bounds=bounds, method="highs")
    if not res.success:
        raise ValueError(f"allocation infeasible: {res.message}")
    plan = [{"channel": ch["name"], "agents": float(math.ceil(x)), "cost": float(math.ceil(x) * ch["cost_per_agent"])}
            for ch, x in zip(channels, res.x, strict=True)]
    return {"plan": plan, "total_agents": sum(p["agents"] for p in plan), "total_cost": sum(p["cost"] for p in plan),
            "safety": safety, "method": "linear programme (HiGHS) with integer rounding up"}
