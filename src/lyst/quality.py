# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data quality, reviewed row by row and resolved with Lyst, never changed silently.

A dataset's current version is scanned for the rows and cells a person should look at:

* **duplicate**: an exact copy of an earlier row (its amounts would count twice);
* **null**: an empty cell;
* **negative**: a negative value in a column that is otherwise positive (cost, revenue, quantity);
* **outlier**: a value more than five robust deviations from the column's median.

Each flagged row or cell is an *issue* with a stable key (its kind, its column and a fingerprint of the row's values),
so a person's review survives a refresh that leaves the row unchanged. A person can accept an issue as valid (an
edge case, nothing changes), or stage a fix (a new value) or an exclusion (the row is left out).

**Lyst proposes fixes** with its reason and how sure it is, from the data itself and never from a model's guess:

* a duplicate copy is dropped;
* a cell is reconciled when the dataset carries an identity (``operating_income = revenue - cogs - opex`` holds in
  every other row): the empty or sign-flipped value is the one that makes the row reconcile;
* an empty measure in a time series is estimated from the periods either side, and said to be an estimate;
* a negative of typical size is read as a sign error, and said to be the likeliest cause rather than a certainty;
* otherwise Lyst says it cannot tell and leaves the issue to a person.

Proposals are only staged, like a person's own fixes. **Applying** staged changes is a resolution: a diff (each change
with its before and after, and what it does to row counts and totals) that waits for an approval
(``quality.resolve``, decided by an admin through the ordinary approvals). Approved, it becomes a new version of the
dataset (source ``quality:<id>``); the earlier version is kept, so the resolution can be undone. If the dataset changed
after the changes were staged, the resolution is not applied and says so.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import re
import threading
import uuid
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from .security import Guard, Principal
from .store import Stores

log = logging.getLogger("lyst.quality")

KINDS: dict[str, tuple[str, str]] = {  # kind -> (label, severity)
    "duplicate": ("Duplicate row", "high"),
    "negative": ("Negative value", "high"),
    "null": ("Empty cell", "medium"),
    "outlier": ("Unusual value", "low"),
}
DECISIONS = ("accept", "exclude", "fix", "reopen")
MAX_ROWS = 1_000_000  # reviewed row by row up to this size; larger tables are resolved with a transformation
MAX_LISTED = 500
_SIGNED = re.compile(r"income|profit|margin|net|change|delta|diff|variance|balance|growth|return", re.I)
_TOL = 0.011  # a cent of rounding per term


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _safe(v: Any) -> Any:
    """A JSON-safe copy of one cell."""
    if v is None:
        return None
    if isinstance(v, float | np.floating):
        return None if math.isnan(float(v)) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return None
        return v.date().isoformat() if v == v.normalize() and v.tzinfo is None else v.isoformat()
    if isinstance(v, datetime | date):
        return v.isoformat()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v if isinstance(v, int | str | bool) else str(v)


def _fmt(v: Any) -> str:
    if v is None:
        return "empty"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e15 else f"{v:.6g}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _decimals(s: pd.Series) -> int:
    vals = s.dropna().head(500).astype(float)
    for d in range(0, 5):
        if np.allclose(vals, vals.round(d), atol=1e-9, rtol=0):
            return d
    return 4


def _formula(target: str, coef: dict[str, int]) -> str:
    parts = []
    for i, (c, k) in enumerate(coef.items()):
        parts.append((("− " if k < 0 else "+ ") if i else ("−" if k < 0 else "")) + c)
    return f"{target} = " + " ".join(parts)


def _arrow(db, physical: str):
    """A version's rows in stored order, as Arrow (the same read for scanning and applying, so fingerprints agree)."""
    with db.read() as cur:
        return cur.sql(f"SELECT * FROM data.{_q(physical)} ORDER BY rowid").to_arrow_table()


