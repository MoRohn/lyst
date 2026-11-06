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
    if len(vals) > 2:
        return _classify_multi(df, target, features, order_by=order_by)
    if len(vals) != 2:
        raise MethodError(f"classification needs a target with at least two values; {target} has {len(vals)}")
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


def _classify_multi(df: pd.DataFrame, target: str, features: list[str], *, order_by: str | None = None) -> dict[str, Any]:
    import statsmodels.api as sm  # noqa: PLC0415

    d = df[[target, *features] + ([order_by] if order_by and order_by not in features else [])].dropna().copy()
    if order_by:
        d = d.sort_values(order_by)
    classes = sorted(d[target].astype(str).unique().tolist())
    if len(classes) > 12:
        raise MethodError(f"{target} has {len(classes)} classes; at most 12 are supported")
    counts = d[target].astype(str).value_counts()
    if len(d) < 20 * len(classes) or counts.min() < 5:
        raise MethodError("multi-class classification needs at least 20 rows per class and 5 of the rarest")
    y = pd.Categorical(d[target].astype(str), categories=classes).codes
    x = d[features].apply(pd.to_numeric, errors="coerce")
    cut = int(len(d) * 0.7)
    mu, sd = x.iloc[:cut].mean(), x.iloc[:cut].std(ddof=0).replace(0, 1)
    xs = sm.add_constant((x - mu) / sd, has_constant="add")
    try:
        model = sm.MNLogit(y[:cut], xs.iloc[:cut]).fit(disp=0, maxiter=300)
    except Exception as exc:  # noqa: BLE001
        raise MethodError(f"the model could not be fitted ({type(exc).__name__})") from None
    pred = np.asarray(model.predict(xs.iloc[cut:])).argmax(axis=1)
    yt = y[cut:]
    acc = float((pred == yt).mean())
    base = float(pd.Series(y[:cut]).value_counts(normalize=True).max())
    rows = []
    for k, c in enumerate(classes):
        tp = int(((pred == k) & (yt == k)).sum())
        fp = int(((pred == k) & (yt != k)).sum())
        fn = int(((pred != k) & (yt == k)).sum())
        rows.append({"class": c, "precision": _r(tp / (tp + fp) if tp + fp else 0, 3), "recall": _r(tp / (tp + fn) if tp + fn else 0, 3),
                     "support": int((yt == k).sum())})
    per = pd.DataFrame(rows).sort_values("recall")
    weakest = per.iloc[0]
    return {"summary": f"A multinomial logistic model predicts {target} across {len(classes)} classes with accuracy {acc:.0%} on the "
                       f"held-out {len(yt)} rows (against {base:.0%} for always guessing the commonest class).",
            "findings": [f"Hardest class: {weakest['class']} (recall {weakest['recall']:.0%})."],
            "tables": [_table("Per class (held out)", per)],
            "metrics": {"accuracy": _r(acc), "baseline": _r(base), "classes": len(classes), "test_rows": int(len(yt))},
            "assumptions": ["Multinomial logistic regression: linear decision boundaries in the standardized features.",
                            "The last 30% of rows " + (f"by {order_by} " if order_by else "") + "are held out."]}


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


