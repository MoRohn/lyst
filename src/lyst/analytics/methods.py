# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Analytical method families the specializations need beyond governed metrics.

Each function takes plain DataFrames (already read through the SQL guard by the caller, so access checks, row
policies and column policies have applied), is deterministic (fixed seeds), and returns a dict with a one-sentence
``summary``, ``findings``, at most a few ``tables`` and the ``assumptions`` the result depends on. None of them calls
a model. They are deliberately standard, well-understood methods with honest limits, not novel algorithms:

- ``segment``: k-means on standardized numeric features, k chosen by silhouette (2..6).
- ``classify``: logistic regression (statsmodels) with a time-ordered holdout; accuracy, precision, recall, AUC.
- ``process_map``: variants, directly-follows transitions, step waiting times, rework and cycle times from an event log.
- ``distances``: haversine distances, nearest neighbour and threshold counts between two point sets.
- ``reconcile``: key-level matching of two sources with a tolerance; missing on each side, value breaks, totals.
- ``graph``: degree, connected components and the most connected nodes of an edge list.
- ``log_patterns``: event mix, rare events and per-entity bursts against each entity's own baseline in an event log.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

import numpy as np
import pandas as pd


def _r(x: float, d: int = 3) -> float:
    return float(round(float(x), d))


def _table(title: str, df: pd.DataFrame, limit: int = 20) -> dict[str, Any]:
    df = df.head(limit)
    return {"title": title, "columns": [str(c) for c in df.columns],
            "rows": [[(None if (isinstance(v, float) and math.isnan(v)) else (v.item() if hasattr(v, "item") else v)) for v in row]
                     for row in df.itertuples(index=False)]}


class MethodError(ValueError):
    """The data does not fit the method (too few rows, missing columns); the message says what is needed."""


# -- segmentation -------------------------------------------------------------------------------------------------


def _silhouette(x: np.ndarray, labels: np.ndarray) -> float:
    n = len(x)
    if n > 2000:  # sample for cost; fixed seed keeps it deterministic
        idx = np.random.default_rng(0).choice(n, 2000, replace=False)
        x, labels = x[idx], labels[idx]
    d = np.sqrt(((x[:, None, :] - x[None, :, :]) ** 2).sum(-1))
    s = []
    for i in range(len(x)):
        same = labels == labels[i]
        if same.sum() <= 1:
            s.append(0.0)
            continue
        a = d[i, same].sum() / (same.sum() - 1)
        b = min(d[i, labels == k].mean() for k in set(labels.tolist()) if k != labels[i])
        s.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return float(np.mean(s))


def segment(df: pd.DataFrame, features: list[str], *, entity: str | None = None, k: int | None = None) -> dict[str, Any]:
    from scipy.cluster.vq import kmeans2  # noqa: PLC0415

    data = df[features].apply(pd.to_numeric, errors="coerce").dropna()
    if len(data) < 10:
        raise MethodError("segmentation needs at least 10 rows with numeric values in " + ", ".join(features))
    mu, sd = data.mean(), data.std(ddof=0).replace(0, 1)
    x = ((data - mu) / sd).to_numpy()
    tried = {}
    for kk in ([k] if k else range(2, min(6, len(data) - 1) + 1)):
        _, labels = kmeans2(x, kk, seed=0, minit="++")
        if len(set(labels.tolist())) < 2:
            continue
        tried[kk] = (_silhouette(x, labels), labels)
    if not tried:
        raise MethodError("the rows do not separate into groups on these features")
    best = max(tried, key=lambda kk: tried[kk][0])
    sil, labels = tried[best]
    out = data.copy()
    out["segment"] = labels + 1
    prof = out.groupby("segment")[features].mean().round(3)
    prof.insert(0, "members", out.groupby("segment").size())
    prof = prof.reset_index().sort_values("members", ascending=False)
    biggest = prof.iloc[0]
    findings = []
    for _, row in prof.iterrows():
        diffs = sorted(((f, (row[f] - mu[f]) / sd[f]) for f in features), key=lambda t: -abs(t[1]))[:2]
        desc = ", ".join(f"{'higher' if z > 0 else 'lower'} {f}" for f, z in diffs)
        findings.append(f"Segment {int(row['segment'])}: {int(row['members'])} members, notably {desc} than average.")
    quality = "clear" if sil >= 0.5 else ("moderate" if sil >= 0.25 else "weak")
    return {"summary": f"The {len(data)} rows fall into {best} segments on {', '.join(features)} "
                       f"(silhouette {sil:.2f}, {quality} separation); the largest has {int(biggest['members'])} members.",
            "findings": findings, "tables": [_table("Segment profiles (feature means)", prof)],
            "metrics": {"k": best, "silhouette": _r(sil), "rows": int(len(data))},
            "assumptions": ["Features are standardized; k-means assumes roughly spherical groups of similar size.",
                            f"k was chosen by silhouette among {sorted(tried)}; seed fixed for reproducibility."]}


