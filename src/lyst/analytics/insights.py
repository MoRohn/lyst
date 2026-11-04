# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Findings: what the numbers say, in sentences, computed without a language model.

Each builder turns one analysis into a `Finding` with a title, a sentence, a tone (good/bad/neutral) and a
magnitude used to rank findings. Dashboards show the top few as a card; every number here is computed, so
the cards work offline and are tested like any other metric.

    f = [change("Sales", 745_568, 613_934, fmt.money, period="FY2026 vs FY2025"),
         driver(drivers.explain(cur, prev, "Sales", dims), "Sales", fmt.money),
         forecast_note(fc, "Sales", fmt.money)]
    top(f, 4)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import fmt as F

__all__ = ["Finding", "anomaly_note", "change", "concentration", "driver", "forecast_note", "negative_members", "rate_driver",
           "significance", "top"]


def _mid(label: str) -> str:
    """A label inside a sentence: "Logo churn" -> "logo churn", but "MRR" stays "MRR"."""
    return label if len(label) > 1 and label[1].isupper() else label[:1].lower() + label[1:]


@dataclass
class Finding:
    kind: str
    title: str
    text: str
    tone: str = "neutral"          # good | bad | neutral
    magnitude: float = 0.0         # for ranking, roughly 0..1
    icon: str = "insights"         # a name the dashboard kit maps to an icon
    evidence: dict = field(default_factory=dict)


def change(label: str, current: float, prior: float, fmt=F.money, *, period: str = "vs prior period",
           higher_is_better: bool = True) -> Finding:
    rel = F.change(current, prior)
    if rel is None:
        return Finding("change", f"{label}: {fmt(current)}", f"No prior period to compare {_mid(label)} with.",
                       evidence={"current": current})
    up = rel > 0
    good = up == higher_is_better
    word = "up" if up else "down"
    return Finding(
        "change", f"{label} {word} {F.pct(abs(rel))}",
        f"{label} is {fmt(current)} {period}, {word} {fmt(abs(current - prior))} from {fmt(prior)}.",
        tone="neutral" if abs(rel) < 0.005 else ("good" if good else "bad"),
        magnitude=min(1.0, abs(rel) * 3), icon="trending_up" if up else "trending_down",
        evidence={"current": current, "prior": prior, "change": current - prior, "pct_change": rel})


def driver(explained: dict, label: str, fmt=F.money, *, higher_is_better: bool = True) -> Finding | None:
    """From analytics.drivers.explain(): which member moved the total."""
    if not explained or not explained.get("total_change"):
        return None
    total, ch, share = explained["total_change"], explained["change"], explained["share"]
    dim, member = explained["dimension"], explained["member"]
    direction = "growth" if total > 0 else "decline"
    good = (total > 0) == higher_is_better
    share_txt = f"{F.pct(share, 0)} of the" if 0 < share <= 1 else "more than the whole"
    return Finding(
        "driver", f"{member} drove the {direction}",
        f"{member} ({dim}) accounts for {share_txt} {direction} in {_mid(label)} ({F.signed(fmt, ch)} of "
        f"{F.signed(fmt, total)}).",
        tone="good" if good else "bad", magnitude=min(1.0, abs(share) if np.isfinite(share) else 1.0) * 0.9,
        icon="account_tree",
        evidence={"dimension": dim, "member": member, "change": ch, "total_change": total, "share": share})


def rate_driver(explained: dict, label: str, *, higher_is_better: bool = False, period: str = "") -> Finding | None:
    """From analytics.drivers.explain_rate(): which member's rate moved the overall rate."""
    if not explained or not explained.get("change"):
        return None
    ch, eff, share = explained["change"], explained["effect"], explained["share"]
    dim, member = explained["dimension"], explained["member"]
    t = explained["table"]
    row = t[t[dim] == member].iloc[0]
    up = ch > 0
    good = up == higher_is_better
    share_txt = f"{F.pct(share, 0)} of" if 0 < share <= 1 else "more than all of"
    return Finding(
        "driver", f"{member} drove the {_mid(label)} {'rise' if up else 'fall'}",
        f"{label} went from {F.pct(explained['prior'])} to {F.pct(explained['current'])}{(' ' + period) if period else ''}. "
        f"{member}'s own rate ({dim}) moved {F.pct(row['prior_rate'])} → {F.pct(row['current_rate'])}, "
        f"{share_txt} the change ({F.pts(eff)} of {F.pts(ch)}).",
        tone="good" if good else "bad", magnitude=min(1.0, abs(share)) * 0.95 if np.isfinite(share) else 0.9,
        icon="account_tree",
        evidence={"dimension": dim, "member": member, "prior": explained["prior"], "current": explained["current"],
                  "change": ch, "member_effect": eff, "share": share})


def anomaly_note(table: pd.DataFrame, label: str, fmt=F.num, *, date_fmt: str = "%b %d, %Y") -> Finding | None:
    """From analytics.timeseries.anomalies(): the strongest flagged points."""
    flagged = table[table["is_anomaly"]].copy()
    if flagged.empty:
        return None
    flagged["abs"] = flagged["score"].abs()
    worst = flagged.sort_values("abs", ascending=False).iloc[0]
    when = pd.Timestamp(worst["period"]).strftime(date_fmt)
    n = len(flagged)
    verb = "spiked to" if worst["direction"] == "spike" else "dropped to"
    more = f" {n - 1} other unusual point{'s' if n > 2 else ''} too." if n > 1 else ""
    return Finding(
        "anomaly", f"Unusual {_mid(label)} on {when}",
        f"{label} {verb} {fmt(worst['value'])} against about {fmt(worst['expected'])} expected "
        f"(robust z {worst['score']:+.1f}).{more}",
        tone="bad" if worst["direction"] == "drop" else "neutral", magnitude=min(1.0, worst["abs"] / 10),
        icon="warning",
        evidence={"date": str(pd.Timestamp(worst["period"]).date()), "value": float(worst["value"]),
                  "expected": float(worst["expected"]), "score": float(worst["score"]), "flagged": n})