def _is_time(s: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(s):
        return True
    first = s.dropna().head(1).tolist()
    return s.dtype == object and bool(first) and isinstance(first[0], date)


class _Scan:
    """One version's issues, computed once and kept while the version is current."""

    def __init__(self, frame: pd.DataFrame, columns: list[str]):
        self.frame = frame
        self.columns = columns
        self.fp = pd.util.hash_pandas_object(frame[columns], index=False).map(lambda h: format(int(h), "016x"))
        self.occ = frame.groupby(self.fp.values).cumcount() + 1
        self.numeric = [c for c in columns if pd.api.types.is_numeric_dtype(frame[c]) and not pd.api.types.is_bool_dtype(frame[c])]
        self._identities: dict[str, tuple[dict[str, int], int, int] | None] = {}
        self.issues: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {k: 0 for k in KINDS}
        self._find()

    # -- finding --------------------------------------------------------------------------------

    def _key(self, kind: str, column: str | None, i: int) -> str:
        return f"{kind}:{column or ''}:{self.fp.iat[i]}:{int(self.occ.iat[i])}"

    def _add(self, kind: str, i: int, column: str | None, why: str, **extra) -> None:
        self.counts[kind] += 1
        key = self._key(kind, column, i)
        label, severity = KINDS[kind]
        self.issues.append({"id": hashlib.sha256(key.encode()).hexdigest()[:12], "key": key, "kind": kind, "label": label,
                            "severity": severity, "column": column, "position": i, "row": i + 1,
                            "value": _safe(self.frame[column].iat[i]) if column else None, "why": why, **extra})

    def _find(self) -> None:
        f, cols = self.frame, self.columns
        dup = f[cols].duplicated(keep="first")
        if dup.any():
            first = pd.Series(np.arange(len(f)), index=f.index).groupby(self.fp.values).transform("min")
            for i in np.flatnonzero(dup.to_numpy()):
                self._add("duplicate", int(i), None, f"An exact copy of row {int(first.iat[i]) + 1:,}; its amounts count twice.",
                          original=int(first.iat[i]) + 1)
        for c in cols:
            for i in np.flatnonzero(f[c].isna().to_numpy()):
                self._add("null", int(i), c, f"{c} is empty in this row.")
        flagged: set[tuple[int, str]] = set()
        for c in self.numeric:
            s = f[c]
            neg = (s < 0).fillna(False).to_numpy()
            n_neg, n = int(neg.sum()), int(s.notna().sum())
            if n_neg and n and n_neg / n <= 0.05 and not _SIGNED.search(c):
                for i in np.flatnonzero(neg):
                    flagged.add((int(i), c))
                    self._add("negative", int(i), c, f"{c} is negative here but positive in {n - n_neg:,} of {n:,} rows.")
        for c in self.numeric:
            s = f[c].astype(float)
            if s.nunique(dropna=True) <= 10:
                continue
            med = s.median()
            mad = (s - med).abs().median()
            if not mad or math.isnan(mad):
                continue
            z = ((s - med).abs() / (1.4826 * mad)).fillna(0).to_numpy()
            for i in np.flatnonzero(z > 5):
                if (int(i), c) not in flagged:
                    self._add("outlier", int(i), c, f"{c} is {z[i]:.0f} robust deviations from its median ({_fmt(_safe(med))}).")

    # -- reasoning ------------------------------------------------------------------------------

    def identity(self, target: str) -> tuple[dict[str, int], int, int] | None:
        """``target`` as a sum of other numeric columns (coefficients ±1) that holds in at least 98% of complete rows:
        (coefficients, rows that hold, rows checked). Cached per column."""
        if target in self._identities:
            return self._identities[target]
        found = None
        others = [c for c in self.numeric if c != target][:6]
        full = self.frame[[target, *others]].dropna().astype(float)
        if others and len(full) >= 12 and full[target].std() > 0:
            sample = full.sample(min(len(full), 5000), random_state=0) if len(full) > 5000 else full
            t = sample[target].to_numpy()
            mat = sample[others].to_numpy()
            best = None
            for k in range(1, len(others) + 1):
                for idx in itertools.combinations(range(len(others)), k):
                    for signs in itertools.product((1, -1), repeat=k):
                        approx = mat[:, idx] @ np.array(signs, dtype=float)
                        ok = np.abs(t - approx) <= _TOL * (k + 1) + 1e-9 * np.abs(t)
                        if ok.mean() >= 0.98:
                            best = {others[j]: s for j, s in zip(idx, signs, strict=True)}
                            break
                    if best:
                        break
                if best:
                    break
            if best:
                approx = full[list(best)].to_numpy() @ np.array(list(best.values()), dtype=float)
                hold = int((np.abs(full[target].to_numpy() - approx) <= _TOL * (len(best) + 1) + 1e-9 * np.abs(full[target].to_numpy())).sum())
                found = (best, hold, len(full))
        self._identities[target] = found
        return found

    def _implied(self, i: int, target: str) -> tuple[float, str, int, int] | None:
        ident = self.identity(target)
        if not ident:
            return None
        coef, hold, n = ident
        row = self.frame.iloc[i]
        if any(pd.isna(row[c]) for c in coef):
            return None
        value = round(sum(float(row[c]) * k for c, k in coef.items()), _decimals(self.frame[target]))
        return value, _formula(target, coef), hold, n

    def _series(self, i: int, target: str) -> tuple[float, str] | None:
        """The average of the periods either side, in the same series (the same values in every text column)."""
        f = self.frame
        times = [c for c in self.columns if _is_time(f[c])]
        if not times:
            return None
        t = times[0]
        keys = [c for c in self.columns if c not in self.numeric and c != t and f[c].nunique(dropna=True) <= 500]
        row = f.iloc[i]
        same = f
        for k in keys:
            same = same[same[k] == row[k]] if not pd.isna(row[k]) else same[same[k].isna()]
        same = same[same[target].notna()]
        at = row[t]
        if pd.isna(at):
            return None
        before = same[same[t] < at].sort_values(t).tail(1)
        after = same[same[t] > at].sort_values(t).head(1)
        if before.empty or after.empty:
            return None
        a, b = float(before[target].iat[0]), float(after[target].iat[0])
        value = round((a + b) / 2, _decimals(f[target]))
        where = " and ".join(f"{k.replace('_', ' ')}" for k in keys) or "series"
        return value, (f"Estimated as the average of the periods before and after for the same {where} "
                       f"({_fmt(_safe(before[t].iat[0]))}: {_fmt(a)}; {_fmt(_safe(after[t].iat[0]))}: {_fmt(b)}). It is an estimate.")

    def proposal(self, issue: dict[str, Any]) -> dict[str, Any]:
        """What Lyst would do about one issue, why, and how sure it is. ``action`` is drop, set, accept or none."""
        kind, c, i, v = issue["kind"], issue["column"], issue["position"], issue["value"]
        if kind == "duplicate":
            return {"action": "drop", "confidence": "high",
                    "reason": f"Remove this copy; row {issue['original']:,} keeps the same values, so nothing is lost."}
        numeric = c in self.numeric
        implied = self._implied(i, c) if numeric else None
        if kind == "null":
            if implied:
                value, formula, hold, n = implied
                return {"action": "set", "value": value, "confidence": "high",
                        "reason": f"{formula} holds in {hold:,} of {n:,} complete rows, so this row's {c} is {_fmt(value)}."}
            if numeric and (est := self._series(i, c)):
                return {"action": "set", "value": est[0], "confidence": "medium", "reason": est[1]}
            return {"action": "none", "confidence": "low",
                    "reason": f"Lyst cannot tell what {c} should be here. Fill it in, or exclude the row."}
        if kind == "negative":
            if implied:
                value, formula, hold, n = implied
                if math.isclose(value, -v, rel_tol=1e-6, abs_tol=_TOL * 4):
                    return {"action": "set", "value": abs(v), "confidence": "high",
                            "reason": f"With {c} positive the row reconciles ({formula}, true in {hold:,} of {n:,} rows); "
                                      "the sign was flipped."}
                if math.isclose(value, v, rel_tol=1e-6, abs_tol=_TOL * 4):
                    return {"action": "accept", "confidence": "high",
                            "reason": f"The row reconciles as it is ({formula}), so this reads as a genuine credit."}
                return {"action": "set", "value": value, "confidence": "medium",
                        "reason": f"{formula} holds in {hold:,} of {n:,} rows; this row's {c} would be {_fmt(value)}."}
            pos = self.frame[c][self.frame[c] > 0].astype(float)
            if len(pos) >= 10 and pos.quantile(0.05) <= abs(v) <= pos.quantile(0.95):
                return {"action": "set", "value": abs(v), "confidence": "medium",
                        "reason": f"Its size is typical for {c}, so a flipped sign is the likeliest cause. "
                                  "If it is a credit or a reversal, accept it as valid instead."}
            return {"action": "none", "confidence": "low",
                    "reason": "It could be a credit or a reversal. Check it with the owner, then accept or fix it."}
        if kind == "outlier":
            if implied:
                value, formula, hold, n = implied
                if math.isclose(value, v, rel_tol=1e-6, abs_tol=_TOL * 4):
                    return {"action": "accept", "confidence": "high",
                            "reason": f"It reconciles with the rest of the row ({formula}), so it is real, just large."}
                return {"action": "set", "value": value, "confidence": "medium",
                        "reason": f"{formula} holds in {hold:,} of {n:,} rows; this row's {c} would be {_fmt(value)}."}
            return {"action": "none", "confidence": "low", "reason": "Large values can be real. Check it with the owner."}
        return {"action": "none", "confidence": "low", "reason": ""}


class QualityService:
    def __init__(self, stores: Stores, guard: Guard, data, runtime=None) -> None:
        self.stores = stores
        self.guard = guard
        self.data = data
        self.runtime = runtime
        self._cache: dict[tuple[str, str, int], _Scan] = {}
        self._lock = threading.Lock()

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- reading --------------------------------------------------------------------------------

    def _current(self, ws: str, name: str) -> dict[str, Any]:
        rows = self._db(ws).query(
            "SELECT d.name, d.title, d.current_version AS version, v.physical_table, v.row_count, v.schema FROM datasets d "
            "JOIN dataset_versions v ON v.dataset = d.name AND v.version = d.current_version WHERE d.name = ?", [name])
        if not rows:
            raise KeyError(name)
        r = rows[0]
        r["schema"] = json.loads(r["schema"]) if isinstance(r["schema"], str) else r["schema"]
        return r

    def _visible(self, actor: Principal, ws: str, name: str) -> None:
        """Reviewing quality row by row shows whole rows, so it needs a person who sees every row and column."""
        self.guard.require(actor, ws, "data.read", name)
        policy = self.data.policy_for(actor, ws)
        if policy.restricted_columns.get(name) or name in policy.row_filters:
            raise ValueError(f"you see only part of {name}; someone who sees all of it must review its quality")
        for check in getattr(self.data, "access_checks", []):
            check(actor, ws, [name])

    def _read(self, ws: str, physical: str) -> pd.DataFrame:
        return _arrow(self._db(ws), physical).to_pandas()

    def _scan(self, ws: str, cur: dict[str, Any]) -> _Scan:
        key = (ws, cur["name"], int(cur["version"]))
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        if int(cur["row_count"] or 0) > MAX_ROWS:
            raise ValueError(f"{cur['name']} has {int(cur['row_count']):,} rows, more than Lyst reviews row by row "
                             f"({MAX_ROWS:,}); clean it with a transformation instead")
        frame = self._read(ws, cur["physical_table"])
        scan = _Scan(frame, [c["name"] for c in cur["schema"]])
        with self._lock:
            for k in [k for k in self._cache if k[:2] == key[:2]]:
                self._cache.pop(k, None)
            if len(self._cache) >= 8:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = scan
        return scan

    def _reviews(self, ws: str, name: str) -> dict[str, dict[str, Any]]:
        out = {}
        for r in self._db(ws).query("SELECT * FROM quality_reviews WHERE dataset = ?", [name]):
            r["value"] = json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
            out[r["issue_key"]] = r
        return out

    def _resolutions(self, ws: str, name: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._db(ws).query("SELECT * FROM quality_resolutions WHERE dataset = ? ORDER BY requested_at DESC LIMIT ?",
                                  [name, limit])
        for r in rows:
            r["changes"] = json.loads(r["changes"]) if isinstance(r["changes"], str) else r["changes"]
            for k in ("requested_at", "decided_at"):
                r[k] = str(r[k]) if r[k] is not None else None
        return rows

    def report(self, actor: Principal, ws: str, name: str) -> dict[str, Any]:
        """Every flagged row and cell of the current version, with its review, Lyst's proposal, the staged changes as a
        diff with their effect, and the resolutions waiting for approval or recently applied."""
        self._visible(actor, ws, name)
        cur = self._current(ws, name)
        scan = self._scan(ws, cur)
        reviews = self._reviews(ws, name)
        resolutions = self._resolutions(ws, name)
        pending = {r["id"]: r for r in resolutions if r["status"] == "pending"}
        issues, staged = [], []
        for base in scan.issues:
            rev = reviews.get(base["key"])
            status = "open"
            if rev and rev["decision"] == "accept":
                status = "valid"
            elif rev and rev["decision"] in ("exclude", "fix"):
                status = "pending" if rev["resolution_id"] in pending else "staged"
            issue = {k: v for k, v in base.items() if k != "position"}
            issue["values"] = {c: _safe(scan.frame[c].iat[base["position"]]) for c in scan.columns}
            issue["status"] = status
            issue["review"] = ({"decision": rev["decision"], "value": rev["value"], "origin": rev["origin"], "note": rev["note"],
                                "reason": rev["reason"], "by": rev["decided_by"], "at": str(rev["decided_at"])} if rev else None)
            issue["proposal"] = scan.proposal(base)
            issues.append(issue)
            if status in ("staged", "pending"):
                staged.append(self._change(issue, rev, scan))
        order = {"open": 0, "staged": 1, "pending": 2, "valid": 3}
        sev = {"high": 0, "medium": 1, "low": 2}
        issues.sort(key=lambda x: (order[x["status"]], sev[x["severity"]], x["row"]))
        types = {c["name"]: ("n" if c["name"] in scan.numeric else "d" if any(t in c["type"] for t in ("date", "time")) else "t")
                 for c in cur["schema"]}
        mine = [s for s in staged if s["status"] == "staged"]
        return {"dataset": name, "title": cur["title"], "version": cur["version"], "rows": len(scan.frame),
                "columns": [{"name": c, "type": types.get(c, "t")} for c in scan.columns],
                "counts": {k: v for k, v in scan.counts.items() if v}, "total": len(issues),
                "open": sum(i["status"] == "open" for i in issues), "valid": sum(i["status"] == "valid" for i in issues),
                "fixable": sum(i["status"] == "open" and i["proposal"]["action"] in ("drop", "set", "accept") for i in issues),
                "issues": issues[:MAX_LISTED], "truncated": len(issues) > MAX_LISTED,
                "staged": mine, "impact": self._impact(scan, mine) if mine else None,
                "pending": [self._public(r) for r in pending.values()],
                "recent": [self._public(r) for r in resolutions if r["status"] in ("applied", "undone", "stale", "rejected", "failed")][:5]}

    @staticmethod
    def _public(r: dict[str, Any]) -> dict[str, Any]:
        return {k: r[k] for k in ("id", "status", "summary", "approval_id", "base_version", "result_version", "undone_version",
                                  "error", "requested_by", "requested_at", "decided_by", "decided_at", "changes")}

    @staticmethod
    def _change(issue: dict[str, Any], rev: dict[str, Any], scan: _Scan) -> dict[str, Any]:
        action = "drop" if rev["decision"] == "exclude" else "set"
        return {"issue_id": issue["id"], "issue_key": issue["key"], "kind": issue["kind"], "label": issue["label"],
                "row": issue["row"], "column": issue["column"], "action": action,
                "before": issue["value"] if action == "set" else issue["values"],
                "after": rev["value"] if action == "set" else None, "origin": rev["origin"], "reason": rev["reason"] or "",
                "status": "pending" if issue["status"] == "pending" else "staged", "resolution_id": rev["resolution_id"]}

    @staticmethod
    def _impact(scan: _Scan, changes: list[dict[str, Any]]) -> dict[str, Any]:
        """Row count and the totals of the measures the changes touch, before and after."""
        f = scan.frame
        keys = scan.fp.to_numpy()
        occ = scan.occ.to_numpy()
        pos = {f"{k}:{o}": i for i, (k, o) in enumerate(zip(keys, occ, strict=True))}
        after = f.copy()
        drop: list[int] = []
        touched: list[str] = []
        for ch in changes:
            i = pos.get(":".join(ch["issue_key"].split(":")[2:]))
            if i is None:
                continue
            if ch["action"] == "drop":
                drop.append(i)
                touched += [c for c in scan.numeric if c not in touched]
            elif ch["column"]:
                after.iat[i, after.columns.get_loc(ch["column"])] = ch["after"]
                if ch["column"] in scan.numeric and ch["column"] not in touched:
                    touched.insert(0, ch["column"])
        after = after.drop(index=f.index[drop]) if drop else after
        totals = []
        for c in touched[:4]:
            b, a = float(f[c].astype(float).sum()), float(pd.to_numeric(after[c], errors="coerce").sum())
            if not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9):
                totals.append({"column": c, "before": round(b, 2), "after": round(a, 2), "change": round(a - b, 2)})
        return {"rows_before": len(f), "rows_after": len(after), "totals": totals}

    # -- reviewing ------------------------------------------------------------------------------

    def review(self, actor: Principal, ws: str, name: str, issue_ids: list[str], decision: str, *, value: Any = None,
               note: str | None = None) -> dict[str, Any]:
        """A person's call on one or more issues: accept as valid, stage a fix (``value``) or an exclusion, or reopen."""
        self.guard.require(actor, ws, "data.ingest", name)
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        self._visible(actor, ws, name)
        cur = self._current(ws, name)
        scan = self._scan(ws, cur)
        byid = {i["id"]: i for i in scan.issues}
        missing = [i for i in issue_ids if i not in byid]
        if missing or not issue_ids:
            raise KeyError(f"no such issue in the current version of {name}: {', '.join(missing) or 'none given'}")
        reviews = self._reviews(ws, name)
        if any((reviews.get(byid[i]["key"]) or {}).get("resolution_id") and reviews[byid[i]["key"]]["decision"] in ("exclude", "fix")
               and self._pending(ws, reviews[byid[i]["key"]]["resolution_id"]) for i in issue_ids):
            raise ValueError("a change to this row is waiting for approval; decide that first")
        rows = []
        for iid in issue_ids:
            issue = byid[iid]
            if decision == "fix":
                if not issue["column"]:
                    raise ValueError("a duplicate row has no single cell to fix; exclude it instead")
                rows.append((issue, self._coerce(scan, issue["column"], value)))
            else:
                rows.append((issue, None))
        self._record(ws, name, [(i, decision, v, "person", None) for i, v in rows], actor, note)
        self.guard.audit(actor, ws, "quality.review", name, "ok", {"decision": decision, "issues": len(issue_ids)})
        return self.report(actor, ws, name)

    def _pending(self, ws: str, rid: str) -> bool:
        return bool(self._db(ws).scalar("SELECT count(*) FROM quality_resolutions WHERE id = ? AND status = 'pending'", [rid]))

    def _record(self, ws: str, name: str, items: list[tuple[dict, str, Any, str, str | None]], actor: Principal,
                note: str | None) -> None:
        def tx(c):
            for issue, decision, value, origin, reason in items:
                c.execute("DELETE FROM quality_reviews WHERE dataset = ? AND issue_key = ?", [name, issue["key"]])
                if decision != "reopen":
                    c.execute("INSERT INTO quality_reviews (dataset, issue_key, kind, column_name, decision, value, origin, reason, "
                              "note, decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              [name, issue["key"], issue["kind"], issue["column"], decision, json.dumps(value, default=str),
                               origin, reason, (note or "")[:1000] or None, actor.user_id])

        self._db(ws).write(tx)

    @staticmethod
    def _coerce(scan: _Scan, column: str, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("give the value to put in the cell")
        if column in scan.numeric:
            try:
                x = float(str(value).replace(",", "").strip())
            except ValueError:
                raise ValueError(f"{column} holds numbers; {value!r} is not one") from None
            if math.isnan(x) or math.isinf(x):
                raise ValueError("give a finite number")
            return int(x) if pd.api.types.is_integer_dtype(scan.frame[column]) or (
                scan.frame[column].dropna().apply(float.is_integer).all() and x.is_integer() and _decimals(scan.frame[column]) == 0) else x
        if _is_time(scan.frame[column]):
            try:
                return pd.Timestamp(str(value)).date().isoformat()
            except ValueError:
                raise ValueError(f"{column} holds dates; {value!r} is not one") from None
        return str(value)[:2000]

    def propose(self, actor: Principal, ws: str, name: str, issue_ids: list[str] | None = None) -> dict[str, Any]:
        """Let Lyst resolve: stage Lyst's proposal for each open issue it can resolve (or the ones given). Proposals that
        accept an issue as valid are recorded as such; fixes and exclusions are only staged, never applied."""
        self.guard.require(actor, ws, "data.ingest", name)
        report = self.report(actor, ws, name)
        chosen = [i for i in report["issues"] if i["status"] == "open" and (issue_ids is None or i["id"] in issue_ids)
                  and i["proposal"]["action"] in ("drop", "set", "accept")]
        items = []
        for i in chosen:
            p = i["proposal"]
            decision = {"drop": "exclude", "set": "fix", "accept": "accept"}[p["action"]]
            items.append(({"key": i["key"], "kind": i["kind"], "column": i["column"]}, decision, p.get("value"), "lyst",
                          f"{p['reason']} ({p['confidence']} confidence)"))
        if items:
            self._record(ws, name, items, actor, None)
        self.guard.audit(actor, ws, "quality.propose", name, "ok", {"staged": len(items)})
        out = self.report(actor, ws, name)
        out["proposed"] = len(items)
        return out

    # -- resolving ------------------------------------------------------------------------------

    def apply(self, actor: Principal, ws: str, name: str, *, note: str | None = None) -> dict[str, Any]:
        """Turn the staged changes into a resolution that waits for an approval. Nothing changes until it is approved."""
        self.guard.require(actor, ws, "data.ingest", name)
        report = self.report(actor, ws, name)
        changes = report["staged"]
        if not changes:
            raise ValueError("nothing is staged: fix, exclude or let Lyst propose first")
        drops = sum(c["action"] == "drop" for c in changes)
        sets = len(changes) - drops
        what = ", ".join(x for x in (f"fix {sets} cell{'s' * (sets != 1)}" if sets else "",
                                     f"remove {drops} row{'s' * (drops != 1)}" if drops else "") if x)
        summary = f"Data quality: {what} in {report['title']} (version {report['version']})"
        rid = "qr_" + uuid.uuid4().hex[:10]
        stored = [{k: c[k] for k in ("issue_id", "issue_key", "kind", "label", "row", "column", "action", "before", "after", "origin",
                                     "reason")} for c in changes]
        db = self._db(ws)
        db.execute("INSERT INTO quality_resolutions (id, dataset, base_version, changes, summary, status, note, requested_by) "
                   "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                   [rid, name, report["version"], json.dumps(stored, default=str), summary, (note or "")[:1000] or None, actor.user_id])
        keys = [c["issue_key"] for c in changes]
        db.execute(f"UPDATE quality_reviews SET resolution_id = ? WHERE dataset = ? AND issue_key IN ({', '.join('?' * len(keys))})",
                   [rid, name, *keys])
        aid = None
        if self.runtime is not None:
            aid = self.runtime.request_approval(ws, actor.user_id, "quality.resolve", summary,
                                                {"dataset": name, "resolution_id": rid, "changes": len(changes),
                                                 "impact": report["impact"]})
            db.execute("UPDATE quality_resolutions SET approval_id = ? WHERE id = ?", [aid, rid])
        self.guard.audit(actor, ws, "quality.request", name, "pending_approval", {"resolution": rid, "changes": len(changes)})
        return {"id": rid, "status": "pending", "approval_id": aid, "summary": summary, "changes": stored,
                "impact": report["impact"]}

    def on_decided(self, approver: Principal, ws: str, payload: dict[str, Any], approve: bool) -> None:
        """An admin decided a ``quality.resolve`` approval: approved, the changes become a new version of the dataset."""
        rid, name = payload["resolution_id"], payload["dataset"]
        db = self._db(ws)
        rows = db.query("SELECT * FROM quality_resolutions WHERE id = ?", [rid])
        if not rows or rows[0]["status"] != "pending":
            return
        res = rows[0]
        if not approve:
            db.execute("UPDATE quality_resolutions SET status = 'rejected', decided_by = ?, decided_at = now() WHERE id = ?",
                       [approver.user_id, rid])
            db.execute("UPDATE quality_reviews SET resolution_id = NULL WHERE resolution_id = ?", [rid])
            self.guard.audit(approver, ws, "quality.resolve", name, "rejected", {"resolution": rid})
            return
        try:
            version = self._apply(approver, ws, name, res)
        except _Stale as exc:
            db.execute("UPDATE quality_resolutions SET status = 'stale', error = ?, decided_by = ?, decided_at = now() WHERE id = ?",
                       [str(exc), approver.user_id, rid])
            db.execute("UPDATE quality_reviews SET resolution_id = NULL WHERE resolution_id = ?", [rid])
            self.guard.audit(approver, ws, "quality.resolve", name, "stale", {"resolution": rid, "reason": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - recorded on the resolution; the dataset keeps its current version
            log.exception("applying %s failed", rid)
            db.execute("UPDATE quality_resolutions SET status = 'failed', error = ?, decided_by = ?, decided_at = now() WHERE id = ?",
                       [str(exc).splitlines()[0][:400], approver.user_id, rid])
            db.execute("UPDATE quality_reviews SET resolution_id = NULL WHERE resolution_id = ?", [rid])
            self.guard.audit(approver, ws, "quality.resolve", name, "failed", {"resolution": rid})
            return
        db.execute("UPDATE quality_resolutions SET status = 'applied', result_version = ?, decided_by = ?, decided_at = now() "
                   "WHERE id = ?", [version, approver.user_id, rid])
        db.execute("DELETE FROM quality_reviews WHERE resolution_id = ?", [rid])
        self.guard.audit(approver, ws, "quality.resolve", name, "ok", {"resolution": rid, "version": version})

    def _apply(self, approver: Principal, ws: str, name: str, res: dict[str, Any]) -> int:
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.compute as pc  # noqa: PLC0415

        cur = self._current(ws, name)
        if int(cur["version"]) != int(res["base_version"]):
            raise _Stale(f"{name} changed after these changes were staged (version {res['base_version']} is now "
                         f"{cur['version']}); review it again")
        changes = json.loads(res["changes"]) if isinstance(res["changes"], str) else res["changes"]
        table = _arrow(self._db(ws), cur["physical_table"])
        scan = _Scan(table.to_pandas(), [f.name for f in table.schema])
        pos = {f"{k}:{o}": i for i, (k, o) in enumerate(zip(scan.fp.to_numpy(), scan.occ.to_numpy(), strict=True))}
        keep = np.ones(table.num_rows, dtype=bool)
        cells: dict[str, dict[int, Any]] = {}
        for ch in changes:
            i = pos.get(":".join(ch["issue_key"].split(":")[2:]))
            if i is None:
                raise _Stale(f"row {ch['row']} is no longer in {name} as it was; review it again")
            if ch["action"] == "drop":
                keep[i] = False
            else:
                cells.setdefault(ch["column"], {})[i] = ch["after"]
        for col, edits in cells.items():
            j = table.schema.get_field_index(col)
            typ = table.schema.field(j).type
            values = table.column(j).to_pylist()
            for i, v in edits.items():
                if pa.types.is_integer(typ):
                    v = int(round(float(v)))
                elif pa.types.is_floating(typ):
                    v = float(v)
                elif pa.types.is_decimal(typ):
                    from decimal import Decimal  # noqa: PLC0415

                    v = Decimal(str(v))
                elif pa.types.is_date(typ):
                    v = date.fromisoformat(str(v)[:10])
                elif pa.types.is_timestamp(typ):
                    v = pd.Timestamp(str(v)).to_pydatetime()
                values[i] = v
            table = table.set_column(j, table.schema.field(j), pa.array(values, type=typ))
        if not keep.all():
            table = table.filter(pc.cast(pa.array(keep), pa.bool_()))
        result = self.data.ingest_arrow(approver, ws, name, table, source=f"quality:{res['id']}")
        return int(result["version"])

    def undo(self, actor: Principal, ws: str, rid: str) -> dict[str, Any]:
        """Put back the version a resolution started from, as a new version (nothing is deleted)."""
        db = self._db(ws)
        rows = db.query("SELECT * FROM quality_resolutions WHERE id = ?", [rid])
        if not rows:
            raise KeyError(rid)
        res = rows[0]
        self.guard.require(actor, ws, "data.ingest", res["dataset"])
        if res["status"] != "applied":
            raise ValueError("only an applied resolution can be undone")
        cur = self._current(ws, res["dataset"])
        if int(cur["version"]) != int(res["result_version"]):
            raise ValueError(f"{res['dataset']} has changed since this was applied; undoing it would lose the newer version")
        physical = db.scalar("SELECT physical_table FROM dataset_versions WHERE dataset = ? AND version = ?",
                             [res["dataset"], res["base_version"]])
        table = _arrow(db, physical)
        result = self.data.ingest_arrow(actor, ws, res["dataset"], table, source=f"quality-undo:{rid}")
        db.execute("UPDATE quality_resolutions SET status = 'undone', undone_version = ? WHERE id = ?", [result["version"], rid])
        self.guard.audit(actor, ws, "quality.undo", res["dataset"], "ok", {"resolution": rid, "version": result["version"]})
        return {"id": rid, "status": "undone", "version": result["version"]}


class _Stale(Exception):
    """The data a resolution was staged against is no longer the current version."""
