# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
# Lyst's own module, maintained here; it grew out of the author's earlier analytics toolkit.
"""Profile a table before designing anything on it.

Covers the six things a data-science EDA pass should encode: schema discovery (each
column's role), missingness, distributions, correlations, quality checks, and the
suggestions a dashboard design starts from (the date column and grain, KPI candidates,
dimensions and their hierarchies).

    p = profile(df)
    p.suggest            # {"date": "Order Date", "grain": "month", "measures": [...], ...}
    print(p.to_markdown())

Lyst profiles every uploaded dataset with it.
"""
from __future__ import annotations

import json
import re
from typing import Any
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

__all__ = ["ColumnProfile", "Profile", "profile", "column_role"]

_ID_NAME = re.compile(r"(^|[\s_\-])(id|key|code|number|no|uuid|guid|sku)$|^row[\s_]?id$", re.I)
_GEO_NAME = re.compile(r"country|state|province|city|region|postal|zip|county|lat|lon|market", re.I)
_MONEY_NAME = re.compile(r"sales|revenue|amount|price|cost|profit|spend|mrr|arr|value|income|fee|budget", re.I)
_PII = {
    "email": re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$"),
    "phone": re.compile(r"^\+?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$"),
    "ssn": re.compile(r"^\d{3}-\d{2}-\d{4}$"),
}


@dataclass
class ColumnProfile:
    name: str
    dtype: str
    role: str                     # id | datetime | measure | dimension | flag | text | constant
    count: int
    nulls: float                  # share of rows
    unique: int
    sample: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    top: list = field(default_factory=list)       # [(value, share)], dimensions and flags
    issues: list = field(default_factory=list)


