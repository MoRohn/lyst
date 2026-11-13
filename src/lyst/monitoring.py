# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Proactive intelligence: monitors on approved metrics that detect, investigate, rank and notify.

A monitor names a metric, a grain and a sensitivity. Creating one opens an approval (explicit
authorization); once approved, the worker ticks it on its interval:

1. **Detect.** The latest complete period against the trailing history: a robust z-score (median and
   MAD) beyond the monitor's threshold is a *metric shift*; an actual outside the ETS forecast's 80%
   interval (fitted without the latest point) is a *forecast deviation*; duplicates or nulls inside the
   period are a *data-quality* finding.
2. **Investigate.** A material shift submits a deep investigation task as the monitor's owner, so the
   finding carries drivers, tested hypotheses and evidence, not just an alert.
3. **Rank.** Priority combines the size of the move (in sigmas, capped), its direction against the
   metric's "higher is better", and its share of the metric's level; findings are deduplicated per
   monitor, period and kind, so the same movement is never raised twice.
4. **Notify.** Each new finding goes to the workspace inbox (owners and admins, plus the monitor's
   creator) and, when the workspace has a webhook, is POSTed there with redacted text.

Monitors run only inside explicit authorization: an approved monitor, on a metric the creator may read,
investigating as that person.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np
import pandas as pd

from .analytics import timeseries
from .security import Guard, Principal, redact
from .semantic import Filter, MetricSpec, compile_metric
from .store import Stores

log = logging.getLogger("lyst.monitoring")
KINDS = ("metric_shift", "forecast_deviation", "data_quality")