def forecast_note(fc, label: str, fmt=F.money, *, stock: bool = False, compare_last: bool = True) -> Finding:
    """From analytics.timeseries.forecast(). A flow (sales, signups) is summed over the horizon and compared with
    the same span a season earlier; a stock (MRR, a margin) reports where it ends up against the last actual."""
    h = len(fc.table)
    end = pd.Timestamp(fc.table["period"].iloc[-1])
    acc = f" Backtest error {F.pct(fc.backtest_mape, 0)} over the last {fc.backtest_points} periods." \
        if fc.backtest_mape is not None else ""
    level = F.pct(fc.level, 0)
    if stock:
        nxt, last = float(fc.table["yhat"].iloc[-1]), float(fc.history.iloc[-1])
        rel = F.change(nxt, last)
        lo, hi = float(fc.table["lo"].iloc[-1]), float(fc.table["hi"].iloc[-1])
        title = f"{label} heading to {fmt(nxt)} by {end:%b %Y}"
        text = (f"{fc.method} projects {label} of {fmt(nxt)} in {end:%B %Y}"
                + (f", {F.signed_pct(rel)} on the latest {fmt(last)}" if rel is not None else "")
                + f" ({level} interval {fmt(lo)}–{fmt(hi)}).{acc}")
    else:
        nxt = fc.total
        s = max(fc.season, h)
        same = fc.history.iloc[-s:-s + h] if len(fc.history) >= s else None
        last = float(same.sum()) if compare_last and same is not None and len(same) == h else None
        rel = F.change(nxt, last) if last else None
        start = pd.Timestamp(fc.table["period"].iloc[0])
        title = f"{label} outlook, {start:%b}–{end:%b %Y}: {fmt(nxt)}"
        text = (f"{fc.method} projects {fmt(nxt)} of {_mid(label)} over the next {h} periods"
                + (f", {F.signed_pct(rel)} on the same periods a year earlier ({fmt(last)})" if rel is not None else "")
                + f". The chart shows the {level} interval for each period.{acc}")
    return Finding("forecast", title, text, tone="neutral",
                   magnitude=0.35 + (min(0.4, abs(rel)) if rel is not None else 0), icon="timeline",
                   evidence={"forecast": nxt, "compared_with": last, "pct_change": rel, "method": fc.method,
                             "backtest_mape": fc.backtest_mape, "stock": stock})


def concentration(pareto_table: pd.DataFrame, entities: str, value: str, *, fraction: float = 0.2) -> Finding:
    k = max(1, int(np.ceil(len(pareto_table) * fraction)))
    share = float(pareto_table["share"].iloc[:k].sum())
    return Finding(
        "concentration", f"Top {F.pct(fraction, 0)} of {entities} bring {F.pct(share, 0)} of {value}",
        f"{k:,} of {len(pareto_table):,} {entities} account for {F.pct(share, 1)} of {value}; "
        f"{int((pareto_table['class'] == 'A').sum()):,} make up the first 80%.",
        tone="neutral", magnitude=min(1.0, max(0.0, share - fraction)), icon="pie_chart",
        evidence={"entities": len(pareto_table), "top_n": k, "top_share": share})


def negative_members(table: pd.DataFrame, dim: str, value: str, label: str, fmt=F.money) -> Finding | None:
    """Members of dim whose `value` (profit, margin) is below zero."""
    neg = table[table[value] < 0].sort_values(value)
    if neg.empty:
        return None
    names = ", ".join(str(x) for x in neg[dim].head(3))
    noun = dim.lower() if len(neg) == 1 else (dim.lower()[:-1] + "ies" if dim.lower().endswith("y") else dim.lower() + "s")
    return Finding(
        "negative", f"{len(neg)} {noun} {'loses' if len(neg) == 1 else 'lose'} money",
        f"{names}{' and others' if len(neg) > 3 else ''} have negative {label.lower()}; together "
        f"{fmt(float(neg[value].sum()))}.",
        tone="bad", magnitude=0.6, icon="money_off",
        evidence={"members": neg[dim].astype(str).tolist(), "total": float(neg[value].sum())})


def significance(label: str, result: dict, *, as_rate: bool = True, higher_is_better: bool = False) -> Finding:
    """From analytics.stats.two_proportions(): whether a rate change is real."""
    from .stats import verdict  # noqa: PLC0415

    p = result["p_value"]
    sig = p < 0.05
    good = (result["diff"] > 0) == higher_is_better
    change_txt = F.pts(result["diff"]) if as_rate else f"{result['diff']:+.3g}"
    return Finding(
        "significance", f"{label} {change_txt}: {'real change' if sig else 'within noise'}",
        f"{label} moved from {F.pct(result['p1'])} to {F.pct(result['p2'])}; the change is {verdict(result)}.",
        tone=("good" if good else "bad") if sig else "neutral", magnitude=0.5 if sig else 0.15,
        icon="science", evidence=result)


def top(findings: list[Finding | None], k: int = 4) -> list[Finding]:
    """The k largest findings, bad news first among equals."""
    fs = [f for f in findings if f is not None]
    return sorted(fs, key=lambda f: (-f.magnitude, f.tone != "bad"))[:k]