def graph(edges: pd.DataFrame, src: str, dst: str, *, query: str | None = None) -> dict[str, Any]:
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
    bc = betweenness(adj) if len(adj) <= 2000 else {}
    comm = communities(adj)
    deg = pd.DataFrame([{"node": n, "connections": len(v), "betweenness": _r(bc.get(n, 0.0), 4), "community": comm[n]}
                        for n, v in adj.items()]).sort_values(["connections", "betweenness", "node"], ascending=[False, False, True])
    broker = max(bc, key=lambda v: (bc[v], v)) if bc else None
    found = [n for n in adj if n.lower() in (query or "").lower()]
    path = shortest_path(adj, found[0], found[1]) if len(found) >= 2 else None
    findings = [f"{sum(1 for c in comps if len(c) == 1)} entities are isolated." if any(len(c) == 1 for c in comps)
                else "Every entity has at least one link."]
    if broker is not None and bc[broker] > 0:
        findings.append(f"{broker} sits on the most shortest paths between others (betweenness {bc[broker]:.2f}): "
                        "removing it would disconnect the most pairs.")
    findings.append(f"{len(set(comm.values()))} communities by label propagation.")
    if path:
        findings.append("Shortest path: " + " → ".join(path) + f" ({len(path) - 1} links).")
    return {"summary": f"{len(adj)} entities and {len(e)} links form {len(comps)} connected group(s); the largest has "
                       f"{len(comps[0])} entities, and {deg.iloc[0]['node']} is the most connected ({deg.iloc[0]['connections']} links)"
                       + (f"; {broker} is the main broker between them." if broker is not None and bc[broker] > 0 else "."),
            "findings": findings,
            "tables": [_table("Most connected entities", deg)],
            "metrics": {"nodes": len(adj), "edges": int(len(e)), "components": len(comps), "largest": len(comps[0])},
            "assumptions": ["Links are treated as undirected and unweighted.",
                            "Betweenness by Brandes' algorithm; communities by deterministic label propagation."]}


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
        seq = failure_then_success(d, ts=ts, event=event, entity=entity)
        metrics["failure_then_success"] = int(len(seq))
        if len(seq):
            findings.append(f"{len(seq)} {entity} value(s) succeeded after repeated failures, for example "
                            f"{seq.iloc[0]['entity']} ({seq.iloc[0]['failures']} failures, then a success at "
                            f"{seq.iloc[0]['success_at'][:16]}): an observed sequence, not a confirmed compromise.")
            tables.append(_table("Failures followed by a success", seq))
            worst = seq.iloc[0]["entity"]
            tl = d[d[entity].astype(str) == str(worst)].sort_values(ts).tail(15)
            tables.append(_table(f"Timeline for {worst}", tl[[ts, event]].astype(str)))
    lead = next((f for f in findings if "succeeded after repeated failures" in f), findings[-1])
    return {"summary": f"{len(d)} events from {d[ts].min():%Y-%m-%d %H:%M} to {d[ts].max():%Y-%m-%d %H:%M} UTC. " + lead,
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


# -- raw log parsing ----------------------------------------------------------------------------------------------

import json as _json  # noqa: E402
import re as _re  # noqa: E402

_KV = _re.compile(r'(\w[\w.-]*)=("[^"]*"|\S+)')
_ACCESS = _re.compile(r'^(?P<ip>\S+) \S+ (?P<user>\S+) \[(?P<ts>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+)[^"]*" (?P<status>\d{3})')
_SYSLOG = _re.compile(r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2} \d\d:\d\d:\d\d) (?P<host>\S+) (?P<prog>[\w./-]+)(?:\[\d+\])?: (?P<msg>.*)$")
_ISO = _re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d)?(\.\d+)?(Z|[+-]\d\d:?\d\d)?")
_ENTITY_KEYS = ("user", "username", "account", "src", "src_ip", "ip", "client", "host", "source", "service")
_EVENT_KEYS = ("event", "action", "status", "outcome", "type", "msg", "message")


def parse_logs(lines: pd.Series, *, year: int = 2026) -> pd.DataFrame:
    """Raw log lines in the common formats (JSON lines, key=value, web access logs, syslog) to events with a timestamp,
    an event name and the entity it concerns. Lines that match no format are counted, not guessed."""
    rows = []
    for raw in lines.dropna().astype(str):
        line = raw.strip()
        rec: dict[str, Any] = {}
        if line.startswith("{"):
            try:
                rec = {str(k).lower(): v for k, v in _json.loads(line).items()}
            except ValueError:
                rec = {}
        if not rec and (m := _ACCESS.match(line)):
            rec = {"ts": pd.to_datetime(m["ts"], format="%d/%b/%Y:%H:%M:%S %z", errors="coerce"), "ip": m["ip"],
                   "user": None if m["user"] == "-" else m["user"], "event": f"http_{m['status']}", "path": m["path"]}
        if not rec and (m := _SYSLOG.match(line)):
            msg = m["msg"]
            kv = {k.lower(): v.strip('"') for k, v in _KV.findall(msg)}
            word = ("failed" if _re.search(r"\bfail\w*|invalid|denied\b", msg, _re.I)
                    else "accepted" if _re.search(r"\baccepted|success\w*\b", msg, _re.I) else m["prog"])
            rec = {"ts": pd.to_datetime(f"{year} {m['ts']}", format="%Y %b %d %H:%M:%S", errors="coerce"), "host": m["host"],
                   "event": word, **kv}
            if "user" not in rec and (u := _re.search(r"for (?:invalid user )?(\w+)", msg)):
                rec["user"] = u.group(1)
            if "ip" not in rec and (ip := _re.search(r"from (\d+\.\d+\.\d+\.\d+)", msg)):
                rec["ip"] = ip.group(1)
        if not rec:
            kv = {k.lower(): v.strip('"') for k, v in _KV.findall(line)}
            if kv:
                rec = kv
                if "ts" not in rec and (iso := _ISO.search(line)):
                    rec["ts"] = iso.group(0)
        if not rec:
            rows.append({"_unparsed": True})
            continue
        ts = rec.get("ts") or rec.get("time") or rec.get("timestamp") or rec.get("@timestamp")
        entity = next((rec[k] for k in _ENTITY_KEYS if rec.get(k)), None)
        event = next((rec[k] for k in _EVENT_KEYS if rec.get(k)), None)
        rows.append({"ts": pd.to_datetime(ts, errors="coerce", utc=True), "event": str(event) if event is not None else None,
                     "entity": str(entity) if entity is not None else None, "_unparsed": False})
    out = pd.DataFrame(rows)
    if out.empty or "ts" not in out:
        raise MethodError("no line matched a known log format (JSON lines, key=value, web access log, syslog)")
    return out