@dataclass
class Profile:
    rows: int
    columns: list[ColumnProfile]
    duplicate_rows: int
    correlations: list = field(default_factory=list)   # [(a, b, spearman)]
    hierarchies: list = field(default_factory=list)    # [(parent, child)]
    suggest: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)

    def column(self, name: str) -> ColumnProfile:
        return next(c for c in self.columns if c.name == name)

    def by_role(self, role: str) -> list[str]:
        return [c.name for c in self.columns if c.role == role]

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))

    def to_markdown(self) -> str:
        s = self.suggest
        lines = [f"**{self.rows:,} rows × {len(self.columns)} columns**, {self.duplicate_rows:,} duplicate rows.", ""]
        if s.get("date"):
            lines.append(f"- Time: `{s['date']}` from {s.get('start')} to {s.get('end')}; suggested grain **{s.get('grain')}**.")
        if s.get("measures"):
            lines.append("- Measures: " + ", ".join(f"`{m}`" for m in s["measures"]))
        if s.get("dimensions"):
            lines.append("- Dimensions: " + ", ".join(f"`{d}`" for d in s["dimensions"]))
        if s.get("entity"):
            lines.append(f"- Entity id for counts and cohorts: `{s['entity']}`")
        for parent, child in self.hierarchies:
            lines.append(f"- Hierarchy: `{parent}` > `{child}`")
        for a, b, r in self.correlations[:6]:
            lines.append(f"- Correlation: `{a}` ~ `{b}` (Spearman {r:+.2f})")
        lines += ["", "| Column | Role | Type | Nulls | Distinct | Summary | Issues |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for c in self.columns:
            if c.role == "measure":
                st = c.stats
                summ = f"{st['min']:,.4g} – {st['max']:,.4g}, median {st['median']:,.4g}"
            elif c.role == "datetime":
                summ = f"{c.stats.get('min')} – {c.stats.get('max')}"
            else:
                summ = ", ".join(f"{v} ({p:.0%})" for v, p in c.top[:3])
            lines.append(f"| `{c.name}` | {c.role} | {c.dtype} | {c.nulls:.1%} | {c.unique:,} | {summ} | {'; '.join(c.issues)} |")
        if self.issues:
            lines += ["", "**Table issues:** " + "; ".join(self.issues)]
        return "\n".join(lines)


def _as_datetime(s: pd.Series) -> pd.Series | None:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    if not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
        return None
    sample = s.dropna().astype(str).head(200)
    if sample.empty or not sample.str.contains(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}").mean() > 0.9:
        return None
    parsed = pd.to_datetime(s, errors="coerce", format="mixed")
    return parsed if parsed.notna().mean() >= 0.9 * s.notna().mean() else None


_UNPARSED = object()


def column_role(name: str, s: pd.Series, rows: int, parsed: Any = _UNPARSED) -> str:
    """Classify a column. The order matters: constants, flags, dates, ids, then numbers vs text. ``parsed`` is the
    column's ``_as_datetime`` result when the caller already has it, so a date column is parsed once."""
    nn = s.dropna()
    n_unique = nn.nunique()
    if n_unique <= 1:
        return "constant"
    if pd.api.types.is_bool_dtype(s) or n_unique == 2 and set(map(str, nn.unique())) <= {
            "0", "1", "Yes", "No", "yes", "no", "Y", "N", "True", "False", "true", "false"}:
        return "flag"
    if (_as_datetime(s) if parsed is _UNPARSED else parsed) is not None:
        return "datetime"
    uniq_share = n_unique / max(1, len(nn))
    if _ID_NAME.search(name) and (uniq_share > 0.2 or not pd.api.types.is_float_dtype(s)):
        return "id"
    if pd.api.types.is_numeric_dtype(s):
        if pd.api.types.is_integer_dtype(s) and uniq_share > 0.98 and rows > 50 and not _MONEY_NAME.search(name):
            return "id"
        if pd.api.types.is_integer_dtype(s) and n_unique <= 12 and not _MONEY_NAME.search(name):
            return "dimension"      # ratings, levels, small codes
        return "measure"
    lengths = nn.astype(str).str.len()
    if uniq_share > 0.5 and lengths.mean() > 40:
        return "text"
    if uniq_share > 0.9 and rows > 50:
        return "id"
    return "dimension"


def _issues(name: str, s: pd.Series, role: str) -> list[str]:
    out = []
    null_share = s.isna().mean()
    if null_share > 0.3:
        out.append(f"{null_share:.0%} missing")
    if role == "measure":
        x = pd.to_numeric(s, errors="coerce").dropna()
        if len(x) > 20:
            q1, q3 = x.quantile([0.25, 0.75])
            iqr = q3 - q1
            if iqr > 0:
                n_out = int(((x < q1 - 3 * iqr) | (x > q3 + 3 * iqr)).sum())
                if n_out:
                    out.append(f"{n_out:,} extreme values (3×IQR)")
            if _MONEY_NAME.search(name) and not re.search(r"profit|margin|change|delta|net", name, re.I) and (x < 0).any():
                out.append(f"{int((x < 0).sum()):,} negative values")
    elif role in ("dimension", "id"):
        txt = s.dropna().astype(str)
        stripped = txt.str.strip()
        if (stripped != txt).any():
            out.append("leading/trailing spaces")
        if role == "dimension":
            folded = stripped.str.lower().str.replace(r"\s+", " ", regex=True)
            if folded.nunique() < stripped.nunique():
                out.append("case/spacing variants of the same value")
    if role in ("dimension", "id", "text") or pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
        # personal data is reported even when sparse: a few e-mail addresses in a notes column still count
        sample = s.dropna().astype(str).str.strip().head(5000)
        for kind, rx in _PII.items():
            if len(sample):
                hits = sample.str.match(rx)
                if hits.sum() >= 3 or (len(sample) < 30 and hits.any()) or hits.mean() > 0.05:
                    out.append(f"looks like {kind} (PII)")
    return out


def _dedupe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Repeated column labels (common in CSV uploads) become name, name_2, ...; profiling needs one Series per label."""
    if not df.columns.duplicated().any():
        return df
    used: set[str] = set()
    labels = []
    for c in map(str, df.columns):
        label, n = c, 1
        while label in used:
            n += 1
            label = f"{c}_{n}"
        used.add(label)
        labels.append(label)
    out = df.copy()
    out.columns = labels
    return out


def profile(df: pd.DataFrame, *, max_corr: int = 12) -> Profile:
    df = _dedupe_columns(df)
    rows = len(df)
    cols: list[ColumnProfile] = []
    parsed_dates: dict[str, pd.Series] = {}
    for name in df.columns:
        s = df[name]
        dt = _as_datetime(s)  # parsed once, used for the role and the date stats
        role = column_role(str(name), s, rows, dt)
        nn = s.dropna()
        cp = ColumnProfile(name=str(name), dtype=str(s.dtype), role=role, count=int(nn.size),
                           nulls=float(s.isna().mean()) if rows else 0.0, unique=int(nn.nunique()),
                           sample=[str(v) for v in nn.head(3).tolist()])
        if role == "measure":
            x = pd.to_numeric(nn, errors="coerce").dropna()
            cp.stats = {"min": float(x.min()), "max": float(x.max()), "mean": float(x.mean()),
                        "median": float(x.median()), "sum": float(x.sum()),
                        "zeros": float((x == 0).mean()), "skew": float(x.skew()) if len(x) > 2 else 0.0}
        elif role == "datetime":
            d = dt
            parsed_dates[cp.name] = d
            cp.stats = {"min": str(d.min().date()), "max": str(d.max().date()),
                        "days": int((d.max() - d.min()).days)}
        if role in ("dimension", "flag", "id", "text", "constant"):
            vc = nn.astype(str).value_counts(normalize=True).head(8)
            cp.top = [(str(k), float(v)) for k, v in vc.items()]
        cp.issues = _issues(cp.name, s, role)
        cols.append(cp)

    measures = [c.name for c in cols if c.role == "measure"]
    corr = []
    if len(measures) >= 2:
        m = df[measures[:max_corr]].apply(pd.to_numeric, errors="coerce").corr(method="spearman")
        for i, a in enumerate(m.columns):
            for b in m.columns[i + 1:]:
                r = m.loc[a, b]
                if pd.notna(r) and abs(r) >= 0.3:
                    corr.append((a, b, round(float(r), 3)))
        corr.sort(key=lambda t: -abs(t[2]))

    uniques = {c.name: c.unique for c in cols if c.role == "dimension" and 2 <= c.unique <= 60}
    hier = []
    for parent, p_u in uniques.items():
        for child, c_u in uniques.items():
            # the cardinality test uses counts already computed; only surviving pairs pay for a groupby
            if parent == child or c_u <= p_u:
                continue
            if df.groupby(child, observed=True)[parent].nunique().max() == 1:
                hier.append((parent, child))

    dup = int(df.duplicated().sum()) if rows else 0
    issues = []
    if dup:
        issues.append(f"{dup:,} fully duplicated rows")
    for c in cols:
        if c.role == "constant":
            issues.append(f"`{c.name}` is constant")

    return Profile(rows=rows, columns=cols, duplicate_rows=dup, correlations=corr, hierarchies=hier,
                   suggest=_suggest(df, cols, parsed_dates, hier), issues=issues)


def _suggest(df, cols: list[ColumnProfile], dates: dict, hier: list) -> dict:
    s: dict = {}
    date_cols = [c for c in cols if c.role == "datetime"]
    if date_cols:
        # Prefer an "order/created/signup/event" date over ship/close dates, then the fullest.
        def rank(c):
            pref = 0 if re.search(r"order|created|signup|start|event|date$|^date", c.name, re.I) else 1
            return (pref, c.nulls)
        best = sorted(date_cols, key=rank)[0]
        d = dates[best.name]
        days = (d.max() - d.min()).days
        s.update(date=best.name, start=str(d.min().date()), end=str(d.max().date()),
                 grain="day" if days <= 120 else "week" if days <= 400 else "month" if days <= 365 * 6 else "quarter")
    measures = [c for c in cols if c.role == "measure"]
    money_first = sorted(measures, key=lambda c: (0 if _MONEY_NAME.search(c.name) else 1, -abs(c.stats.get("sum", 0))))
    s["measures"] = [c.name for c in money_first[:6]]
    ids = [c for c in cols if c.role == "id"]
    entity = next((c for c in ids if re.search(r"customer|account|user|client|member|employee|patient", c.name, re.I)), None)
    order = next((c for c in ids if re.search(r"order|transaction|invoice|ticket|session|run", c.name, re.I)), None)
    if entity:
        s["entity"] = entity.name
    if order:
        s["event_id"] = order.name
    children = {c for _, c in hier}
    dims = [c for c in cols if c.role == "dimension" and 2 <= c.unique <= 60]
    dims.sort(key=lambda c: (c.name in children, c.unique > 12, c.nulls, c.unique))
    s["dimensions"] = [c.name for c in dims[:8]]
    s["geo"] = [c.name for c in dims if _GEO_NAME.search(c.name)]
    kpis = []
    for m in s["measures"][:3]:
        kpis.append({"label": m, "agg": "sum", "column": m})
    if s.get("event_id"):
        kpis.append({"label": f"{s['event_id']} count", "agg": "nunique", "column": s["event_id"]})
    elif s.get("entity"):
        kpis.append({"label": f"{s['entity']} count", "agg": "nunique", "column": s["entity"]})
    else:
        kpis.append({"label": "Rows", "agg": "count", "column": None})
    s["kpis"] = kpis
    ratio = [(a, b) for a in s["measures"] for b in s["measures"]
             if re.search(r"profit|margin", a, re.I) and re.search(r"sales|revenue", b, re.I)]
    if ratio:
        s["ratios"] = [{"label": "Margin", "numerator": ratio[0][0], "denominator": ratio[0][1]}]
    return s
