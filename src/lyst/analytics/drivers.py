# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
# Lyst's own module, maintained here; it grew out of the author's earlier analytics toolkit.
"""Why did it change? Attribution of a change between two periods.

    c = contribution(cur, prev, "Region", "Sales")          # who moved the total, and by how much
    best = explain(cur, prev, "Sales", ["Region", "Segment", "Category"])   # the dimension that explains most
    steps = pvm(cur, prev, segment="Sub-Category", units="Quantity", revenue="Sales")
    chart = figures.waterfall(steps, palette)

Totals and rates need different tools. `contribution`/`explain` attribute a change in a
*total* (sales, churned MRR). A change in a *rate* (churn, conversion, margin) is a weighted
average of member rates, so `rate_change`/`explain_rate` split it exactly into each
member's rate effect (its rate change × its current weight) plus a mix effect (weights
shifting between members). Found on the subscriptions template (2026-10-23): attributing
churned MRR year on year pointed at the Enterprise segment (a few large accounts, on a
growing base), while the rate decomposition finds the planted EMEA churn spike.

`pvm` is the price-volume-mix bridge: Prior + Volume + Mix + Price + New/lost = Current,
exactly (tested), with Volume = ΔQ × prior average price, Mix = Q₁ × Σ(Δshare × prior
price), Price = Σ q₁ × Δprice, over segments sold in both periods; segments sold in only
one period form the New/lost step.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["contribution", "explain", "pvm", "bridge_from_contribution", "rate_change", "explain_rate", "MISSING"]

MISSING = "(missing)"


def _by(df: pd.DataFrame, dim: str) -> pd.Series:
    """Group keys with blanks kept as their own member: groupby drops NaN keys, which would
    take those rows out of the totals and stop bridges closing (review, 2026-10-23)."""
    return df[dim].astype(object).where(df[dim].notna(), MISSING).rename(dim)


def contribution(cur: pd.DataFrame, prev: pd.DataFrame, dim: str, value: str, *, agg: str = "sum") -> pd.DataFrame:
    """Per member of `dim`: prior, current, change, pct_change, share_of_change (of the total
    change; members moving against the total get a negative share). Sorted by |change|."""
    a = prev.groupby(_by(prev, dim))[value].agg(agg).rename("prior")
    b = cur.groupby(_by(cur, dim))[value].agg(agg).rename("current")
    t = pd.concat([a, b], axis=1).fillna(0.0)
    t["change"] = t["current"] - t["prior"]
    t["pct_change"] = np.where(t["prior"] != 0, t["change"] / t["prior"].abs(), np.nan)
    total = t["change"].sum()
    t["share_of_change"] = t["change"] / total if total != 0 else np.nan
    t = t.reindex(t["change"].abs().sort_values(ascending=False).index)
    t.index.name = dim
    return t.reset_index()


def explain(cur: pd.DataFrame, prev: pd.DataFrame, value: str, dims: list[str], *, agg: str = "sum") -> dict:
    """The dimension whose single largest mover explains the most of the total change.

    Returns {"dimension", "member", "change", "share", "total_change", "table"}; share is
    that member's change over the total change (can exceed 1 when other members offset)."""
    total = cur[value].agg(agg) - prev[value].agg(agg)
    best = None
    for d in dims:
        t = contribution(cur, prev, d, value, agg=agg)
        if t.empty:
            continue
        # The top mover in the direction of the total change.
        same_dir = t[np.sign(t["change"]) == np.sign(total)] if total != 0 else t
        top = (same_dir if not same_dir.empty else t).iloc[0]
        share = top["change"] / total if total != 0 else np.nan
        score = abs(top["change"]) / (t["change"].abs().sum() or 1)
        if best is None or score > best["_score"]:
            best = {"dimension": d, "member": top[d], "change": float(top["change"]), "share": float(share),
                    "total_change": float(total), "table": t, "_score": score}
    if best:
        best.pop("_score")
    return best or {}


def pvm(cur: pd.DataFrame, prev: pd.DataFrame, *, segment: str, units: str, revenue: str) -> pd.DataFrame:
    """Price-volume-mix bridge from prior to current revenue. Columns: step, value, kind
    ("total" for the ends, "delta" for the effects)."""
    g0 = prev.groupby(_by(prev, segment))[[units, revenue]].sum()
    g1 = cur.groupby(_by(cur, segment))[[units, revenue]].sum()
    both = g0.index.intersection(g1.index)
    both = [s for s in both if g0.loc[s, units] > 0 and g1.loc[s, units] > 0]
    r0_all, r1_all = float(g0[revenue].sum()), float(g1[revenue].sum())
    a, b = g0.loc[both], g1.loc[both]
    q0, q1 = a[units].astype(float), b[units].astype(float)
    p0, p1 = a[revenue] / q0, b[revenue] / q1
    Q0, Q1 = q0.sum(), q1.sum()
    s0, s1 = q0 / Q0, q1 / Q1
    volume = float((Q1 - Q0) * (s0 * p0).sum())
    mix = float(Q1 * ((s1 - s0) * p0).sum())
    price = float((q1 * (p1 - p0)).sum())
    common0, common1 = float(a[revenue].sum()), float(b[revenue].sum())
    new_lost = (r1_all - common1) - (r0_all - common0)
    rows = [("Prior", r0_all, "total"), ("Volume", volume, "delta"), ("Mix", mix, "delta"),
            ("Price", price, "delta")]
    if abs(new_lost) > 1e-9:
        rows.append(("New / lost", new_lost, "delta"))
    rows.append(("Current", r1_all, "total"))
    return pd.DataFrame(rows, columns=["step", "value", "kind"])