_FAIL = _re.compile(r"fail|denied|invalid|error|reject|http_401|http_403", _re.I)
_OK = _re.compile(r"success|accepted|ok\b|login_success|http_200", _re.I)


def failure_then_success(d: pd.DataFrame, *, ts: str, event: str, entity: str, threshold: int = 5,
                         window: str = "30min") -> pd.DataFrame:
    """Entities with at least ``threshold`` failures followed by a success within ``window``: the classic pattern of a
    guessed password, reported as an observed sequence (not a confirmed compromise)."""
    hits = []
    span = pd.Timedelta(window)
    for ent, g in d.sort_values(ts).groupby(entity):
        fails: list[pd.Timestamp] = []
        for t, ev in zip(g[ts], g[event].astype(str), strict=True):
            if _FAIL.search(ev):
                fails.append(t)
                fails = [f for f in fails if t - f <= span]
            elif _OK.search(ev) and len(fails) >= threshold:
                hits.append({"entity": ent, "failures": len(fails), "first_failure": str(fails[0]), "success_at": str(t)})
                fails = []
    return pd.DataFrame(hits)


# -- graph paths, centrality and communities ----------------------------------------------------------------------


def _adjacency(e: pd.DataFrame) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for a, b in e.itertuples(index=False):
        adj[a].add(b)
        adj[b].add(a)
    return adj


def betweenness(adj: dict[str, set[str]]) -> dict[str, float]:
    """Brandes' algorithm, unweighted, normalized to 0..1."""
    nodes = sorted(adj)
    bc = dict.fromkeys(nodes, 0.0)
    for s in nodes:
        stack, pred = [], {v: [] for v in nodes}
        sigma = dict.fromkeys(nodes, 0.0)
        dist = dict.fromkeys(nodes, -1)
        sigma[s], dist[s] = 1.0, 0
        queue = [s]
        while queue:
            v = queue.pop(0)
            stack.append(v)
            for w in sorted(adj[v]):
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    n = len(nodes)
    scale = 1 / ((n - 1) * (n - 2)) if n > 2 else 1.0
    return {v: bc[v] * scale for v in nodes}


def communities(adj: dict[str, set[str]], *, rounds: int = 20) -> dict[str, str]:
    """Deterministic label propagation: each node takes its neighbours' most common label (ties: smallest)."""
    label = {v: v for v in adj}
    for _ in range(rounds):
        changed = False
        for v in sorted(adj):
            if not adj[v]:
                continue
            counts = Counter(label[w] for w in adj[v])
            best = min((-c, lab) for lab, c in counts.items())[1]
            if label[v] != best:
                label[v], changed = best, True
        if not changed:
            break
    return label


def shortest_path(adj: dict[str, set[str]], a: str, b: str) -> list[str] | None:
    prev, queue, seen = {a: None}, [a], {a}
    while queue:
        v = queue.pop(0)
        if v == b:
            path = []
            while v is not None:
                path.append(v)
                v = prev[v]
            return path[::-1]
        for w in sorted(adj[v]):
            if w not in seen:
                seen.add(w)
                prev[w] = v
                queue.append(w)
    return None


