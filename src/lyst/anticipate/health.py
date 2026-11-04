# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data and integration health foresight: what is about to break analytical work, found from operational metadata, with
a safe way to prevent it.

Signals (all from metadata Lyst already records; no source is contacted):

* **Credential expiry** behind a connection: an ``expires_at`` recorded with the credential (Settings > Secrets), or its
  age past ``LYST_CREDENTIAL_MAX_AGE_DAYS`` (default 180) since it was connected or last replaced. Only the credential's
  name and id are ever shown; renewal is a link to Settings, where the person pastes the new secret.
* **Connection reliability** over the last 14 days of connection events: rising latency (median of the last five
  successful calls at least twice the earlier median and above a second), rate-limit pressure (a third or more of recent
  attempts retried for rate limits), intermittent failures (two or more of the last ten failed although the latest
  succeeded), and authentication failures.
* **Schema change impact**: a dataset version that removed or retyped columns approved metrics use. The metrics are
  predicted to break; every registered asset and enabled schedule built on them is listed. When a removed column has a
  likely replacement (same type family, similar name), a candidate definition is drafted and validated by running it on
  the new version; accepting files it as a review, so an admin approves the definition change. Nothing is modified
  silently.

Each item names the scheduled runs and assets it puts at risk. Expiry before the next dependent run (or within three
days), an expired credential, an authentication failure, trouble on a connection that feeds a run in the next 48 hours, or
a predicted break with dependents is pushed once (level 2 and above); the rest wait in the activity center. An item closes itself (``resolved``) when its signal is gone, for example after the credential is
renewed.
"""

from __future__ import annotations

import difflib
import json
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from ..security import Principal

KINDS = ("health_foresight",)
RESOLVES = True
SURFACE = ("risk",)
WINDOW = timedelta(days=14)


def _aware(ts):
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=UTC)


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        ts = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _days(td: timedelta) -> str:
    d = td.total_seconds() / 86400
    return "today" if d < 1 else f"in {d:.0f} day{'s' if round(d) != 1 else ''}"


# -- dependents ------------------------------------------------------------------------------------------------------------

def _datasets_of(svc, ws: str, connection_id: str) -> list[str]:
    return [r["dataset"] for r in svc.stores.control.query(
        "SELECT dataset FROM connection_syncs WHERE workspace_id = ? AND connection_id = ?", [ws, connection_id])]


def _dependents(svc, ws: str, datasets: set[str], metrics: set[str], now: datetime) -> dict[str, Any]:
    """Enabled schedules and registered assets that read these datasets or metrics, with the earliest next run."""
    from . import recurring  # noqa: PLC0415

    db = svc.stores.workspace(ws)
    runs = []
    for s in svc.stores.control.query("SELECT * FROM schedules WHERE workspace_id = ? AND enabled", [ws]):
        owner = recurring._owner(svc, s["created_by"])  # noqa: SLF001
        if owner is None:
            continue
        deps = recurring.dependencies(svc, ws, owner, s, recurring.history(svc.stores.control, s["id"]))
        if set(deps["datasets"]) & datasets or set(deps["metrics"]) & metrics:
            runs.append({"schedule_id": s["id"], "title": s["title"], "next_run_at": _aware(s["next_run_at"])})
    assets = []
    for a in db.query("SELECT id, kind, title, depends FROM assets WHERE status = 'active'"):
        dep = _j(a["depends"])
        if set(dep.get("datasets") or {}) & datasets or set(dep.get("metrics") or {}) & metrics:
            assets.append({"asset_id": a["id"], "kind": a["kind"], "title": a["title"]})
    runs.sort(key=lambda r: r["next_run_at"] or now + timedelta(days=3650))
    return {"schedules": runs, "assets": assets, "next_run_at": runs[0]["next_run_at"] if runs else None}


def _at_risk(dep: dict[str, Any]) -> str:
    parts = []
    if dep["schedules"]:
        parts.append("scheduled run" + ("s " if len(dep["schedules"]) > 1 else " ") + ", ".join(f"'{s['title']}'" for s in dep["schedules"][:3]))
    if dep["assets"]:
        parts.append(f"{len(dep['assets'])} report(s) or dashboard(s)")
    return " and ".join(parts) if parts else "nothing scheduled yet"


# -- observe ---------------------------------------------------------------------------------------------------------------

def observe(svc, ws: str, now: datetime) -> list[dict[str, Any]]:
    ctl = svc.stores.control
    out: list[dict[str, Any]] = []
    max_age = timedelta(days=int(os.environ.get("LYST_CREDENTIAL_MAX_AGE_DAYS", "180")))
    for c in ctl.query("SELECT id, name, credential_id, created_by FROM connections WHERE workspace_id = ? AND credential_id IS NOT NULL", [ws]):
        cr = ctl.query("SELECT id, name, provider, fields, status, created_at, replaced_at FROM credentials WHERE id = ?", [c["credential_id"]])
        if not cr or cr[0]["status"] != "active":
            continue
        cr = cr[0]
        expires = _parse_ts(_j(cr["fields"]).get("expires_at"))
        since = _aware(cr["replaced_at"] or cr["created_at"])
        if expires and expires - now <= WINDOW:
            out.append({"kind": "credential_expiry", "connection": c, "credential": cr, "expires_at": expires})
        elif not expires and since and now - since >= max_age - WINDOW:
            out.append({"kind": "credential_age", "connection": c, "credential": cr, "rotate_by": since + max_age})
    for c in ctl.query("SELECT id, name, consecutive_failures, failure_category, created_by FROM connections WHERE workspace_id = ?", [ws]):
        ev = ctl.query("SELECT outcome, category, retried, ms, occurred_at FROM connection_events WHERE connection_id = ? AND occurred_at >= ? "
                       "ORDER BY occurred_at DESC LIMIT 40", [c["id"], now - WINDOW])
        if not ev:
            continue
        last10 = ev[:10]
        fails = [e for e in last10 if e["outcome"] != "ok"]
        if ev[0]["outcome"] != "ok" and ev[0]["category"] == "auth":
            out.append({"kind": "auth_failure", "connection": c, "detail": "the latest attempt was refused by the source (authentication)"})
            continue
        if len(fails) >= 2 and ev[0]["outcome"] == "ok":
            out.append({"kind": "intermittent_failures", "connection": c,
                        "detail": f"{len(fails)} of the last {len(last10)} attempts failed ({', '.join(sorted({f['category'] or 'error' for f in fails}))}) "
                                  "although the latest succeeded"})
        limited = [e for e in last10 if "rate_limit" in (_j(e["retried"]) if e["retried"] else [])]
        if len(last10) >= 3 and len(limited) / len(last10) >= 1 / 3:
            out.append({"kind": "rate_limit_pressure", "connection": c,
                        "detail": f"{len(limited)} of the last {len(last10)} attempts were slowed by rate limits"})
        ok_ms = [e["ms"] for e in ev if e["outcome"] == "ok" and e["ms"] is not None]
        if len(ok_ms) >= 10:
            recent, earlier = statistics.median(ok_ms[:5]), statistics.median(ok_ms[5:])
            if recent >= 1000 and earlier > 0 and recent >= 2 * earlier:
                out.append({"kind": "rising_latency", "connection": c,
                            "detail": f"recent calls take {recent / 1000:.1f}s against {earlier / 1000:.1f}s before"})
    db = svc.stores.workspace(ws)
    for d in db.query("SELECT d.name, d.current_version AS v FROM datasets d WHERE d.current_version > 1"):
        rows = {r["version"]: r for r in db.query("SELECT version, schema, created_at FROM dataset_versions WHERE dataset = ? AND version IN (?, ?)",
                                                  [d["name"], d["v"], d["v"] - 1])}
        if len(rows) < 2 or now - _aware(rows[d["v"]]["created_at"]) > WINDOW:
            continue
        before = {c["name"]: c["type"] for c in _j(rows[d["v"] - 1]["schema"])}
        after = {c["name"]: c["type"] for c in _j(rows[d["v"]]["schema"])}
        removed = sorted(set(before) - set(after))
        retyped = sorted(c for c in set(before) & set(after) if before[c] != after[c])
        if removed or retyped:
            out.append({"kind": "schema_change", "dataset": d["name"], "version": d["v"], "removed": removed, "retyped": retyped,
                        "added": sorted(set(after) - set(before)), "before": before, "after": after})
    return out


# -- candidates ------------------------------------------------------------------------------------------------------------

def _metric_specs(svc, ws: str):
    from ..semantic import MetricSpec  # noqa: PLC0415

    out = []
    for r in svc.stores.workspace(ws).query("SELECT m.name, m.owner, v.spec FROM metrics m JOIN metric_versions v ON v.name = m.name "
                                            "AND v.version = m.current_version WHERE m.status = 'approved'"):
        out.append((r["owner"], MetricSpec.model_validate(_j(r["spec"]))))
    return out


def candidates(svc, ws: str, now: datetime, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for sig in signals:
        k = sig["kind"]
        if k in ("credential_expiry", "credential_age"):
            c, cr = sig["connection"], sig["credential"]
            datasets = set(_datasets_of(svc, ws, c["id"]))
            dep = _dependents(svc, ws, datasets, set(), now)
            if k == "credential_expiry":
                when = sig["expires_at"]
                expired = when <= now
                before_run = dep["next_run_at"] is not None and dep["next_run_at"] >= when
                reasons = [f"The credential '{cr['name']}' behind the connection {c['name']} "
                           + (f"expired {when:%d %b %Y}." if expired else f"expires {when:%d %b %Y} ({_days(when - now)}).")]
                if dep["schedules"]:
                    reasons.append(f"It feeds {_at_risk(dep)}"
                                   + (f"; the next run ({dep['next_run_at']:%d %b %H:%M UTC}) is after it expires." if before_run else "."))
                risk = expired or before_run or (when - now) <= timedelta(days=3)
                key = f"health|cred|{cr['id']}|{when.isoformat()}"
                objective = f"Renew the credential for {c['name']}"
                hours = max((when - now).total_seconds() / 3600, 0)
            else:
                when = sig["rotate_by"]
                risk = False
                reasons = [f"The credential '{cr['name']}' behind {c['name']} has not been replaced since "
                           f"{_aware(cr['replaced_at'] or cr['created_at']):%d %b %Y}; the rotation policy is due {when:%d %b %Y}."]
                key = f"health|cred-age|{cr['id']}|{when.date().isoformat()}"
                objective = f"Rotate the credential for {c['name']}"
                hours = max((when - now).total_seconds() / 3600, 0)
            out.append(_cand(k, objective, "connection", c["id"], reasons, risk, hours, key, dep,
                             [{"id": "renew", "label": "Renew in Settings", "kind": "link", "credential_id": cr["id"], "connection_id": c["id"],
                               "href": f"settings/secrets?credential={cr['id']}",
                               "effect": "opens Settings > Secrets, where you paste the new secret; Lyst never shows the current one"}],
                             {"credential": {"id": cr["id"], "fields_expiry": str(sig.get("expires_at") or ""),
                                             "replaced": str(cr["replaced_at"] or "")}},
                             {"signal": {"kind": k, "credential_id": cr["id"], "connection_id": c["id"], "at": when.isoformat()}}, now,
                             [c["created_by"]]))
        elif k in ("auth_failure", "intermittent_failures", "rate_limit_pressure", "rising_latency"):
            c = sig["connection"]
            dep = _dependents(svc, ws, set(_datasets_of(svc, ws, c["id"])), set(), now)
            risk = k == "auth_failure" or (dep["next_run_at"] is not None and dep["next_run_at"] - now <= timedelta(hours=48))
            reasons = [f"The connection {c['name']}: {sig['detail']}.", f"It feeds {_at_risk(dep)}."]
            labels = {"auth_failure": "Fix the sign-in for", "intermittent_failures": "Check the reliability of",
                      "rate_limit_pressure": "Ease rate limits on", "rising_latency": "Check the slowdown of"}
            hours = (dep["next_run_at"] - now).total_seconds() / 3600 if dep["next_run_at"] else 72.0
            out.append(_cand(k, f"{labels[k]} {c['name']}", "connection", c["id"], reasons, risk, max(hours, 0), f"health|conn|{c['id']}|{k}",
                             dep, [{"id": "test_connection", "label": "Test the connection now",
                                    "effect": "runs the connector's read-only test, as you"},
                                   {"id": "open_source", "label": "Open the source", "kind": "link", "connection_id": c["id"],
                                    "href": f"data/sources?connection={c['id']}"}],
                             {"connection": {"consecutive_failures": c["consecutive_failures"], "category": c["failure_category"]}},
                             {"signal": {"kind": k, "connection_id": c["id"], "detail": sig["detail"]}}, now, [c["created_by"]]))
        elif k == "schema_change":
            broken = []
            for owner, spec in _metric_specs(svc, ws):
                if spec.dataset != sig["dataset"]:
                    continue
                used = sorted(spec.columns())
                gone = [c for c in used if c in sig["removed"]]
                typed = [c for c in used if c in sig["retyped"]]
                if gone or typed:
                    broken.append({"metric": spec.name, "owner": owner, "spec": spec, "removed": gone, "retyped": typed})
            if not broken:
                continue
            dep = _dependents(svc, ws, set(), {b["metric"] for b in broken}, now)
            what = "; ".join(f"{b['metric']} uses " + ", ".join(b["removed"] + b["retyped"]) for b in broken)
            reasons = [f"Version {sig['version']} of {sig['dataset']} " + ", ".join(
                p for p in [f"removed {', '.join(sig['removed'])}" if sig["removed"] else "",
                            f"changed the type of {', '.join(sig['retyped'])}" if sig["retyped"] else ""] if p) + ".",
                       f"These definitions will fail: {what}. That puts {_at_risk(dep)} at risk."]
            hours = (dep["next_run_at"] - now).total_seconds() / 3600 if dep["next_run_at"] else 48.0
            cand = _cand(k, f"Prevent breakage from the {sig['dataset']} change", "dataset", sig["dataset"], reasons,
                         bool(dep["schedules"] or dep["assets"]), max(hours, 0), f"health|schema|{sig['dataset']}|v{sig['version']}", dep,
                         [{"id": "open_dataset", "label": "Open the dataset", "kind": "link", "dataset": sig["dataset"],
                           "href": f"data?dataset={sig['dataset']}"}],
                         {"dataset_version": sig["version"]}, {"signal": {"kind": k, "dataset": sig["dataset"], "version": sig["version"],
                                                                          "removed": sig["removed"], "retyped": sig["retyped"],
                                                                          "added": sig["added"]}}, now,
                         sorted({b["owner"] for b in broken}))
            cand["level_required"] = 1
            cand["_ctx"].update({"broken": broken, "signal": sig})
            out.append(cand)
    return out


def _cand(kind, objective, subject_kind, subject_id, reasons, risk, hours, key, dep, actions, depends, extra, now,
          owners: list[str]) -> dict[str, Any]:
    return {
        "kind": "health_foresight", "objective": objective, "subject_kind": subject_kind, "subject_id": subject_id,
        "audience": owners, "signals": [{**extra["signal"], "dependents": {"schedules": len(dep["schedules"]), "assets": len(dep["assets"])}}],
        "reasons": reasons, "level_required": 1,
        "scores": {"value": 0.85 if risk else 0.45, "likelihood": 0.9 if risk else 0.6, "evidence": 0.9,
                   "urgency": round(1 / (1 + hours / 24), 3), "cost": 0.01, "risk": 0.0, "interruption": 0.6 if risk else 0.0},
        "actions": actions, "depends": depends, "needed_at": dep["next_run_at"], "valid_until": now + WINDOW,
        "dedupe_key": key, "_ctx": {"risk": risk, "dependents": dep, "health_kind": kind},
    }


# -- prepare ---------------------------------------------------------------------------------------------------------------

def _swap(spec, old: str, new: str):
    d = json.loads(spec.model_dump_json())

    def walk(v):
        if isinstance(v, dict):
            return {k: (new if k in ("column", "time_column") and x == old else walk(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [new if x == old else walk(x) for x in v]
        return v

    d = walk(d)
    return type(spec).model_validate(d)


def _family(t: str) -> str:
    from ..catalog import _family as fam  # noqa: PLC0415

    return fam(t)


def prepare(svc, ws: str, cand: dict[str, Any], now: datetime, *, automate: bool) -> dict[str, Any]:
    t0 = time.perf_counter()
    ctx = cand["_ctx"]
    checks = [{"check": "dependents", "status": "warn" if ctx["risk"] else "ok",
               "detail": f"{len(ctx['dependents']['schedules'])} scheduled run(s), {len(ctx['dependents']['assets'])} asset(s) depend on it"}]
    prepared: dict[str, Any] = {"dependents": {"schedules": [{**s, "next_run_at": str(s["next_run_at"])} for s in ctx["dependents"]["schedules"]],
                                               "assets": ctx["dependents"]["assets"]}}
    reasons = list(cand["reasons"])
    queries = 0
    if "broken" in ctx:
        from ..semantic import compile_metric  # noqa: PLC0415

        sig = ctx["signal"]
        fixes = []
        for b in ctx["broken"]:
            for old in b["removed"]:
                fam = _family(sig["before"][old])
                options = [a for a in sig["added"] if _family(sig["after"][a]) == fam]
                best = max(options, key=lambda a: difflib.SequenceMatcher(None, old, a).ratio(), default=None)
                if best is None or (len(options) > 1 and difflib.SequenceMatcher(None, old, best).ratio() < 0.5):
                    checks.append({"check": f"repair:{b['metric']}", "status": "warn",
                                   "detail": f"no clear replacement for {old}; a person needs to decide"})
                    continue
                candidate = _swap(b["spec"], old, best)
                r = svc.stores.control.query("SELECT id, email FROM users WHERE id = ?", [b["owner"]])
                try:
                    actor = Principal(r[0]["id"], r[0]["email"])
                    q = compile_metric(candidate, grain="month" if candidate.time_column else None)
                    res = svc.services.data.query(actor, ws, q)
                    queries += 1
                    ok = bool(res.rows)
                except Exception as exc:  # noqa: BLE001 - a candidate that does not run is not proposed
                    ok, res = False, None
                    checks.append({"check": f"repair:{b['metric']}", "status": "warn", "detail": f"the candidate failed: {str(exc)[:120]}"})
                if ok:
                    fixes.append({"metric": b["metric"], "replace": old, "with": best, "spec": json.loads(candidate.model_dump_json()),
                                  "validated_rows": len(res.rows)})
                    checks.append({"check": f"repair:{b['metric']}", "status": "ok",
                                   "detail": f"using {best} for {old} runs on version {sig['version']} ({len(res.rows)} periods)"})
        prepared["fixes"] = fixes
        if fixes:
            cand["actions"].insert(0, {"id": "propose_fix", "label": "Propose the fix for approval",
                                       "effect": "files the corrected definition as a review; an admin approves it before anything changes"})
            reasons.append("A candidate fix was validated on the new version: " + "; ".join(
                f"{f['metric']}: use {f['with']} for {f['replace']}" for f in fixes) + ". Nothing changes until an admin approves it.")
    return {"verdict": "risk" if ctx["risk"] else "watch", "prepared": prepared, "verification": {"checks": checks, "complete": True},
            "scores": {}, "actions": cand["actions"],
            "resources": {"compute_ms": int((time.perf_counter() - t0) * 1000), "queries": queries, "model_calls": 0, "refreshes": 0},
            "reasons": reasons}


def summary(a: dict[str, Any]) -> str:
    return " ".join(a["reasons"][:3])


def run(svc, actor: Principal, ws: str, a: dict[str, Any], action: str) -> dict[str, Any]:
    act = next(x for x in a["actions"] if x["id"] == action)
    if act.get("kind") == "link":   # the UI routes by the action's id and these ids; href is a fallback
        return {"open": act["href"], **{k: act[k] for k in ("credential_id", "connection_id", "dataset") if k in act}}
    if action == "test_connection":
        r = svc.connections.test(actor, ws, a["subject_id"])
        return {"test": {"ok": bool(r.get("ok", True))}}
    if action == "propose_fix":
        out = []
        for f in (a.get("prepared") or {}).get("fixes", []):
            r = svc.collab.propose(actor, ws, "metric.define", object_kind="metric", object_id=f["metric"], payload=f["spec"],
                                   summary=f"Use {f['with']} for {f['replace']} after the {a['subject_id']} schema change")
            out.append(r.get("id") or r.get("review_id"))
        return {"reviews": out, "needs_approval": True}
    raise ValueError(f"{action} is not available here")