# -- classification -----------------------------------------------------------------------------------------------


def classify(df: pd.DataFrame, target: str, features: list[str], *, order_by: str | None = None) -> dict[str, Any]:
    import statsmodels.api as sm  # noqa: PLC0415

    d = df[[target, *features] + ([order_by] if order_by and order_by not in features else [])].copy()
    y_raw = d[target]
    vals = sorted(y_raw.dropna().unique().tolist(), key=str)
    if len(vals) != 2:
        raise MethodError(f"classification needs a two-valued target; {target} has {len(vals)} distinct values")
    positive = next((v for v in vals if str(v).lower() in ("1", "true", "yes", "churned", "fraud", "failed", "positive")), vals[-1])
    d["_y"] = (y_raw == positive).astype(float)
    x = d[features].apply(pd.to_numeric, errors="coerce")
    keep = x.notna().all(axis=1) & y_raw.notna()
    d, x = d[keep], x[keep]
    if len(d) < 40 or d["_y"].sum() < 5 or (1 - d["_y"]).sum() < 5:
        raise MethodError("classification needs at least 40 rows with at least 5 of each outcome")
    if order_by:
        idx = d.sort_values(order_by).index
        d, x = d.loc[idx], x.loc[idx]
    cut = int(len(d) * 0.7)
    mu, sd = x.iloc[:cut].mean(), x.iloc[:cut].std(ddof=0).replace(0, 1)
    xs = sm.add_constant((x - mu) / sd, has_constant="add")
    try:
        model = sm.Logit(d["_y"].iloc[:cut], xs.iloc[:cut]).fit(disp=0, maxiter=200)
    except Exception as exc:  # noqa: BLE001 - perfect separation and singular matrices
        raise MethodError(f"the model could not be fitted ({type(exc).__name__}); features may separate the outcome perfectly") from None
    p = model.predict(xs.iloc[cut:])
    yt = d["_y"].iloc[cut:].to_numpy()
    pred = (p.to_numpy() >= 0.5).astype(float)
    tp, fp, fn = float(((pred == 1) & (yt == 1)).sum()), float(((pred == 1) & (yt == 0)).sum()), float(((pred == 0) & (yt == 1)).sum())
    acc = float((pred == yt).mean())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    order = np.argsort(p.to_numpy())
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    npos, nneg = yt.sum(), len(yt) - yt.sum()
    auc = float((ranks[yt == 1].sum() - npos * (npos + 1) / 2) / (npos * nneg)) if npos and nneg else float("nan")
    base = float(max(yt.mean(), 1 - yt.mean()))
    coefs = pd.DataFrame({"feature": features, "coefficient": [model.params[f] for f in features],
                          "p_value": [model.pvalues[f] for f in features]}).round(4)
    coefs = coefs.reindex(coefs["coefficient"].abs().sort_values(ascending=False).index)
    top = coefs.iloc[0]
    return {"summary": f"A logistic model predicts {target} = {positive} with AUC {auc:.2f} on the held-out "
                       f"{len(yt)} rows (accuracy {acc:.0%} against {base:.0%} for always guessing the majority).",
            "findings": [f"Precision {prec:.0%}, recall {rec:.0%} at a 0.5 threshold.",
                         f"Strongest feature: {top['feature']} ({'raises' if top['coefficient'] > 0 else 'lowers'} the odds; "
                         f"p = {top['p_value']:.3f})."],
            "tables": [_table("Coefficients (standardized features)", coefs)],
            "metrics": {"auc": _r(auc), "accuracy": _r(acc), "precision": _r(prec), "recall": _r(rec), "baseline": _r(base),
                        "train_rows": cut, "test_rows": int(len(yt))},
            "assumptions": ["Logistic regression assumes a roughly linear relationship between features and log-odds.",
                            "The last 30% of rows " + (f"by {order_by} " if order_by else "") + "are held out; no tuning on them.",
                            "Coefficients describe association, not cause."]}