# -- optimization -------------------------------------------------------------------------------------------------


def optimize_allocation(df: pd.DataFrame, *, option: str, cost: str, value: str, budget: float,
                        max_units: str | None = None, min_units: str | None = None, integer: bool = True) -> dict[str, Any]:
    """Maximize total value subject to a budget and per-option limits: a linear (or integer) program solved exactly
    with SciPy's HiGHS solver."""
    from scipy.optimize import Bounds, LinearConstraint, milp  # noqa: PLC0415

    d = df[[c for c in (option, cost, value, max_units, min_units) if c]].dropna(subset=[option, cost, value]).copy()
    if d.empty or budget <= 0:
        raise MethodError("optimization needs options with a cost and a value per unit, and a positive budget")
    c = -d[value].astype(float).to_numpy()
    a = d[cost].astype(float).to_numpy()[None, :]
    lo = d[min_units].astype(float).to_numpy() if min_units else np.zeros(len(d))
    hi = d[max_units].astype(float).to_numpy() if max_units else np.full(len(d), np.inf)
    res = milp(c, constraints=LinearConstraint(a, -np.inf, budget), bounds=Bounds(lo, hi),
               integrality=np.ones(len(d)) if integer else np.zeros(len(d)))
    if not res.success:
        raise MethodError(f"no feasible allocation within the budget ({res.message})")
    d["units"] = np.round(res.x, 4)
    d["spend"] = d["units"] * d[cost]
    d["expected_value"] = d["units"] * d[value]
    plan = d[[option, "units", "spend", "expected_value"]].sort_values("expected_value", ascending=False)
    top = plan.iloc[0]
    binding = [str(r[option]) for _, r in d.iterrows() if max_units and abs(r["units"] - r[max_units]) < 1e-6]
    total_v, total_s = float(d["expected_value"].sum()), float(d["spend"].sum())
    return {"summary": f"Best allocation of a {budget:,.0f} budget: {top[option]} gets the most ({top['units']:,.0f} units); "
                       f"expected value {total_v:,.0f} for {total_s:,.0f} spent.",
            "findings": ([f"Options at their limit: {', '.join(binding)}; raising those limits would add value."] if binding else [])
            + [f"{(d['units'] == 0).sum()} option(s) get nothing at this budget."],
            "tables": [_table("Allocation", plan.round(2))],
            "metrics": {"expected_value": _r(total_v, 2), "spend": _r(total_s, 2), "budget": _r(budget, 2)},
            "assumptions": ["Value per unit is constant (no diminishing returns) and options are independent.",
                            "Solved exactly as an integer program (HiGHS)." if integer else "Solved as a linear program (HiGHS)."]}


# -- text analytics -----------------------------------------------------------------------------------------------

_POS = {"great", "good", "love", "excellent", "fast", "helpful", "easy", "happy", "thanks", "resolved", "perfect"}
_NEG = {"bad", "slow", "broken", "refund", "angry", "terrible", "late", "missing", "wrong", "cancel", "worst", "issue",
        "problem", "frustrated", "charged", "error"}
_TEXT_STOP = set("a an the and or to of in on for is are was were be it this that with my i we you our your me at by from "
                 "not no but have has had do does did can could will would just very so as if they them their its".split())


