# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
# Vendored from ai-analytics stkit (analytics/stats.py) at commit d86ee66, same author (Rohn Springfield).
# Changes for Lyst are marked 'lyst:'. Keep in sync deliberately, not automatically.
"""Is the change real? Small, explicit significance tests for KPI deltas.

A dashboard that shows "+1.8 pts" should be able to say whether that is noise. These return
plain dicts, and `verdict()` turns one into a short label for a caption or tooltip.

    two_proportions(x1=120, n1=1000, x2=150, n2=1000)   # conversion or churn rates
    welch(a, b)                                        # means of two samples
    bootstrap_ci(values, np.median)                    # any statistic
"""
from __future__ import annotations

import numpy as np
from scipy import stats as sps

__all__ = ["two_proportions", "welch", "bootstrap_ci", "verdict"]


def two_proportions(x1: int, n1: int, x2: int, n2: int) -> dict:
    """Two-sided z-test for p2 − p1 (pooled standard error), with a 95% Wald interval for the
    difference. Returns p1, p2, diff, z, p_value, ci."""
    if min(n1, n2) <= 0:
        raise ValueError("both groups need observations")
    p1, p2 = x1 / n1, x2 / n2
    pool = (x1 + x2) / (n1 + n2)
    se = np.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    z = (p2 - p1) / se if se > 0 else 0.0
    p = 2 * sps.norm.sf(abs(z))
    se_u = np.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return {"p1": p1, "p2": p2, "diff": p2 - p1, "z": float(z), "p_value": float(p),
            "ci": (p2 - p1 - 1.96 * se_u, p2 - p1 + 1.96 * se_u)}


def welch(a, b) -> dict:
    """Welch's t-test for mean(b) − mean(a)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    t, p = sps.ttest_ind(b, a, equal_var=False)
    return {"mean_a": float(a.mean()), "mean_b": float(b.mean()), "diff": float(b.mean() - a.mean()),
            "t": float(t), "p_value": float(p)}


def bootstrap_ci(values, statistic=np.mean, *, n: int = 2000, level: float = 0.95, seed: int = 0) -> tuple:
    """Percentile bootstrap interval for any statistic."""
    x = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    boots = np.array([statistic(rng.choice(x, size=len(x), replace=True)) for _ in range(n)])
    lo, hi = np.quantile(boots, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def verdict(result: dict, *, alpha: float = 0.05) -> str:
    """'significant (p = 0.003)' or 'within noise (p = 0.41)'."""
    p = result["p_value"]
    ptxt = "p < 0.001" if p < 0.001 else f"p = {p:.3f}" if p < 0.1 else f"p = {p:.2f}"
    return f"significant ({ptxt})" if p < alpha else f"within noise ({ptxt})"