# -- process mining -----------------------------------------------------------------------------------------------


def process_map(df: pd.DataFrame, case: str, activity: str, ts: str) -> dict[str, Any]:
    d = df[[case, activity, ts]].dropna().copy()
    d[ts] = pd.to_datetime(d[ts], errors="coerce", utc=True)
    d = d.dropna().sort_values([case, ts])
    if d[case].nunique() < 3:
        raise MethodError("process mining needs an event log with at least 3 cases (case id, activity, timestamp)")
    variants = Counter()
    trans: Counter = Counter()
    waits: dict[tuple, list[float]] = defaultdict(list)
    rework = 0
    cycle = []
    for _, g in d.groupby(case, sort=False):
        acts = g[activity].astype(str).tolist()
        times = g[ts].tolist()
        variants[" → ".join(acts)] += 1
        if len(set(acts)) < len(acts):
            rework += 1
        for a, b, t1, t2 in zip(acts, acts[1:], times, times[1:], strict=False):
            trans[(a, b)] += 1
            waits[(a, b)].append((t2 - t1).total_seconds() / 3600)
        cycle.append((times[-1] - times[0]).total_seconds() / 3600)
    n = sum(variants.values())
    top_v = variants.most_common(5)
    tr = pd.DataFrame([{"from": a, "to": b, "cases": c, "median_wait_hours": _r(np.median(waits[(a, b)]), 2)}
                       for (a, b), c in trans.items()]).sort_values("median_wait_hours", ascending=False)
    slow = tr.iloc[0]
    return {"summary": f"{n} cases follow {len(variants)} distinct paths; the most common covers {top_v[0][1] / n:.0%} of cases. "
                       f"The slowest handoff is {slow['from']} → {slow['to']} (median {slow['median_wait_hours']:.1f} h).",
            "findings": [f"{rework} of {n} cases ({rework / n:.0%}) repeat at least one activity (rework).",
                         f"Median case cycle time {np.median(cycle):.1f} h; 90th percentile {np.percentile(cycle, 90):.1f} h."],
            "tables": [_table("Most common paths", pd.DataFrame([{"path": v, "cases": c, "share": _r(c / n)} for v, c in top_v])),
                       _table("Handoffs by median wait", tr)],
            "metrics": {"cases": n, "variants": len(variants), "rework_cases": rework, "median_cycle_hours": _r(np.median(cycle), 2)},
            "assumptions": ["Events are ordered by timestamp within each case; ties keep their stored order.",
                            "Waiting time is the gap between consecutive events, which includes work time."]}


