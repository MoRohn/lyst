# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Capability-gap forecasting through the Capability Foundry: a method that upcoming approved work will need, found
before that work runs.

Signals:

* an enabled, approved schedule whose question names a method (the Foundry's own ``detect_gap``) that no approved skill
  covers;
* an open assignment with a due date whose note names such a method;
* the same missing method hit by two or more finished tasks in 30 days (the Foundry reported a gap or a failed draft).

Preparation (level 2, offline): what Lyst can say without the internet. Whether a Foundry template exists; which
open-source libraries suited to the method are *installed here* (checked with ``importlib``, never fetched); the
contract a skill must meet; and the input query when the question names an approved metric. At level 3 a template-backed
candidate is drafted and run through the same static check, sandbox, representative and adversarial tests as any
proposal; its benchmark is recorded.

Promotion is never automatic: accepting asks for registration, which an owner who did not propose it must approve,
exactly as for a person's own proposal. Nothing here elevates privileges, changes security controls or deploys a skill.
"""

from __future__ import annotations

import importlib.util
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from ..security import Principal

KINDS = ("capability_gap",)
RESOLVES = True

# methods Lyst is asked for, and the open-source libraries suited to each; only what is installed is reported
LIBRARIES: dict[str, list[tuple[str, str]]] = {
    "herfindahl": [("numpy", "sum of squared shares")],
    "gini": [("numpy", "sorted cumulative shares (the Lorenz curve)")],
    "pareto": [("pandas", "rank members and take the top 20 percent")],
    "theil": [("numpy", "entropy of shares: sum(s * ln(s * n)) / n"), ("scipy", "scipy.stats.entropy")],
    "entropy": [("scipy", "scipy.stats.entropy")],
    "cohort": [("pandas", "group by first-seen period and age")],
    "survival": [("lifelines", "Kaplan-Meier and Cox models"), ("statsmodels", "statsmodels.duration")],
    "regression": [("statsmodels", "OLS with inference"), ("scikit-learn", "sklearn.linear_model")],
    "cluster": [("scikit-learn", "sklearn.cluster.KMeans")],
    "arima": [("statsmodels", "statsmodels.tsa.arima")],
    "elasticity": [("statsmodels", "log-log OLS")],
}
_MODULE = {"scikit-learn": "sklearn"}


def _aware(ts):
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=UTC)


def _owner(svc, user_id: str) -> Principal | None:
    r = svc.stores.control.query("SELECT id, email FROM users WHERE id = ?", [user_id])
    return Principal(r[0]["id"], r[0]["email"]) if r else None


def _covered(svc, ws: str, actor: Principal, gap: dict[str, Any]) -> bool:
    try:
        return bool(svc.services.skills.match(actor, ws, gap["method"]))
    except Exception:  # noqa: BLE001 - no skills service or no access: treat as not covered
        return False


def observe(svc, ws: str, now: datetime) -> list[dict[str, Any]]:
    from ..analyst.foundry import detect_gap  # noqa: PLC0415

    out = []
    for s in svc.stores.control.query("SELECT * FROM schedules WHERE workspace_id = ? AND enabled", [ws]):
        q = (json.loads(s["request"]) if isinstance(s["request"], str) else s["request"]).get("question", "")
        gap = detect_gap(q)
        owner = _owner(svc, s["created_by"])
        if gap and owner and not _covered(svc, ws, owner, gap):
            out.append({"kind": "scheduled_method", "gap": gap, "text": q, "owner": owner, "subject": ("schedule", s["id"]),
                        "title": s["title"], "needed_at": _aware(s["next_run_at"])})
    db = svc.stores.workspace(ws)
    for a in db.query("SELECT * FROM assignments WHERE status = 'open' AND due IS NOT NULL AND note IS NOT NULL"):
        gap = detect_gap(a["note"])
        owner = _owner(svc, a["assignee"])
        if gap and owner and not _covered(svc, ws, owner, gap):
            due = datetime.combine(a["due"], datetime.min.time(), tzinfo=UTC)
            out.append({"kind": "assigned_method", "gap": gap, "text": a["note"], "owner": owner, "subject": ("assignment", a["id"]),
                        "title": a["note"][:80], "needed_at": due})
    hits: dict[str, list[dict[str, Any]]] = {}
    for t in svc.stores.control.query("SELECT id, created_by, request, created_at FROM tasks WHERE workspace_id = ? AND status = 'done' "
                                      "AND created_at >= ?", [ws, now - timedelta(days=30)]):
        q = (json.loads(t["request"]) if isinstance(t["request"], str) else t["request"]).get("question", "")
        gap = detect_gap(q)
        if gap:
            hits.setdefault(gap["label"], []).append({"task": t, "gap": gap, "text": q})
    for label, hs in hits.items():
        owner = _owner(svc, hs[-1]["task"]["created_by"])
        if len(hs) >= 2 and owner and not _covered(svc, ws, owner, hs[0]["gap"]):
            out.append({"kind": "repeated_limitation", "gap": hs[0]["gap"], "text": hs[-1]["text"], "owner": owner,
                        "subject": ("method", label), "title": label, "needed_at": None, "times": len(hs)})
    return out


def _metric_for(svc, ws: str, text: str):
    from ..semantic import MetricSpec  # noqa: PLC0415
    from . import recurring  # noqa: PLC0415

    for r in svc.stores.workspace(ws).query("SELECT v.spec FROM metrics m JOIN metric_versions v ON v.name = m.name "
                                            "AND v.version = m.current_version WHERE m.status = 'approved'"):
        spec = MetricSpec.model_validate(json.loads(r["spec"]) if isinstance(r["spec"], str) else r["spec"])
        if recurring._matches(text, spec):  # noqa: SLF001
            return spec
    return None


def candidates(svc, ws: str, now: datetime, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    seen: set[tuple[str, str]] = set()
    for sig in signals:
        gap, (sk, sid) = sig["gap"], sig["subject"]
        if (gap["label"], sid) in seen:
            continue
        seen.add((gap["label"], sid))
        if sig["kind"] == "scheduled_method":
            why = (f"The approved schedule '{sig['title']}' asks for {gap['method']}"
                   + (f"; its next run is {sig['needed_at']:%d %b %H:%M UTC}" if sig["needed_at"] else "") + ".")
        elif sig["kind"] == "assigned_method":
            why = f"An assignment due {sig['needed_at']:%d %b %Y} asks for {gap['method']}."
        else:
            why = f"{gap['method'].capitalize()} was asked for {sig.get('times')} times in 30 days."
        hours = (sig["needed_at"] - now).total_seconds() / 3600 if sig["needed_at"] else 24 * 14
        out.append({
            "kind": "capability_gap", "objective": f"Prepare a {gap['method']} capability before it is needed",
            "subject_kind": sk, "subject_id": sid, "audience": [sig["owner"].user_id],
            "signals": [{"kind": sig["kind"], "method": gap["method"], "template": gap["template"], "text": sig["text"][:200]}],
            "reasons": [why, "Lyst has no approved skill for it yet, so that work would stop at a gap."],
            "scores": {"value": 0.7, "likelihood": 0.9 if sig["kind"] != "repeated_limitation" else 0.6, "evidence": 0.8,
                       "urgency": round(1 / (1 + max(hours, 0) / 48), 3), "cost": 0.05, "risk": 0.0, "interruption": 0.0},
            "level_required": 1, "actions": [], "depends": {"skills": gap["label"]},
            "needed_at": sig["needed_at"], "valid_until": (sig["needed_at"] or now + timedelta(days=14)) + timedelta(days=1),
            "dedupe_key": f"capgap|{gap['label']}|{sk}|{sid}",
            "_ctx": {"gap": gap, "owner": sig["owner"], "text": sig["text"]},
        })
    return out


def prepare(svc, ws: str, cand: dict[str, Any], now: datetime, *, automate: bool) -> dict[str, Any]:
    from ..analyst import foundry  # noqa: PLC0415

    t0 = time.perf_counter()
    ctx = cand["_ctx"]
    gap, owner = ctx["gap"], ctx["owner"]
    key = gap["method"] if gap["template"] else next((k for k in LIBRARIES if k in gap["method"]), None)
    libs = []
    for name, use in LIBRARIES.get(key or "", []):
        installed = importlib.util.find_spec(_MODULE.get(name, name)) is not None
        libs.append({"library": name, "use": use, "installed": installed})
    spec = _metric_for(svc, ws, ctx["text"])
    checks = [{"check": "template", "status": "ok" if gap["template"] else "warn",
               "detail": f"the Foundry has a tested template ({foundry.TEMPLATES[gap['method']]['name']})" if gap["template"]
               else "no template: a draft needs a configured model or a person"},
              {"check": "libraries", "status": "ok" if any(x["installed"] for x in libs) else "warn",
               "detail": ", ".join(f"{x['library']} ({'installed' if x['installed'] else 'not installed'})" for x in libs)
               or "no known open-source library recorded for this method"}]
    proposal: dict[str, Any] = {"method": gap["method"], "skill": gap["label"], "libraries": libs,
                                "contract": "run(df[member, value], **params) -> dict with a 'summary' string and optionally a 'table'"}
    if spec is not None:
        try:
            proposal["input_sql"], proposal["dimension"] = foundry._input_sql(spec, None)  # noqa: SLF001
            proposal["metric"] = spec.name
        except ValueError as exc:
            checks.append({"check": "input", "status": "warn", "detail": str(exc)})
    actions: list[dict[str, Any]] = []
    queries = 0
    if gap["template"] and spec is not None and svc.level(ws) >= 3 and "input_sql" in proposal:
        t = foundry.TEMPLATES[gap["method"]]
        skills = svc.services.skills
        try:
            p = skills.propose(owner, ws, name=gap["label"], purpose=t["purpose"], source=t["source"], input_sql=proposal["input_sql"],
                               tests=t["tests"], triggers=t["triggers"])
            b0 = time.perf_counter()
            tested = skills.test(owner, ws, gap["label"], p["version"]) if p["status"] == "proposed" else {"status": p["status"], "report": {}}
            bench = int((time.perf_counter() - b0) * 1000)
            rep = tested.get("report") or {}
            proposal["candidate"] = {"version": p["version"], "status": tested["status"], "benchmark_ms": bench,
                                     "representative": rep.get("representative"), "adversarial": rep.get("adversarial")}
            checks.append({"check": "sandbox", "status": "ok" if tested["status"] == "tested" else "block",
                           "detail": f"candidate v{p['version']} {tested['status']} in the sandbox in {bench} ms"})
            queries += 1
            if tested["status"] == "tested":
                actions.append({"id": "request_registration", "label": "Ask an owner to approve it",
                                "effect": "requests registration; an owner who did not propose it approves before it can run"})
        except Exception as exc:  # noqa: BLE001 - a failed draft is reported, not raised
            checks.append({"check": "sandbox", "status": "block", "detail": f"the candidate could not be tested: {str(exc)[:120]}"})
    reasons = list(cand["reasons"])
    if "candidate" in proposal and proposal["candidate"]["status"] == "tested":
        c = proposal["candidate"]
        reasons.append(f"Lyst drafted {gap['label']} v{c['version']} from its template and it passed {c['representative']} representative "
                       f"and {c['adversarial']} adversarial cases in the sandbox; it still needs an owner's approval.")
    elif any(x["installed"] for x in libs):
        reasons.append("Suitable open-source libraries are already installed: "
                       + ", ".join(x["library"] for x in libs if x["installed"]) + ".")
    return {"verdict": "ready" if actions else "proposal", "prepared": {"proposal": proposal}, "actions": actions,
            "verification": {"checks": checks, "complete": True},
            "resources": {"compute_ms": int((time.perf_counter() - t0) * 1000), "queries": queries, "model_calls": 0, "refreshes": 0},
            "reasons": reasons}


def run(svc, actor: Principal, ws: str, a: dict[str, Any], action: str) -> dict[str, Any]:
    if action != "request_registration":
        raise ValueError(f"{action} is not available here")
    c = ((a.get("prepared") or {}).get("proposal") or {}).get("candidate") or {}
    if c.get("status") != "tested":
        raise ValueError("no tested candidate to register")
    name = a["prepared"]["proposal"]["skill"]
    approval = svc.services.skills.request_registration(actor, ws, name, int(c["version"]), svc.runtime)
    return {"approval_id": approval, "needs_approval": True, "skill": name, "version": c["version"]}