class MonitorService:
    def __init__(self, stores: Stores, guard: Guard, data, semantic, runtime, *, http: httpx.Client | None = None):
        self.stores = stores
        self.guard = guard
        self.data = data
        self.semantic = semantic
        self.runtime = runtime
        self.http = http or httpx.Client(timeout=10)
        self.events = None  # the event service (lane.attach): each finding is staged as metric.deviated

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- lifecycle --------------------------------------------------------------------------

    def create(self, actor: Principal, ws: str, *, metric: str, grain: str = "month", threshold_sigma: float = 3.0,
               every_seconds: int = 3600, title: str | None = None, investigate: bool = True,
               segment: dict[str, Any] | None = None) -> dict[str, Any]:
        """``segment`` ({"dimension": "region", "member": "EMEA"}) watches one member of an approved dimension
        instead of the whole metric; the member must exist in the data the creator can read."""
        self.guard.require(actor, ws, "schedule.create", metric)
        spec = self.semantic.get(actor, ws, metric)  # raises KeyError when unknown
        if grain not in ("month", "quarter", "week"):
            raise ValueError("grain must be month, quarter or week")
        if every_seconds < 60:
            raise ValueError("the shortest interval is 60 seconds")
        if segment:
            segment = self._check_segment(actor, ws, spec, segment)
        mid = "mon_" + uuid.uuid4().hex[:10]
        title = title or f"{spec.title}{_in(segment)} by {grain}"
        self._db(ws).execute(
            "INSERT INTO monitors (id, metric, grain, threshold_sigma, every_seconds, title, investigate, enabled, created_by, "
            "next_run_at, segment) VALUES (?, ?, ?, ?, ?, ?, ?, false, ?, now(), ?)",
            [mid, metric, grain, threshold_sigma, every_seconds, title, investigate, actor.user_id,
             json.dumps(segment) if segment else None])
        aid = self.runtime.request_approval(ws, actor.user_id, "monitor.activate",
                                            f"Watch {title}: tell us about "
                                            f"{_SENSITIVITY.get(float(threshold_sigma), f'moves beyond {threshold_sigma:g} sigma')}, "
                                            f"checked {cadence(every_seconds)}", {"monitor_id": mid})
        self._db(ws).execute("UPDATE monitors SET approval_id = ? WHERE id = ?", [aid, mid])
        self.guard.audit(actor, ws, "monitor.create", mid, "pending_approval")
        return {"monitor_id": mid, "approval_id": aid, "enabled": False}

    def _check_segment(self, actor: Principal, ws: str, spec: MetricSpec, segment: dict[str, Any]) -> dict[str, Any]:
        dim, member = segment.get("dimension"), segment.get("member")
        if dim not in spec.dimensions:
            raise ValueError(f"{spec.title} can be watched by {', '.join(spec.dimensions) or 'no dimension'}, not {dim}")
        if member is None or str(member) == "":
            raise ValueError("name the member to watch, for example EMEA")
        r = self.data.query(actor, ws, f'SELECT DISTINCT CAST("{dim}" AS VARCHAR) FROM "{spec.dataset}"')
        members = {row[0] for row in r.rows if row[0] is not None}
        if str(member) not in members:
            raise ValueError(f"{member} is not a {dim.replace('_', ' ')} in {spec.dataset}")
        return {"dimension": dim, "member": str(member)}

    def _spec(self, actor: Principal, ws: str, monitor: dict[str, Any]) -> MetricSpec:
        """The monitored metric, narrowed to its segment when it watches one."""
        spec = self.semantic.get(actor, ws, monitor["metric"])
        seg = _segment(monitor)
        if not seg:
            return spec
        return spec.model_copy(update={
            "title": f"{spec.title} in {seg['member']}",
            "filters": spec.filters + [Filter(column=seg["dimension"], value=seg["member"])],
            "dimensions": [d for d in spec.dimensions if d != seg["dimension"]]})

    def on_approved(self, approver: Principal, ws: str, monitor_id: str) -> None:
        self._db(ws).execute("UPDATE monitors SET enabled = true WHERE id = ?", [monitor_id])
        self.guard.audit(approver, ws, "monitor.activate", monitor_id, "ok")

    def disable(self, actor: Principal, ws: str, monitor_id: str) -> None:
        self.guard.require(actor, ws, "task.control", monitor_id)
        self._db(ws).execute("UPDATE monitors SET enabled = false WHERE id = ?", [monitor_id])
        self.guard.audit(actor, ws, "monitor.disable", monitor_id, "ok")

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query("SELECT * FROM monitors ORDER BY created_at DESC")
        for r in rows:
            r["segment"] = _segment(r)
        return rows

    # -- detection --------------------------------------------------------------------------

    def _history(self, actor: Principal, ws: str, spec: MetricSpec, grain: str) -> pd.Series:
        from .onboarding import fiscal_start  # noqa: PLC0415 - quarterly monitors follow the fiscal calendar

        r = self.data.query(actor, ws, compile_metric(spec, grain=grain, fiscal_start=fiscal_start(self._db(ws))))
        pts = [(pd.Timestamp(p).tz_localize(None) if getattr(p, "tzinfo", None) else pd.Timestamp(p), v)
               for p, v in r.rows if v is not None]
        s = pd.Series([float(v) for _, v in pts], index=pd.DatetimeIndex([p for p, _ in pts]))
        # the final period is complete only if the data runs to its end
        anchor = self.data.query(actor, ws, f'SELECT max("{spec.time_column}") FROM "{spec.dataset}"').rows[0][0]
        anchor = pd.Timestamp(anchor)
        if len(s) and grain == "month" and anchor.day != 1 and anchor < (s.index[-1] + pd.offsets.MonthEnd(0)):
            s = s.iloc[:-1]
        return s

    def pulse(self, actor: Principal, ws: str, *, grain: str = "month", limit: int = 6) -> list[dict[str, Any]]:
        """The approved metrics at a glance: the last complete period, the one before, the change and a short
        history, for a business reader's Home. Runs through the SQL guard as the reader, so column and row policies
        apply; a metric the reader may not query is left out rather than failing the whole pulse."""
        from .analyst.engine import fmt  # noqa: PLC0415

        out: list[dict[str, Any]] = []
        for spec in self.semantic.metrics(actor, ws):
            if len(out) >= limit or not spec.time_column:
                continue
            try:
                s = self._history(actor, ws, spec, grain)
            except Exception as e:  # noqa: BLE001  (denied, or a dataset that cannot be read now)
                log.debug("pulse skipped %s: %s", spec.name, e)
                continue
            if len(s) < 2:
                continue
            cur, prev = float(s.iloc[-1]), float(s.iloc[-2])
            change = cur - prev if spec.format == "percent" else ((cur - prev) / abs(prev) if prev else None)
            flat = change is None or abs(change) < 5e-4  # rounds to 0.0 pts / 0.0%: neither good nor bad
            good = None if flat else ((change > 0) == spec.higher_is_better)
            out.append({"metric": spec.name, "title": spec.title, "format": spec.format, "grain": grain,
                        "period": s.index[-1].strftime("%b %Y"), "value": cur, "prior": prev, "change": change,
                        "value_text": fmt(cur, spec),
                        "change_text": (fmt(change, spec, delta=True) if spec.format == "percent"
                                        else (f"{change * 100:+.1f}%" if change is not None else "n/a")),
                        "good": good, "history": [float(v) for v in s.iloc[-12:]]})
        return out

    def detect(self, actor: Principal, ws: str, monitor: dict[str, Any]) -> list[dict[str, Any]]:
        spec = self._spec(actor, ws, monitor)
        s = self._history(actor, ws, spec, monitor["grain"])
        if len(s) < 6:
            return []
        latest_ts, latest = s.index[-1], float(s.iloc[-1])
        label = latest_ts.strftime("%b %Y") if monitor["grain"] == "month" else f"Q{(latest_ts.month - 1) // 3 + 1} {latest_ts.year}"
        hist = s.iloc[-13:-1] if len(s) > 13 else s.iloc[:-1]
        med, mad = float(np.median(hist)), float(np.median(np.abs(hist - np.median(hist))))
        sigma = mad / 0.6745 if mad > 0 else float(hist.std(ddof=1)) or 1e-12
        z = (latest - med) / sigma
        good = (z > 0) == spec.higher_is_better
        found: list[dict[str, Any]] = []
        base = {"metric": spec.name, "metric_title": spec.title, "format": spec.format, "period": latest_ts.date().isoformat(),
                "label": label, "value": latest, "baseline": med}
        if abs(z) >= float(monitor["threshold_sigma"]):
            found.append({**base, "kind": "metric_shift", "z": z, "direction": "risk" if not good else "opportunity",
                          "priority": self._priority(z, latest, med, good),
                          "summary": f"{spec.title} is {_fmt(latest, spec)} in {label}, {abs(z):.1f} sigma "
                                     f"{'below' if z < 0 else 'above'} its trailing median of {_fmt(med, spec)}."})
        if len(s) >= 9:
            try:
                f = timeseries.forecast(s.iloc[:-1], horizon=1, grain=monitor["grain"] if monitor["grain"] != "week" else "week",
                                        level=0.8, backtest=0)
                lo, hi, yhat = float(f.table["lo"].iloc[0]), float(f.table["hi"].iloc[0]), float(f.table["yhat"].iloc[0])
                if not (lo <= latest <= hi):
                    dev = latest - yhat
                    found.append({**base, "kind": "forecast_deviation", "z": dev / max(abs(hi - lo) / 2.56, 1e-12),
                                  "direction": "risk" if (dev < 0) == spec.higher_is_better else "opportunity",
                                  "priority": self._priority(dev / max(abs(hi - lo) / 2.56, 1e-12), latest, yhat, good),
                                  "summary": f"{spec.title} in {label} ({_fmt(latest, spec)}) fell outside the 80% forecast "
                                             f"interval {_fmt(lo, spec)} to {_fmt(hi, spec)} ({f.method})."})
            except Exception as exc:  # noqa: BLE001 - a model failure is not a finding
                log.warning("forecast check failed for %s: %s", monitor["id"], exc)
        # data quality inside the latest period
        start = latest_ts.date().isoformat()
        end = (latest_ts + (pd.offsets.MonthBegin(1) if monitor["grain"] == "month" else pd.offsets.QuarterBegin(1))).date().isoformat()
        cols = sorted(spec.columns())
        collist = ", ".join(f'"{c}"' for c in cols)
        where = f'"{spec.time_column}" >= \'{start}\' AND "{spec.time_column}" < \'{end}\''
        seg = _segment(monitor)
        if seg:  # a segment monitor checks only its own rows; the member was validated against the data when created
            member = str(seg["member"]).replace("'", "''")
            where += f' AND CAST("{seg["dimension"]}" AS VARCHAR) = \'{member}\''
        r = self.data.query(actor, ws, f'SELECT count(*) AS n, count(*) - (SELECT count(*) FROM (SELECT DISTINCT {collist} FROM '
                                       f'"{spec.dataset}" WHERE {where})) AS dups, '
                                       + ", ".join(f'count(*) - count("{c}") AS "null_{c}"' for c in cols if c != spec.time_column)
                                       + f' FROM "{spec.dataset}" WHERE {where}')
        row = dict(zip(r.columns, r.rows[0], strict=True))
        problems = ([f"{row['dups']} duplicate rows"] if row["dups"] else []) + \
                   [f"{v} nulls in {k[5:]}" for k, v in row.items() if k.startswith("null_") and v]
        if problems:
            found.append({**base, "kind": "data_quality", "z": 0.0, "direction": "risk", "priority": 6.0,
                          "summary": f"Data quality in {label} for {spec.title}: " + ", ".join(problems) + "."})
        return found

    @staticmethod
    def _priority(z: float, value: float, baseline: float, good: bool) -> float:
        size = min(abs(z), 8.0)
        rel = abs(value - baseline) / abs(baseline) if baseline else 0.0
        score = size + min(rel * 10, 4.0)
        return round(score * (1.0 if not good else 0.6), 2)

    # -- ticking ----------------------------------------------------------------------------

    def tick(self, ws: str, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(UTC)
        db = self._db(ws)
        due = db.query("SELECT * FROM monitors WHERE enabled AND next_run_at <= ?", [now])
        new_findings: list[dict[str, Any]] = []
        for m in due:
            db.execute("UPDATE monitors SET next_run_at = ?, last_run_at = ? WHERE id = ?",
                       [now + timedelta(seconds=m["every_seconds"]), now, m["id"]])
            owner = self.stores.control.query("SELECT id, email FROM users WHERE id = ?", [m["created_by"]])
            if not owner:
                continue
            actor = Principal(owner[0]["id"], owner[0]["email"])
            try:
                detected = self.detect(actor, ws, m)
            except Exception as exc:  # noqa: BLE001 - one broken monitor must not stop the others
                log.exception("monitor %s failed", m["id"])
                self.guard.audit(actor, ws, "monitor.tick", m["id"], "error", {"error": str(exc)[:200]})
                continue
            fresh = [f for f in detected
                     if not db.scalar("SELECT count(*) FROM findings WHERE dedupe_key = ?", [f"{m['id']}|{f['period']}|{f['kind']}"])]
            investigation = None
            if m["investigate"] and any(f["kind"] in ("metric_shift", "forecast_deviation") for f in fresh):
                f0 = fresh[0]
                ts = pd.Timestamp(f0["period"])
                # a monthly alert is investigated at quarter level, so a shift that began earlier in the quarter is
                # compared with the quarter before it rather than with its own already-shifted neighbour
                scope = f"Q{(ts.month - 1) // 3 + 1} {ts.year}" if m["grain"] == "month" else f0["label"]
                seg = _segment(m)
                what = (f"{self.semantic.get(actor, ws, m['metric']).title.lower()} in {seg['member']}" if seg
                        else f0["metric_title"].lower())
                q = (f"Investigate why {what} changed in {scope} (alert raised for {f0['label']}) "
                     f"and test alternative explanations.")
                investigation = self.runtime.submit(actor, ws, q, autonomy="autonomous", kind="investigation")
            for f in fresh:
                key = f"{m['id']}|{f['period']}|{f['kind']}"
                fid = "f_" + uuid.uuid4().hex[:10]
                task_id = investigation if f["kind"] in ("metric_shift", "forecast_deviation") else None
                def record(c, f=f, fid=fid, task_id=task_id, key=key, m=m):
                    c.execute(
                        "INSERT INTO findings (id, monitor_id, kind, period, label, metric, summary, priority, direction, z, value, "
                        "baseline, task_id, status, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)",
                        [fid, m["id"], f["kind"], f["period"], f["label"], f["metric"], f["summary"], f["priority"], f["direction"],
                         f["z"], f["value"], f["baseline"], task_id, key])
                    if self.events is not None:  # staged with the finding: rules can act on a validated deviation
                        self.events.stage(c, ws, "metric.deviated", subject=("metric", f["metric"]), source="service",
                                          dedupe_key=f"finding:{key}", related=[("monitor", m["id"]), ("finding", fid)],
                                          data={"kind": f["kind"], "period": str(f["label"]), "direction": f["direction"],
                                                "z": f["z"], "value": f["value"], "baseline": f["baseline"],
                                                "priority": f["priority"], "investigation_task": task_id})

                db.write(record)
                f = {**f, "id": fid, "monitor_id": m["id"], "task_id": task_id}
                new_findings.append(f)
                self._notify(ws, m, f)
            self.guard.audit(actor, ws, "monitor.tick", m["id"], "ok", {"findings": len(detected)})
        return sorted(new_findings, key=lambda f: f["priority"], reverse=True)

    # -- notifications ----------------------------------------------------------------------

    def _recipients(self, ws: str, monitor: dict[str, Any]) -> set[str]:
        rows = self.stores.control.query("SELECT user_id FROM memberships WHERE workspace_id = ? AND role IN ('owner', 'admin')", [ws])
        return {r["user_id"] for r in rows} | {monitor["created_by"]}

    def _notify(self, ws: str, monitor: dict[str, Any], f: dict[str, Any]) -> None:
        db = self._db(ws)
        for uid in self._recipients(ws, monitor):
            db.execute("INSERT INTO notifications (id, user_id, finding_id, channel, status) VALUES (?, ?, ?, 'inbox', 'unread')",
                       ["n_" + uuid.uuid4().hex[:10], uid, f["id"]])
        hook = self.stores.control.scalar("SELECT webhook_url FROM workspaces WHERE id = ?", [ws])
        if hook:
            payload = {"workspace": ws, "finding_id": f["id"], "kind": f["kind"], "metric": f["metric"], "period": f["label"],
                       "priority": f["priority"], "direction": f["direction"], "summary": redact(f["summary"]),
                       "investigation_task": f.get("task_id")}
            try:
                r = self.http.post(hook, json=payload)
                status = "sent" if r.status_code < 300 else f"failed:{r.status_code}"
            except httpx.HTTPError as exc:
                status = f"failed:{type(exc).__name__}"
            db.execute("INSERT INTO notifications (id, user_id, finding_id, channel, status) VALUES (?, NULL, ?, 'webhook', ?)",
                       ["n_" + uuid.uuid4().hex[:10], f["id"], status])

    # -- reading ----------------------------------------------------------------------------

    def findings(self, actor: Principal, ws: str, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        sql = "SELECT * FROM findings"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        return self._db(ws).query(sql + " ORDER BY priority DESC, created_at DESC LIMIT ?", params + [limit])

    def set_status(self, actor: Principal, ws: str, finding_id: str, status: str) -> None:
        if status not in ("new", "acknowledged", "dismissed"):
            raise ValueError("status must be new, acknowledged or dismissed")
        self.guard.require(actor, ws, "task.control", finding_id)
        self._db(ws).execute("UPDATE findings SET status = ? WHERE id = ?", [status, finding_id])
        self._db(ws).execute("UPDATE notifications SET status = 'read' WHERE finding_id = ? AND user_id = ?", [finding_id, actor.user_id])

    def inbox(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        return self._db(ws).query(
            "SELECT n.id AS notification_id, n.status AS read_status, f.* FROM notifications n JOIN findings f ON f.id = n.finding_id "
            "WHERE n.user_id = ? AND n.channel = 'inbox' ORDER BY f.priority DESC, f.created_at DESC", [actor.user_id])


def _segment(monitor: dict[str, Any]) -> dict[str, Any] | None:
    seg = monitor.get("segment")
    return json.loads(seg) if isinstance(seg, str) else (seg or None)


def _in(segment: dict[str, Any] | None) -> str:
    return f" in {segment['member']}" if segment else ""


def _fmt(value: float, spec: MetricSpec) -> str:
    from .analyst.engine import fmt  # noqa: PLC0415

    return fmt(value, spec)


_SENSITIVITY = {4.0: "big moves only", 3.0: "notable moves", 2.0: "any unusual move"}


def cadence(seconds: int) -> str:
    """'every day', not 'every 1440 min': approval summaries are read by business users."""
    return {3600: "every hour", 21600: "every 6 hours", 86400: "every day", 604800: "every week",
            2592000: "every month"}.get(seconds, f"every {seconds // 60} min")