# -- geospatial ---------------------------------------------------------------------------------------------------


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = (np.radians(v) for v in (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * np.arcsin(np.sqrt(a))


def distances(points: pd.DataFrame, targets: pd.DataFrame, *, lat: str, lon: str, tlat: str, tlon: str,
              label: str | None = None, tlabel: str | None = None, within_km: float | None = None) -> dict[str, Any]:
    p = points.dropna(subset=[lat, lon]).copy()
    t = targets.dropna(subset=[tlat, tlon]).copy()
    if p.empty or t.empty:
        raise MethodError("distance analysis needs latitude and longitude on both sets of points")
    for frame, a, b in ((p, lat, lon), (t, tlat, tlon)):
        if not (frame[a].between(-90, 90).all() and frame[b].between(-180, 180).all()):
            raise MethodError("coordinates must be decimal degrees (latitude -90..90, longitude -180..180)")
    dm = _haversine_km(p[lat].to_numpy()[:, None], p[lon].to_numpy()[:, None], t[tlat].to_numpy()[None, :], t[tlon].to_numpy()[None, :])
    nearest = dm.argmin(axis=1)
    p["nearest"] = t[tlabel].to_numpy()[nearest] if tlabel else nearest
    p["distance_km"] = dm.min(axis=1).round(2)
    out = p[[c for c in (label, "nearest", "distance_km") if c]].sort_values("distance_km", ascending=False)
    findings = [f"Median distance to the nearest target {np.median(p['distance_km']):.1f} km; farthest "
                f"{p['distance_km'].max():.1f} km."]
    summary = f"{len(p)} points measured against {len(t)} targets."
    metrics = {"points": int(len(p)), "targets": int(len(t)), "median_km": _r(np.median(p["distance_km"]), 2)}
    if within_km is not None:
        beyond = int((p["distance_km"] > within_km).sum())
        summary = f"{beyond} of {len(p)} points are more than {within_km:g} km from the nearest target."
        metrics["beyond"] = beyond
    return {"summary": summary, "findings": findings, "tables": [_table("Distance to nearest target (farthest first)", out)],
            "metrics": metrics,
            "assumptions": ["Great-circle (haversine) distance on a spherical Earth; road or travel distance will be longer."]}


# -- reconciliation -----------------------------------------------------------------------------------------------


def reconcile(left: pd.DataFrame, right: pd.DataFrame, *, key: str, value: str, rkey: str | None = None,
              rvalue: str | None = None, tolerance: float = 0.005, names: tuple[str, str] = ("left", "right")) -> dict[str, Any]:
    rkey, rvalue = rkey or key, rvalue or value
    a = left.groupby(key, dropna=False)[value].sum(min_count=1).rename("a")
    b = right.groupby(rkey, dropna=False)[rvalue].sum(min_count=1).rename("b")
    a.index = a.index.astype(str)
    b.index = b.index.astype(str)
    m = pd.concat([a, b], axis=1)
    only_a, only_b = m[m["b"].isna()], m[m["a"].isna()]
    both = m.dropna()
    diff = (both["a"] - both["b"]).abs()
    breaks = both[diff > tolerance * both[["a", "b"]].abs().max(axis=1).clip(lower=1)]
    ta, tb = float(a.sum()), float(b.sum())
    agree = abs(ta - tb) <= tolerance * max(abs(ta), abs(tb), 1)
    rows = pd.concat([only_a.assign(status=f"only in {names[0]}"), only_b.assign(status=f"only in {names[1]}"),
                      breaks.assign(status="amounts differ")]).reset_index(names=key)
    rows = rows.rename(columns={"a": names[0], "b": names[1]})
    return {"summary": (f"Totals {'agree' if agree else 'do not agree'}: {names[0]} {ta:,.2f} vs {names[1]} {tb:,.2f}. "
                        f"{len(both) - len(breaks)} of {len(m)} keys match; {len(only_a)} only in {names[0]}, "
                        f"{len(only_b)} only in {names[1]}, {len(breaks)} with different amounts."),
            "findings": [f"Unexplained difference {ta - tb:,.2f}."] if not agree else ["Every difference is accounted for by the listed keys."],
            "tables": [_table("Reconciliation breaks", rows)],
            "metrics": {"keys": int(len(m)), "matched": int(len(both) - len(breaks)), "only_left": int(len(only_a)),
                        "only_right": int(len(only_b)), "breaks": int(len(breaks)), "total_left": _r(ta, 2),
                        "total_right": _r(tb, 2), "totals_agree": bool(agree)},
            "assumptions": [f"Amounts are summed per key and compared within a relative tolerance of {tolerance:.1%}.",
                            "Both sources use the same currency and period; check that before reading breaks."]}


# -- graphs -------------------------------------------------------------------------------------------------------


def graph(edges: pd.DataFrame, src: str, dst: str) -> dict[str, Any]:
    e = edges[[src, dst]].dropna().astype(str)
    if e.empty:
        raise MethodError("graph analysis needs an edge list (two columns naming connected entities)")
    adj: dict[str, set[str]] = defaultdict(set)
    for a, b in e.itertuples(index=False):
        adj[a].add(b)
        adj[b].add(a)
    seen, comps = set(), []
    for node in adj:
        if node in seen:
            continue
        stack, comp = [node], set()
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            stack.extend(adj[x] - comp)
        seen |= comp
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    deg = pd.DataFrame([{"node": n, "connections": len(v)} for n, v in adj.items()]).sort_values(
        ["connections", "node"], ascending=[False, True])
    return {"summary": f"{len(adj)} entities and {len(e)} links form {len(comps)} connected group(s); the largest has "
                       f"{len(comps[0])} entities, and {deg.iloc[0]['node']} is the most connected ({deg.iloc[0]['connections']} links).",
            "findings": [f"{sum(1 for c in comps if len(c) == 1)} entities are isolated." if any(len(c) == 1 for c in comps)
                         else "Every entity has at least one link."],
            "tables": [_table("Most connected entities", deg)],
            "metrics": {"nodes": len(adj), "edges": int(len(e)), "components": len(comps), "largest": len(comps[0])},
            "assumptions": ["Links are treated as undirected and unweighted."]}


# -- event logs ---------------------------------------------------------------------------------------------------


def log_patterns(df: pd.DataFrame, *, ts: str, event: str, entity: str | None = None, bucket: str = "h") -> dict[str, Any]:
    d = df.dropna(subset=[ts, event]).copy()
    d[ts] = pd.to_datetime(d[ts], errors="coerce", utc=True)
    d = d.dropna(subset=[ts])
    if len(d) < 20:
        raise MethodError("log analysis needs at least 20 timestamped events")
    mix = d[event].astype(str).value_counts()
    rare = mix[mix <= max(1, int(0.01 * len(d)))]
    findings = [f"{len(mix)} event types; the most common is {mix.index[0]} ({mix.iloc[0] / len(d):.0%})."]
    if len(rare):
        findings.append(f"{len(rare)} rare event type(s), for example {', '.join(rare.index[:3])}.")
    tables = [_table("Event mix", mix.rename_axis("event").reset_index(name="count"))]
    metrics: dict[str, Any] = {"events": int(len(d)), "types": int(len(mix)), "rare_types": int(len(rare))}
    bursts = pd.DataFrame()
    if entity:
        d["_b"] = d[ts].dt.floor(bucket)
        per = d.groupby([entity, "_b"]).size().rename("events").reset_index()
        stats = per.groupby(entity)["events"].agg(["median", "max", "count"])
        mad = per.groupby(entity)["events"].apply(lambda s: float(np.median(np.abs(s - s.median()))) or 1.0)
        stats["score"] = (stats["max"] - stats["median"]) / (1.4826 * mad)
        bursts = stats[(stats["count"] >= 3) & (stats["score"] >= 5)].sort_values("score", ascending=False).reset_index()
        metrics["entities"] = int(stats.shape[0])
        metrics["bursting_entities"] = int(len(bursts))
        findings.append(f"{len(bursts)} of {stats.shape[0]} {entity} values show a burst far above their own usual rate."
                        if len(bursts) else f"No {entity} shows a burst far above its own usual rate.")
        if len(bursts):
            tables.append(_table(f"Bursts by {entity} (robust score)", bursts.round(2)))
    return {"summary": f"{len(d)} events from {d[ts].min():%Y-%m-%d %H:%M} to {d[ts].max():%Y-%m-%d %H:%M} UTC. " + findings[-1],
            "findings": findings, "tables": tables, "metrics": metrics,
            "assumptions": ["Timestamps are normalized to UTC.", "A burst is a bucket at least five robust deviations above that "
                            "entity's own median; it is an observed pattern, not a confirmed threat."]}


# -- two-group comparison -----------------------------------------------------------------------------------------


def compare_groups(df: pd.DataFrame, group: str, outcome: str) -> dict[str, Any]:
    """A/B readout: two-proportion z-test for a 0/1 outcome, Welch's t-test otherwise; lift with a 95% interval and a
    sample-ratio check."""
    from scipy import stats as sps  # noqa: PLC0415

    d = df[[group, outcome]].dropna()
    groups = sorted(d[group].astype(str).unique().tolist())
    if len(groups) != 2:
        raise MethodError(f"a comparison needs exactly two groups in {group}; found {len(groups)}")
    control = next((g for g in groups if g.lower() in ("control", "a", "baseline", "holdout", "old")), groups[0])
    treat = next(g for g in groups if g != control)
    a = pd.to_numeric(d.loc[d[group].astype(str) == control, outcome], errors="coerce").dropna()
    b = pd.to_numeric(d.loc[d[group].astype(str) == treat, outcome], errors="coerce").dropna()
    if min(len(a), len(b)) < 30:
        raise MethodError("each group needs at least 30 observations")
    binary = set(pd.concat([a, b]).unique().tolist()) <= {0, 1}
    if binary:
        pa, pb = a.mean(), b.mean()
        pool = (a.sum() + b.sum()) / (len(a) + len(b))
        se0 = math.sqrt(pool * (1 - pool) * (1 / len(a) + 1 / len(b)))
        z = (pb - pa) / se0 if se0 else 0.0
        p = float(2 * (1 - sps.norm.cdf(abs(z))))
        se = math.sqrt(pa * (1 - pa) / len(a) + pb * (1 - pb) / len(b))
        test = "two-proportion z-test"
    else:
        pa, pb = a.mean(), b.mean()
        t = sps.ttest_ind(b, a, equal_var=False)
        p = float(t.pvalue)
        se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
        test = "Welch's t-test"
    diff = pb - pa
    lo, hi = diff - 1.96 * se, diff + 1.96 * se
    srm = float(sps.chisquare([len(a), len(b)]).pvalue)
    sig = p < 0.05
    fmt = (lambda x: f"{x:.2%}") if binary else (lambda x: f"{x:,.3g}")
    findings = [f"{control}: {fmt(pa)} over {len(a):,}; {treat}: {fmt(pb)} over {len(b):,}.",
                f"Difference {fmt(diff)} (95% interval {fmt(lo)} to {fmt(hi)}), {test}, p = {p:.3g}."]
    if srm < 0.01:
        findings.append(f"Sample ratio mismatch: the groups differ in size more than chance allows (p = {srm:.3g}); "
                        "check the assignment before trusting the result.")
    verdict = ("is higher than" if diff > 0 else "is lower than") if sig else "is not distinguishable from"
    return {"summary": (f"{treat} {verdict} {control} on {outcome}: {fmt(pb)} vs {fmt(pa)}"
                        + (f" (p = {p:.3g})." if sig else f"; p = {p:.3g}, not significant.")),
            "findings": findings, "tables": [_table("Groups", pd.DataFrame([
                {"group": control, "n": len(a), "mean": _r(pa, 4)}, {"group": treat, "n": len(b), "mean": _r(pb, 4)}]))],
            "metrics": {"difference": _r(diff, 5), "ci_low": _r(lo, 5), "ci_high": _r(hi, 5), "p_value": _r(p, 5),
                        "srm_p": _r(srm, 5), "significant": bool(sig)},
            "assumptions": ["Units were randomly assigned and observed once each.",
                            "A single pre-planned comparison; looking repeatedly or at many metrics needs a correction."]}


# -- document briefs ----------------------------------------------------------------------------------------------


def brief(passages: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """An extractive brief: the passages that answer the question, quoted with their source, nothing paraphrased."""
    if not passages:
        raise MethodError("no document passage matches this question")
    docs = []
    for p in passages:
        if p["title"] not in docs:
            docs.append(p["title"])
    top = passages[0]
    excerpt = " ".join(str(top["text"]).split())
    excerpt = excerpt if len(excerpt) <= 320 else excerpt[:319].rsplit(" ", 1)[0] + "…"
    return {"summary": f"From “{top['title']}”: {excerpt}",
            "findings": [f"“{p['title']}”, passage {p['passage'] + 1}: " + " ".join(str(p["text"]).split())[:400]
                         for p in passages[1:4]],
            "tables": [_table("Sources", pd.DataFrame([{"document": p["title"], "passage": p["passage"] + 1,
                                                        "relevance": p["score"]} for p in passages]))],
            "metrics": {"passages": len(passages), "documents": len(docs)},
            "assumptions": ["Quoted from the workspace's own documents, ranked by relevance; nothing is paraphrased or added.",
                            "Only documents your role may read were searched."]}


# -- model evaluation ---------------------------------------------------------------------------------------------


def evaluate_predictions(df: pd.DataFrame, actual: str, predicted: str | None = None, *, segment: str | None = None,
                         passed: str | None = None) -> dict[str, Any]:
    """How good are recorded predictions? Classification (accuracy, precision, recall per class), regression (MAE,
    MAPE, bias) or pass/fail evaluation results, overall and by segment, with the worst segment named."""
    cols = [c for c in (actual, predicted, segment, passed) if c]
    d = df[[c for c in dict.fromkeys(cols) if c in df.columns]].dropna()
    if len(d) < 20:
        raise MethodError("evaluation needs at least 20 scored rows")
    if passed:
        ok = pd.to_numeric(d[passed], errors="coerce").fillna(0).astype(int)
        by = d.assign(_ok=ok).groupby(segment)["_ok"].agg(["mean", "count"]).rename(columns={"mean": "pass_rate"}) if segment \
            else None
        rate = float(ok.mean())
        lo = rate - 1.96 * math.sqrt(rate * (1 - rate) / len(ok))
        hi = rate + 1.96 * math.sqrt(rate * (1 - rate) / len(ok))
        findings = [f"Pass rate {rate:.1%} (95% interval {max(lo, 0):.1%} to {min(hi, 1):.1%}) over {len(ok)} cases."]
        tables = []
        worst = None
        if by is not None:
            by = by.sort_values("pass_rate").reset_index()
            worst = by.iloc[0]
            findings.append(f"Lowest: {worst[segment]} at {worst['pass_rate']:.1%} over {int(worst['count'])} cases.")
            tables.append(_table(f"Pass rate by {segment}", by.round(4)))
        return {"summary": f"Overall pass rate {rate:.1%} on {len(ok)} cases" + (f"; {worst[segment]} is weakest "
                           f"({worst['pass_rate']:.1%})." if worst is not None else "."),
                "findings": findings, "tables": tables, "metrics": {"pass_rate": _r(rate, 4), "cases": int(len(ok))},
                "assumptions": ["Each case is scored pass or fail by a recorded check; the cases represent real use."]}
    a, p = d[actual], d[predicted]
    numeric = pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(p) and a.nunique() > 10
    if numeric:
        err = (p - a).astype(float)
        mae, bias = float(err.abs().mean()), float(err.mean())
        nz = a != 0
        mape = float((err[nz].abs() / a[nz].abs()).mean()) if nz.any() else float("nan")
        frame = d.assign(_abs=err.abs())
        summary = f"Mean absolute error {mae:,.3g} (MAPE {mape:.1%}), bias {bias:+,.3g} over {len(d)} predictions"
        metrics = {"mae": _r(mae, 4), "mape": _r(mape, 4), "bias": _r(bias, 4), "rows": int(len(d))}
        key = "_abs"
        worst_desc = "highest error"
    else:
        correct = (a.astype(str) == p.astype(str))
        acc = float(correct.mean())
        frame = d.assign(_wrong=(~correct).astype(float))
        classes = sorted(a.astype(str).unique().tolist())
        rows = []
        for c in classes:
            tp = int(((a.astype(str) == c) & (p.astype(str) == c)).sum())
            fp = int(((a.astype(str) != c) & (p.astype(str) == c)).sum())
            fn = int(((a.astype(str) == c) & (p.astype(str) != c)).sum())
            rows.append({"class": c, "precision": _r(tp / (tp + fp) if tp + fp else 0, 3), "recall": _r(tp / (tp + fn) if tp + fn else 0, 3),
                         "support": int((a.astype(str) == c).sum())})
        summary = f"Accuracy {acc:.1%} over {len(d)} predictions across {len(classes)} classes"
        metrics = {"accuracy": _r(acc, 4), "rows": int(len(d)), "classes": len(classes)}
        key = "_wrong"
        worst_desc = "lowest accuracy"
    tables, findings = [], []
    if not numeric:
        tables.append(_table("Per class", pd.DataFrame(rows)))
    if segment:
        by = frame.groupby(segment)[key].agg(["mean", "count"]).sort_values("mean", ascending=False).reset_index()
        worst = by.iloc[0]
        val = f"{1 - worst['mean']:.1%} accurate" if not numeric else f"MAE {worst['mean']:,.3g}"
        findings.append(f"{worst[segment]} has the {worst_desc} ({val} over {int(worst['count'])} rows).")
        summary += f"; {worst[segment]} is the weakest segment"
        tables.append(_table(f"By {segment}", by.rename(columns={"mean": "error_rate" if not numeric else "mae"}).round(4)))
    return {"summary": summary + ".", "findings": findings, "tables": tables, "metrics": metrics,
            "assumptions": ["Predictions were recorded before the outcomes were known (no leakage).",
                            "Rows are scored once each; the evaluation set represents the population the model serves."]}
