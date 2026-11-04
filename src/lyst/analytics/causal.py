# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Causal and quasi-experimental methods with their identification assumptions stated.

* ``interrupted_time_series``: a segmented regression (level and slope change at the break) with
  HAC (Newey-West) standard errors. Assumes no other intervention at the break and that the pre-period
  trend would have continued.
* ``difference_in_differences``: two-way comparison of treated and control units before and after,
  with the parallel-trends check on the pre-period. Assumes parallel trends and no spillover.

Each returns an effect, its confidence interval and p-value, and the assumptions as text, so an answer
can say "plausible causal estimate under these assumptions" rather than "caused".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import statsmodels.api as sm


@dataclass
class Effect:
    method: str
    effect: float
    ci_low: float
    ci_high: float
    p_value: float
    n: int
    details: dict = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def interrupted_time_series(y: pd.Series, break_index: int, *, level: float = 0.95) -> Effect:
    """Segmented regression: y = a + b·t + c·post + d·(t − t_break)·post. ``c`` is the immediate level change;
    the reported effect is the average gap between actual and the pre-trend counterfactual over the post period."""
    y = y.astype(float).reset_index(drop=True)
    n = len(y)
    if n < 6 or break_index < 3 or n - break_index < 2:
        raise ValueError("an interrupted time series needs at least 3 points before and 2 after the break")
    t = np.arange(n, dtype=float)
    post = (t >= break_index).astype(float)
    X = sm.add_constant(np.column_stack([t, post, (t - break_index) * post]))
    lags = max(1, int(round(n ** (1 / 3))))
    fit = sm.OLS(y.to_numpy(), X).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    a, b, c, d = fit.params
    counterfactual = a + b * t[break_index:]
    actual = y.to_numpy()[break_index:]
    gap = float(np.mean(actual - counterfactual))
    # delta-method CI on the average post-period gap: linear combination of (c, d)
    k = len(actual)
    w = np.array([0.0, 0.0, 1.0, float(np.mean(np.arange(k)))])
    var = float(w @ fit.cov_params() @ w)
    z = {0.9: 1.645, 0.95: 1.96, 0.99: 2.576}.get(level, 1.96)
    se = np.sqrt(max(var, 0.0))
    warnings = []
    if n - break_index < 4:
        warnings.append("fewer than 4 post-break points: the slope change is poorly identified")
    return Effect("interrupted time series (segmented OLS, HAC errors)", gap, gap - z * se, gap + z * se,
                  float(fit.pvalues[2]), n,
                  details={"level_change": float(c), "slope_change": float(d), "pre_slope": float(b),
                           "level_change_p": float(fit.pvalues[2]), "slope_change_p": float(fit.pvalues[3]),
                           "post_points": k, "pre_points": break_index},
                  assumptions=["no other intervention at the break", "the pre-break trend would have continued",
                               "errors may be autocorrelated (handled with HAC) but the model is linear in time"],
                  warnings=warnings)


def difference_in_differences(treated: pd.Series, control: pd.Series, break_index: int, *, level: float = 0.95) -> Effect:
    """DiD on two aligned series. Effect = (treated_post − treated_pre) − (control_post − control_pre), estimated by
    OLS with unit, period and interaction terms; the pre-period trend gap is reported as the parallel-trends check."""
    treated, control = treated.astype(float).reset_index(drop=True), control.astype(float).reset_index(drop=True)
    if len(treated) != len(control):
        raise ValueError("treated and control must cover the same periods")
    n = len(treated)
    if break_index < 2 or n - break_index < 1:
        raise ValueError("DiD needs at least 2 pre-periods and 1 post-period")
    rows = []
    for unit, s in (("treated", treated), ("control", control)):
        for i, v in enumerate(s):
            rows.append({"y": v, "treated": 1.0 if unit == "treated" else 0.0, "post": 1.0 if i >= break_index else 0.0, "t": i})
    df = pd.DataFrame(rows)
    df["did"] = df["treated"] * df["post"]
    X = sm.add_constant(df[["treated", "post", "did"]].to_numpy())
    fit = sm.OLS(df["y"].to_numpy(), X).fit(cov_type="HC1")
    effect = float(fit.params[3])
    ci = fit.conf_int(alpha=1 - level)[3]
    pre_t, pre_c = treated.iloc[:break_index], control.iloc[:break_index]
    slope_t = np.polyfit(np.arange(break_index), pre_t, 1)[0]
    slope_c = np.polyfit(np.arange(break_index), pre_c, 1)[0]
    gap = float(slope_t - slope_c)
    scale = float(np.std(np.diff(pre_c))) or 1.0
    warnings = []
    if abs(gap) > scale:
        warnings.append(f"pre-period trends differ ({gap:+.3g} per period): parallel trends are doubtful")
    return Effect("difference-in-differences (OLS, HC1 errors)", effect, float(ci[0]), float(ci[1]), float(fit.pvalues[3]), 2 * n,
                  details={"pre_trend_gap": gap, "treated_pre": float(pre_t.mean()), "treated_post": float(treated.iloc[break_index:].mean()),
                           "control_pre": float(pre_c.mean()), "control_post": float(control.iloc[break_index:].mean())},
                  assumptions=["parallel trends: without the treatment, the treated unit would have moved like the control",
                               "no spillover from treated to control", "composition of both units is stable"],
                  warnings=warnings)