def rate_change(cur: pd.DataFrame, prev: pd.DataFrame, dim: str, num: str, den: str) -> pd.DataFrame:
    """Decompose the change in an overall rate Σnum/Σden between two periods by `dim`.

    Per member: prior/current rate and weight (its share of den), rate_effect = w₁ × (r₁ − r₀),
    mix_effect = (w₁ − w₀) × (r₀ − R₀), contribution = their sum. Measuring mix against the
    prior overall rate R₀ gives each member the right sign: a low-rate member that gains
    weight pulls the rate down (with × r₀ alone its mix effect came out positive). The
    weight changes sum to zero, so the contributions still add up to the overall change
    exactly. A member missing from the prior period takes R₀ as r₀. Numerators with no
    denominator in a period can't be weighted; their effect goes to an "(unattributed)" row
    so the identity always holds. Sorted by |rate_effect|."""
    a = prev.groupby(_by(prev, dim))[[num, den]].sum()
    b = cur.groupby(_by(cur, dim))[[num, den]].sum()
    t = a.add_suffix("_0").join(b.add_suffix("_1"), how="outer").fillna(0.0)
    R0 = t[f"{num}_0"].sum() / t[f"{den}_0"].sum() if t[f"{den}_0"].sum() else np.nan
    R1 = t[f"{num}_1"].sum() / t[f"{den}_1"].sum() if t[f"{den}_1"].sum() else np.nan
    w0 = t[f"{den}_0"] / t[f"{den}_0"].sum()
    w1 = t[f"{den}_1"] / t[f"{den}_1"].sum()
    r0 = (t[f"{num}_0"] / t[f"{den}_0"]).where(t[f"{den}_0"] > 0, R0)
    r1 = (t[f"{num}_1"] / t[f"{den}_1"]).where(t[f"{den}_1"] > 0, r0)
    out = pd.DataFrame({"prior_rate": r0, "current_rate": r1, "prior_weight": w0, "current_weight": w1,
                        "rate_effect": w1 * (r1 - r0), "mix_effect": (w1 - w0) * (r0 - R0),
                        "prior_den": t[f"{den}_0"], "current_den": t[f"{den}_1"]})
    out["contribution"] = out["rate_effect"] + out["mix_effect"]
    residual = (R1 - R0) - out["contribution"].sum() if np.isfinite(R1 - R0) else 0.0
    if abs(residual) > 1e-12:
        out.loc["(unattributed)"] = {"prior_rate": np.nan, "current_rate": np.nan, "prior_weight": 0.0,
                                     "current_weight": 0.0, "rate_effect": residual, "mix_effect": 0.0,
                                     "prior_den": 0.0, "current_den": 0.0, "contribution": residual}
    out.attrs.update(prior=R0, current=R1, change=R1 - R0)
    out = out.reindex(out["rate_effect"].abs().sort_values(ascending=False).index)
    out.index.name = dim
    return out.reset_index()


def explain_rate(cur: pd.DataFrame, prev: pd.DataFrame, num: str, den: str, dims: list[str]) -> dict:
    """The dimension whose single member's rate effect explains the most of the change in
    the overall rate. Returns {"dimension", "member", "effect", "share", "prior", "current",
    "change", "table"}; share = effect / change."""
    best = None
    for d in dims:
        t = rate_change(cur, prev, d, num, den)
        if t.empty:
            continue
        change = t.attrs["change"]
        same = t[np.sign(t["rate_effect"]) == np.sign(change)] if change else t
        top = (same if not same.empty else t).iloc[0]
        score = abs(top["rate_effect"]) / (t["rate_effect"].abs().sum() or 1)
        if best is None or score > best["_score"]:
            best = {"dimension": d, "member": top[d], "effect": float(top["rate_effect"]),
                    "share": float(top["rate_effect"] / change) if change else np.nan,
                    "prior": float(t.attrs["prior"]), "current": float(t.attrs["current"]),
                    "change": float(change), "table": t, "_score": score}
    if best:
        best.pop("_score")
    return best or {}


def bridge_from_contribution(c: pd.DataFrame, dim: str, *, top: int = 6, prior_label: str = "Prior",
                             current_label: str = "Current") -> pd.DataFrame:
    """A waterfall from a contribution table: prior total, the `top` movers, 'Other', current."""
    head = c.head(top)
    other = c.iloc[top:]["change"].sum()
    rows = [(prior_label, c["prior"].sum(), "total")]
    rows += [(str(r[dim]), r["change"], "delta") for _, r in head.iterrows()]
    if len(c) > top:
        rows.append(("Other", other, "delta"))
    rows.append((current_label, c["current"].sum(), "total"))
    return pd.DataFrame(rows, columns=["step", "value", "kind"])