def text_themes(df: pd.DataFrame, text: str, *, group: str | None = None, top: int = 8) -> dict[str, Any]:
    """Themes (terms that stand out, TF-IDF across rows), lexicon sentiment and simple entities (emails, amounts,
    order numbers) in a text column."""
    d = df.dropna(subset=[text]).copy()
    if len(d) < 10:
        raise MethodError("text analysis needs at least 10 rows of text")
    docs = [[w for w in _re.findall(r"[a-z][a-z'-]+", s.lower()) if w not in _TEXT_STOP and len(w) > 2] for s in d[text].astype(str)]
    df_count = Counter(w for doc in docs for w in set(doc))
    n = len(docs)
    score: Counter = Counter()
    for doc in docs:
        tf = Counter(doc)
        for w, c in tf.items():
            score[w] += c / max(len(doc), 1) * math.log(1 + n / df_count[w])
    themes = [(w, df_count[w]) for w, _ in score.most_common(top * 3) if df_count[w] >= max(2, int(0.03 * n))][:top]
    sent = [sum(w in _POS for w in doc) - sum(w in _NEG for w in doc) for doc in docs]
    d["_sent"] = np.sign(sent)
    share_neg = float((d["_sent"] < 0).mean())
    ents = Counter()
    for s in d[text].astype(str):
        ents.update(_re.findall(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", s) and ["email address"] or [])
        ents.update(["money amount"] * len(_re.findall(r"[$€£]\s?\d[\d,.]*", s)))
        ents.update(["order number"] * len(_re.findall(r"\b(?:order|ord)[ #-]?\d{3,}\b", s, _re.I)))
    tables = [_table("Themes (rows mentioning)", pd.DataFrame(themes, columns=["theme", "rows"]))]
    findings = [f"{share_neg:.0%} of rows read as negative, {(d['_sent'] > 0).mean():.0%} as positive (lexicon-based)."]
    if group:
        by = d.groupby(group)["_sent"].apply(lambda s: float((s < 0).mean())).sort_values(ascending=False)
        findings.append(f"Most negative {group}: {by.index[0]} ({by.iloc[0]:.0%} negative).")
        tables.append(_table(f"Negative share by {group}", by.rename("negative_share").reset_index().round(3)))
    if ents:
        findings.append("Entities mentioned: " + ", ".join(f"{k} ({v})" for k, v in ents.most_common(3)) + ".")
    lead = ", ".join(t for t, _ in themes[:3]) or "no recurring terms"
    return {"summary": f"Across {n} rows the leading themes are {lead}; {share_neg:.0%} of rows are negative.",
            "findings": findings, "tables": tables,
            "metrics": {"rows": n, "negative_share": _r(share_neg, 3), "themes": len(themes)},
            "assumptions": ["Themes are distinctive terms (TF-IDF), not a trained topic model.",
                            "Sentiment uses a small word list; sarcasm and negation are not handled."]}


# -- polygons and network travel ----------------------------------------------------------------------------------


def _ring(geometry: Any) -> list[tuple[float, float]]:
    """A polygon's outer ring as (lon, lat) pairs, from GeoJSON (Polygon or MultiPolygon's first part) or
    "lon lat; lon lat; ..." text."""
    g = geometry
    if isinstance(g, str) and g.strip().startswith("{"):
        g = _json.loads(g)
    if isinstance(g, dict):
        coords = g.get("coordinates") or []
        if g.get("type") == "MultiPolygon":
            coords = coords[0] if coords else []
        return [(float(x), float(y)) for x, y, *_ in (coords[0] if coords else [])]
    pts = []
    for part in str(g).split(";"):
        nums = _re.findall(r"-?\d+(?:\.\d+)?", part)
        if len(nums) >= 2:
            pts.append((float(nums[0]), float(nums[1])))
    return pts


def point_in_polygon(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray casting (even-odd rule); points on an edge count as inside."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon <= x:
                inside = not inside
    return inside


def regions(points: pd.DataFrame, polygons: pd.DataFrame, *, lat: str, lon: str, region: str, geometry: str,
            label: str | None = None) -> dict[str, Any]:
    rings = [(str(r[region]), _ring(r[geometry])) for _, r in polygons.iterrows()]
    rings = [(name, ring) for name, ring in rings if len(ring) >= 3]
    if not rings:
        raise MethodError("no polygon could be read (GeoJSON Polygon/MultiPolygon or 'lon lat; lon lat; ...' rings)")
    p = points.dropna(subset=[lat, lon]).copy()
    p["region"] = [next((name for name, ring in rings if point_in_polygon(x, y, ring)), None) for x, y in zip(p[lon], p[lat], strict=True)]
    counts = p["region"].value_counts().rename_axis("region").reset_index(name="points")
    outside = int(p["region"].isna().sum())
    top = counts.iloc[0] if len(counts) else None
    return {"summary": (f"{top['region']} contains the most points ({int(top['points'])} of {len(p)})" if top is not None else
                        "No point falls inside any region") + (f"; {outside} fall outside every region." if outside else "."),
            "findings": [f"{len(rings)} regions read; {len(counts)} contain at least one point."],
            "tables": [_table("Points per region", counts),
                       _table("Points and their region", p[[c for c in (label, lat, lon, "region") if c]].astype(str))],
            "metrics": {"regions": len(rings), "points": int(len(p)), "outside": outside},
            "assumptions": ["Point-in-polygon by ray casting on longitude/latitude (no projection); holes are ignored."]}


def travel_times(edges: pd.DataFrame, *, src: str, dst: str, cost: str, origin: str, within: float | None = None,
                 directed: bool = False) -> dict[str, Any]:
    """Dijkstra shortest travel cost from ``origin`` to every node of a supplied network."""
    import heapq  # noqa: PLC0415

    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for a, b, c in edges[[src, dst, cost]].dropna().itertuples(index=False):
        if float(c) < 0:
            raise MethodError("travel costs must not be negative")
        adj[str(a)].append((str(b), float(c)))
        if not directed:
            adj[str(b)].append((str(a), float(c)))
    if origin not in adj:
        raise MethodError(f"{origin} is not a node of the network")
    dist, prev, heap = {origin: 0.0}, {origin: None}, [(0.0, origin)]
    while heap:
        d, v = heapq.heappop(heap)
        if d > dist.get(v, math.inf):
            continue
        for w, c in adj[v]:
            if d + c < dist.get(w, math.inf):
                dist[w], prev[w] = d + c, v
                heapq.heappush(heap, (d + c, w))
    nodes = sorted(set(adj))
    rows = []
    for n in nodes:
        if n == origin:
            continue
        path, v = [], n
        while v is not None and n in dist:
            path.append(v)
            v = prev[v]
        rows.append({"node": n, "travel": _r(dist[n], 2) if n in dist else None, "via": " → ".join(reversed(path)) if n in dist else ""})
    t = pd.DataFrame(rows).sort_values("travel", ascending=False, na_position="first")
    unreachable = int(t["travel"].isna().sum())
    beyond = t[t["travel"] > within] if within is not None else t.iloc[0:0]
    far = t.dropna().iloc[0] if t["travel"].notna().any() else None
    summary = (f"{len(beyond)} of {len(t)} places are more than {within:g} from {origin}" if within is not None
               else f"The farthest place from {origin} is {far['node']} ({far['travel']:g})" if far is not None else
               f"Nothing is reachable from {origin}")
    findings = [f"Farthest: {far['node']} at {far['travel']:g} via {far['via']}."] if far is not None else []
    if len(beyond):
        findings.insert(0, "Beyond the limit: " + ", ".join(beyond["node"].astype(str).tolist()[:8]) + ".")
    if unreachable:
        findings.append(f"{unreachable} place(s) cannot be reached on this network.")
    return {"summary": summary + ".", "findings": findings, "tables": [_table(f"Travel from {origin}", t)],
            "metrics": {"nodes": len(nodes), "beyond": int(len(beyond)), "unreachable": unreachable},
            "assumptions": ["Shortest paths by Dijkstra on the supplied network and its travel costs; no live traffic.",
                            "Links are two-way unless the network is marked directed."]}


# -- data cleaning ------------------------------------------------------------------------------------------------


def _norm_name(s: str) -> str:
    s = _re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    s = _re.sub(r"\b(inc|incorporated|ltd|limited|llc|gmbh|corp|corporation|co|company|plc|the)\b", " ", s)
    return " ".join(s.split())


def clean_frame(df: pd.DataFrame, *, key: str | None = None, name: str | None = None,
                threshold: float = 0.92) -> tuple[pd.DataFrame, dict[str, Any]]:
    """A cleaned copy and what changed: whitespace trimmed, numbers stored as text converted, exact duplicate rows
    dropped, near-duplicate names merged (``difflib`` ratio >= ``threshold`` within a blocking letter, the first
    record surviving), and missing numbers filled with the column median with an ``_imputed`` flag beside them."""
    from difflib import SequenceMatcher  # noqa: PLC0415

    d = df.copy()
    trimmed = 0
    for c in d.columns:
        if d[c].dtype == object or pd.api.types.is_string_dtype(d[c]):
            before = d[c]
            d[c] = d[c].map(lambda v: " ".join(v.split()) if isinstance(v, str) else v)
            trimmed += int((before.fillna("") != d[c].fillna("")).sum())
            conv = pd.to_numeric(d[c], errors="coerce")
            if d[c].notna().sum() and conv.notna().sum() == d[c].notna().sum():
                d[c] = conv
    n0 = len(d)
    d = d.drop_duplicates()
    exact = n0 - len(d)
    merged: list[dict[str, Any]] = []
    if name and name in d.columns:
        keep, seen = [], {}
        for idx, v in d[name].items():
            nv = _norm_name(v)
            block = nv[:1]
            match = next((o for o in seen.get(block, []) if SequenceMatcher(None, nv, o[1]).ratio() >= threshold), None)
            if match:
                merged.append({"kept": str(d.at[match[0], name]), "merged": str(v),
                               "similarity": _r(SequenceMatcher(None, nv, match[1]).ratio(), 3)})
            else:
                keep.append(idx)
                seen.setdefault(block, []).append((idx, nv))
        d = d.loc[keep]
    imputed = {}
    for c in d.columns:
        if c == key or not pd.api.types.is_numeric_dtype(d[c]):
            continue
        miss = d[c].isna()
        if miss.any() and miss.mean() < 0.5:
            d[f"{c}_imputed"] = miss
            d[c] = d[c].fillna(d[c].median())
            imputed[c] = int(miss.sum())
    report = {"rows_in": n0, "rows_out": int(len(d)), "exact_duplicates": int(exact), "merged_names": merged,
              "imputed": imputed, "trimmed_cells": trimmed}
    return d.reset_index(drop=True), report


# -- regimes ------------------------------------------------------------------------------------------------------


def regimes(df: pd.DataFrame, *, date: str, value: str) -> dict[str, Any]:
    """Regimes in a series: level shifts and volatility shifts (penalized change points on the values and on their
    absolute deviations), each regime's mean and volatility, and a Welch test between the last two regimes."""
    from scipy import stats as sps  # noqa: PLC0415

    from .changepoint import change_points  # noqa: PLC0415

    d = df[[date, value]].dropna().copy()
    d[date] = pd.to_datetime(d[date], errors="coerce")
    d = d.dropna().sort_values(date)
    if len(d) < 30:
        raise MethodError("regime analysis needs at least 30 observations")
    x = d[value].astype(float).to_numpy()
    level = change_points(x, min_segment=10, max_breaks=3)
    dev = np.abs(x - np.median(x))
    vol = change_points(dev, min_segment=10, max_breaks=3)
    cuts = sorted({b.index for b in level} | {b.index for b in vol})
    bounds = [0, *cuts, len(x)]
    rows = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        seg = x[a:b]
        rows.append({"from": str(d[date].iloc[a].date()), "to": str(d[date].iloc[b - 1].date()), "observations": int(len(seg)),
                     "mean": _r(seg.mean(), 6), "volatility": _r(seg.std(ddof=1), 6)})
    t = pd.DataFrame(rows)
    findings = []
    for b in vol:
        when = str(d[date].iloc[b.index].date())
        before, after = x[:b.index].std(ddof=1), x[b.index:].std(ddof=1)
        findings.append(f"Volatility changed on {when}: {before:.4g} before, {after:.4g} after ({after / before:.1f}x).")
    for b in level:
        findings.append(f"Level shifted on {str(d[date].iloc[b.index].date())}: mean {b.before:.4g} to {b.after:.4g}.")
    if len(rows) >= 2:
        a_seg, b_seg = x[bounds[-3]:bounds[-2]], x[bounds[-2]:]
        test = sps.ttest_ind(b_seg, a_seg, equal_var=False)
        lev = sps.levene(a_seg, b_seg)
        findings.append(f"Last two regimes: difference in means p = {test.pvalue:.3g} (Welch); difference in variance "
                        f"p = {lev.pvalue:.3g} (Levene).")
    lead = findings[0] if findings else "No regime change was found: the series is consistent with one regime."
    return {"summary": f"{len(rows)} regime(s) in {len(x)} observations. " + lead, "findings": findings,
            "tables": [_table("Regimes", t)],
            "metrics": {"regimes": len(rows), "level_breaks": len(level), "volatility_breaks": len(vol)},
            "assumptions": ["Breaks are penalized least-squares change points (at least 10 observations per regime).",
                            "Past regimes describe the data; they do not forecast the next change."]}
