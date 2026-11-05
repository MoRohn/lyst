# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The task harness: a contract per task, a change receipt per run, and failures that upgrade the harness.

The model proposes; the task runtime decides what runs, with which budget, and whether the result counts. This
module adds four things to that runtime (docs/HARNESS.md):

* **Contract.** At submission every task gets a frozen, hashed contract: the goal (the question as asked), inputs,
  the expected output, constraints (autonomy, step, time and cost budgets, retry cap, approvals, refused tools,
  recorded decisions) and ``done_when``, a list of checks a program can fail. At the end of each run the checks are
  evaluated against the stored task, its steps and its result, never against the model's own say-so. A task whose
  run finished but whose contract is not met is not reported as done-and-verified.
* **Receipt.** Every run that ends (done, failed or cancelled) leaves one immutable receipt: context sources, the
  data policy and harness rule versions in force, the model route, the tools used, the checks, retries, human
  corrections, cost, the accepted artifact and the rollback point. Receipts make runs comparable across model
  changes and failures attributable.
* **Failure classes.** A run that failed or missed its contract is classified (missing context, wrong tool, bad
  output, repeated loop, unsafe action, lost decision, unknown). When the same failure repeats on the same job, a
  harness update is *proposed* (a guide, a test, a tool route, a permission gate, a recorded decision, or a budget)
  and goes to the workspace's approvers. Nothing changes until a person approves it; an approved rule then applies
  to every later run of that job, mechanically. The next run does not need to remember the incident.
* **State.** :func:`state` reconstructs the durable state of a task (current step, artifacts, decisions, failures,
  pending approvals, budgets used, where it would resume) from records, not from a conversation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from typing import Any

log = logging.getLogger("lyst.harness")

FAILURE_CLASSES = ("missing_context", "wrong_tool", "bad_output", "repeated_loop", "unsafe_action", "lost_decision",
                   "unknown")
# the class of failure -> the kind of permanent harness change that fixes it (None: nothing to learn)
FIX_FOR = {"missing_context": "guide", "wrong_tool": "tool", "bad_output": "test", "repeated_loop": "budget",
           "unsafe_action": "permission", "lost_decision": "state", "unknown": None}
RULE_KINDS = ("guide", "test", "tool", "permission", "state", "budget")
RULE_STATUS = ("proposed", "active", "rejected", "retired")

# every check a contract can name; each one is evaluated from records a program reads
CHECKS = {
    "steps_finished": "every planned step finished (an optional step may be skipped)",
    "within_budget": "the run stayed inside its step, time and cost budgets",
    "goal_kept": "the question answered is the question asked",
    "outcome_complete": "the result is complete: verified, not unverified or waiting for input",
    "validation_passed": "no validation check failed",
    "has_sources": "the answer lists the dataset versions and queries it came from",
    "no_validation_warnings": "no validation check warned",
}
DEFAULT_DONE_WHEN = ("steps_finished", "within_budget", "goal_kept", "outcome_complete", "validation_passed")
OUTPUT_FOR = {"analysis": "a verified answer with its evidence", "scheduled": "a verified answer with its evidence"}


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else None


def default_budget_usd() -> float | None:
    """Model spend one task may use (``LYST_TASK_BUDGET_USD``, default 1.00; 0 for no cap)."""
    return _env_float("LYST_TASK_BUDGET_USD", 1.0)


def default_max_retries() -> int:
    """How many times a person may retry one failed task before it must be asked again (``LYST_TASK_MAX_RETRIES``)."""
    try:
        return max(0, int(os.environ.get("LYST_TASK_MAX_RETRIES", "3")))
    except ValueError:
        return 3


def job_key(question: str) -> str:
    """The job a request is an instance of: the question, lower-cased, without punctuation or numbers' digits."""
    text = re.sub(r"[^a-z0-9 ]+", " ", (question or "").lower())
    return re.sub(r"\s+", " ", text).strip()[:200]


def _sig(text: str) -> str:
    """A failure's signature: its message with ids, numbers and quoted values taken out, so repeats match."""
    t = re.sub(r"'[^']*'|\"[^\"]*\"", "_", (text or "").lower())
    t = re.sub(r"\b[a-z]{1,3}_[0-9a-f]{6,}\b|\d+(\.\d+)?", "#", t)
    return re.sub(r"\s+", " ", t).strip()[:120]


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _loads(v: Any) -> Any:
    if v is None or isinstance(v, dict | list):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


# -- rules ----------------------------------------------------------------------------------------------------------


def active_rules(db, ws: str, key: str | None = None) -> list[dict[str, Any]]:
    """The approved rules that apply to a job (or to every job, ``job_key = '*'``)."""
    rows = db.query("SELECT * FROM harness_rules WHERE workspace_id = ? AND status = 'active' ORDER BY created_at",
                    [ws])
    out = []
    for r in rows:
        if key is not None and r["job_key"] not in ("*", key):
            continue
        r["spec"] = _loads(r["spec"]) or {}
        out.append(r)
    return out


def rules_version(db, ws: str) -> str:
    """A short fingerprint of the active rules: two runs with the same value ran under the same harness."""
    ids = [r["id"] for r in db.query("SELECT id FROM harness_rules WHERE workspace_id = ? AND status = 'active' "
                                     "ORDER BY id", [ws])]
    return f"{len(ids)}:{_sha(ids)[:10]}" if ids else "0"


def _hit(db, rule_id: str) -> None:
    db.execute("UPDATE harness_rules SET hits = hits + 1 WHERE id = ?", [rule_id])


def refused_tool(db, ws: str, key: str, capability: str) -> dict[str, Any] | None:
    """An approved tool rule that routes this job away from a capability, or None."""
    for r in active_rules(db, ws, key):
        if r["kind"] == "tool" and r["capability"] == capability:
            _hit(db, r["id"])
            return r
    return None


def gated(db, ws: str, key: str, capability: str) -> dict[str, Any] | None:
    """An approved permission rule that makes this capability wait for a person on this job, or None."""
    for r in active_rules(db, ws, key):
        if r["kind"] == "permission" and r["capability"] in (capability, "*"):
            _hit(db, r["id"])
            return r
    return None


# -- contracts ------------------------------------------------------------------------------------------------------


def build_contract(db, ws: str, *, question: str, kind: str, autonomy: str, max_steps: int, budget_seconds: int,
                   budget_usd: float | None, request: dict[str, Any]) -> dict[str, Any]:
    """The bounded object a task is held to. Approved rules for this job tighten it; nothing loosens it."""
    key = job_key(question)
    rules = active_rules(db, ws, key)
    done_when = list(DEFAULT_DONE_WHEN)
    checks_named: list[str] = []
    max_retries = default_max_retries()
    refused, gates, decisions, guides, applied = [], [], [], [], []
    for r in rules:
        spec = r["spec"]
        applied.append(r["id"])
        if r["kind"] == "test":
            for c in spec.get("checks") or []:
                if c in CHECKS and c not in done_when:
                    done_when.append(c)
            for name in spec.get("validation_checks") or []:
                if f"check:{name}" not in checks_named:
                    checks_named.append(f"check:{name}")
        elif r["kind"] == "budget":
            if spec.get("max_steps"):
                max_steps = min(max_steps, int(spec["max_steps"]))
            if spec.get("budget_seconds"):
                budget_seconds = min(budget_seconds, int(spec["budget_seconds"]))
            if spec.get("budget_usd") is not None:
                cap = float(spec["budget_usd"])
                budget_usd = cap if budget_usd is None else min(budget_usd, cap)
            if spec.get("max_retries") is not None:
                max_retries = min(max_retries, int(spec["max_retries"]))
        elif r["kind"] == "tool":
            refused.append({"capability": r["capability"], "rule": r["id"], "why": r["text"]})
        elif r["kind"] == "permission":
            gates.append({"capability": r["capability"], "rule": r["id"], "why": r["text"]})
        elif r["kind"] == "state":
            decisions.append({"rule": r["id"], "decision": r["text"]})
        elif r["kind"] == "guide":
            guides.append({"rule": r["id"], "guide": r["text"]})
    inputs = [f"workspace:{ws}"]
    if request.get("workflow"):
        inputs.append(f"workflow:{(request['workflow'] or {}).get('id', 'inline')}")
    if request.get("expertise"):
        inputs.append("assembled expertise")
    if request.get("channel"):
        inputs.append(f"channel:{request['channel']}")
    contract = {
        "version": 1,
        "goal": " ".join((question or "").split()),
        "job": key,
        "kind": kind,
        "inputs": inputs,
        "output": OUTPUT_FOR.get(kind, f"a finished {kind} task with its result"),
        "constraints": {
            "autonomy": autonomy,
            "max_steps": max_steps,
            "budget_seconds": budget_seconds,
            "budget_usd": budget_usd,
            "max_retries": max_retries,
            "approval_required_for": gates,
            "refused_tools": refused,
            "decisions": decisions,
            "guides": guides,
            "never": ["decide its own approvals", "read data above the workspace policy"],
        },
        "done_when": done_when + checks_named,
        "rules_applied": applied,
    }
    return contract


def save_contract(c, task_id: str, ws: str, contract: dict[str, Any]) -> None:
    """Written in the submit transaction, so a task never exists without its contract."""
    c.execute("INSERT INTO task_contracts (task_id, workspace_id, job_key, contract, sha256) VALUES (?, ?, ?, ?, ?)",
              [task_id, ws, contract["job"], json.dumps(contract, default=str), _sha(contract)])


def contract_of(db, task_id: str) -> dict[str, Any] | None:
    rows = db.query("SELECT contract, sha256 FROM task_contracts WHERE task_id = ?", [task_id])
    if not rows:
        return None
    out = _loads(rows[0]["contract"]) or {}
    out["sha256"] = rows[0]["sha256"]
    out["intact"] = _sha({k: v for k, v in out.items() if k not in ("sha256", "intact")}) == rows[0]["sha256"]
    return out


def task_cost(db, task_id: str) -> float:
    return float(db.scalar("SELECT coalesce(sum(cost_usd), 0) FROM llm_calls WHERE task_id = ?", [task_id]) or 0.0)


def check_contract(contract: dict[str, Any], task: dict[str, Any], steps: list[dict[str, Any]],
                   result: dict[str, Any] | None, cost_usd: float) -> list[dict[str, Any]]:
    """Each ``done_when`` item, with passed (True, False, or None when it does not apply) and the evidence read."""
    result = result or {}
    cons = contract.get("constraints") or {}
    checks = ((result.get("confidence") or {}).get("checks")) or []
    out = []
    for item in contract.get("done_when") or []:
        passed: bool | None
        if item == "steps_finished":
            bad = [s["capability"] for s in steps if s["status"] not in ("done", "skipped")]
            passed = task["status"] == "done" and not bad
            evidence = (f"{len(steps)} steps, all finished" if passed else
                        f"task {task['status']}" + (f"; unfinished: {', '.join(bad[:4])}" if bad else ""))
        elif item == "within_budget":
            err = (task.get("error") or "").lower()
            over_cost = cons.get("budget_usd") is not None and cost_usd > float(cons["budget_usd"]) + 1e-9
            passed = "budget" not in err and not over_cost
            evidence = (f"cost ${cost_usd:.4f}" + (f" of ${float(cons['budget_usd']):.2f}" if cons.get("budget_usd")
                                                   is not None else "") + f"; {len(steps)} of {cons.get('max_steps')} steps"
                        + ("" if passed else f"; {task.get('error') or 'over the cost budget'}"))
        elif item == "goal_kept":
            asked = result.get("question")
            if task["status"] != "done" or not asked:
                passed, evidence = None, "no restated question in the result"
            else:
                passed = job_key(asked) == contract.get("job")
                evidence = "answered the question as asked" if passed else f"answered \"{asked[:120]}\" instead"
        elif item == "outcome_complete":
            st = result.get("status") if task["status"] == "done" else None
            passed = st == "complete" if task["status"] == "done" else False
            evidence = f"outcome {st}" if task["status"] == "done" else f"task {task['status']}"
        elif item == "validation_passed":
            if not checks:
                passed, evidence = (None, "no validation checks recorded") if task["status"] == "done" else \
                    (False, f"task {task['status']}")
            else:
                failed = [c.get("name") or "check" for c in checks if _cstat(c) == "fail"]
                passed = not failed
                evidence = f"{len(checks)} checks, none failed" if passed else f"failed: {', '.join(map(str, failed[:4]))}"
        elif item == "no_validation_warnings":
            warned = [c.get("name") or "check" for c in checks if _cstat(c) == "warn"]
            passed = bool(checks) and not warned if task["status"] == "done" else False
            evidence = (f"warned: {', '.join(map(str, warned[:4]))}" if warned else
                        (f"{len(checks)} checks, none warned" if checks else "no validation checks recorded"))
        elif item == "has_sources":
            sup = result.get("supporting") or {}
            n_src, n_q = len(sup.get("sources") or []), len(sup.get("queries") or [])
            passed = task["status"] == "done" and n_src > 0 and n_q > 0
            evidence = f"{n_src} sources, {n_q} queries"
        elif item.startswith("check:"):
            name = item.split(":", 1)[1]
            hits = [c for c in checks if c.get("name") == name]
            passed = bool(hits) and all(_cstat(c) == "pass" for c in hits)
            evidence = f"{name}: {_cstat(hits[0])}" if hits else f"{name} did not run"
        else:
            passed, evidence = False, "unknown check"
        out.append({"id": item, "text": CHECKS.get(item, f"validation check {item[6:]} passes"
                                                    if item.startswith("check:") else item),
                    "passed": passed, "evidence": evidence})
    return out


def _cstat(c: dict[str, Any]) -> str:
    """A validation check's state: pass, warn (a failed warning) or fail (a failed error)."""
    if c.get("passed"):
        return "pass"
    return "warn" if c.get("severity") == "warning" else "fail"


def met(results: list[dict[str, Any]]) -> bool:
    return all(r["passed"] is not False for r in results)


# -- failure classification -------------------------------------------------------------------------------------------


def classify(task: dict[str, Any], steps: list[dict[str, Any]], results: list[dict[str, Any]],
             result: dict[str, Any] | None, rejected: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Which class of failure a run is, the capability involved and the evidence, or None (nothing failed)."""
    err = task.get("error") or ""
    low = err.lower()
    failed_step = next((s for s in reversed(steps) if s["status"] == "failed"), None)
    cap = failed_step["capability"] if failed_step else None
    step_err = (failed_step or {}).get("error") or ""
    fclass = step_err.split(":", 1)[0].strip() if failed_step else ""
    if rejected:  # a person said no to a step: that step should have asked before running, on this job
        a = rejected[0]
        return {"class": "unsafe_action", "capability": a.get("capability"),
                "evidence": f"a person rejected {a.get('summary') or a.get('action')}"}
    if task["status"] == "failed":
        if "budget" in low or "abandoned" in low or "max_steps" in low:
            return {"class": "repeated_loop", "capability": cap, "evidence": err}
        if fclass == "permission":
            return {"class": "unsafe_action", "capability": cap, "evidence": step_err}
        if "harness rule" in low:
            return None  # a rule already routed this job; the rule is the fix, not a new failure
        if fclass in ("input", "data"):
            if re.search(r"not found|unknown|missing|no such|does not exist|no metric|not defined", step_err.lower()):
                return {"class": "missing_context", "capability": cap, "evidence": step_err}
            return {"class": "wrong_tool", "capability": cap, "evidence": step_err}
        if fclass in ("transient", "model"):
            return None  # a blip teaches nothing; the runtime already retried it
        return {"class": "unknown", "capability": cap, "evidence": err or "failed with no recorded step error"}
    if task["status"] != "done":
        return None
    st = (result or {}).get("status")
    if st == "needs_input":
        return {"class": "lost_decision", "capability": None,
                "evidence": ((result or {}).get("answer") or "the task stopped to ask the person")[:300]}
    bad = [r for r in results if r["passed"] is False]
    if bad:
        if bad[0]["id"] == "goal_kept":
            return {"class": "missing_context", "capability": "understand", "evidence": bad[0]["evidence"]}
        return {"class": "bad_output", "capability": None,
                "evidence": "; ".join(f"{r['id']}: {r['evidence']}" for r in bad[:3])}
    return None


def _draft(fix: str, failure: dict[str, Any], contract: dict[str, Any], result: dict[str, Any] | None,
           checks: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """The proposed rule's text and spec, drafted from the evidence. The approver may edit the text first."""
    goal = contract.get("goal", "")[:120]
    cap = failure.get("capability")
    ev = (failure.get("evidence") or "")[:240]
    if fix == "guide":
        return (f"For requests like \"{goal}\": {ev}. Name the metric, dataset or period this needs.",
                {"layer": "procedural"})
    if fix == "state":
        return (f"Decision for \"{goal}\": record the answer to what Lyst asked ({ev}) so later runs use it.",
                {"layer": "team"})
    if fix == "test":
        vchecks = ((result or {}).get("confidence") or {}).get("checks") or []
        named = sorted({str(c["name"]) for c in vchecks if _cstat(c) != "pass" and c.get("name")})[:5]
        return (f"Runs of \"{goal}\" count as done only when " +
                (f"these checks pass: {', '.join(named)}" if named else "every validation check passes and the "
                                                                          "answer lists its sources"),
                {"checks": ["no_validation_warnings", "has_sources"] if not named else ["has_sources"],
                 "validation_checks": named})
    if fix == "tool":
        return (f"Do not use {cap} for \"{goal}\": it failed the same way more than once ({ev}).", {})
    if fix == "permission":
        return (f"Ask a person before {cap or 'any approval-gated step'} runs for \"{goal}\" ({ev}).", {})
    # budget: fail fast and escalate instead of spending the same budget on the same loop
    cons = contract.get("constraints") or {}
    return (f"\"{goal}\" stopped on its budget more than once ({ev}). Later runs stop at half the time budget and "
            "are not retried; a person changes the request instead.",
            {"budget_seconds": max(60, int((cons.get("budget_seconds") or 600) / 2)), "max_retries": 0})


# -- the run's end: checks, receipt, failure, proposal ----------------------------------------------------------------


def record_run(runtime, task_id: str, state: str) -> dict[str, Any] | None:
    """After a run ends: evaluate the contract, write the receipt, classify a failure and maybe propose a fix.
    Never changes the task's own state. Returns the receipt."""
    if state not in ("done", "failed", "cancelled"):
        return None
    db = runtime.db
    t = db.query("SELECT * FROM tasks WHERE id = ?", [task_id])
    if not t:
        return None
    task = t[0]
    ws = task["workspace_id"]
    contract = contract_of(db, task_id)
    if contract is None:
        return None  # submitted before contracts existed
    steps = db.query("SELECT seq, phase, capability, agent, status, error, output FROM task_steps WHERE task_id = ? "
                     "ORDER BY seq", [task_id])
    result = _loads(task["result"])
    cost = task_cost(db, task_id)
    results = check_contract(contract, task, steps, result, cost) if state != "cancelled" else []
    run = int(db.scalar("SELECT coalesce(max(run), 0) FROM task_receipts WHERE task_id = ?", [task_id]) or 0) + 1
    approvals = db.query("SELECT id, action, summary, status, payload, decided_by FROM approvals WHERE task_id = ?",
                         [task_id])
    rejected = []
    for a in approvals:
        if a["status"] == "rejected" and a["action"].startswith("step:"):
            seq = int(a["action"].split(":", 1)[1])
            planned = ["understand", "contextualize", "plan"] + [x["capability"] for x in
                                                                   (_loads(task["plan"]) or {}).get("steps") or []]
            capability = next((s["capability"] for s in steps if s["seq"] == seq),
                              planned[seq] if seq < len(planned) else None)
            rejected.append({**a, "capability": capability})
    receipt = _receipt(runtime, task, contract, steps, result, cost, results, run, approvals)
    ok = met(results) if state != "cancelled" else None

    def tx(c):
        c.execute("INSERT INTO task_receipts (task_id, run, workspace_id, state, contract_met, receipt, sha256) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  [task_id, run, ws, state, ok, json.dumps(receipt, default=str), _sha(receipt)])

    db.write(tx)
    if state == "done" and ok is False:
        bad = [r["id"] for r in results if r["passed"] is False]
        runtime.event(task_id, "contract_missed", f"Finished, but its contract is not met: {', '.join(bad)}",
                      {"checks": bad})
    failure = classify(task, steps, results, result, rejected) if state != "cancelled" or rejected else None
    if failure:
        _record_failure(runtime, task, contract, run, failure, result,
                        ((result or {}).get("confidence") or {}).get("checks") or [])
    return receipt


def _receipt(runtime, task, contract, steps, result, cost, results, run, approvals) -> dict[str, Any]:
    db = runtime.db
    ws = task["workspace_id"]
    calls = db.query("SELECT model, provider, tier, workload, purpose, outcome, coalesce(attempt, 1) AS attempt, "
                     "input_tokens, output_tokens FROM llm_calls WHERE task_id = ? ORDER BY id", [task["id"]])
    ctx_out = next((_loads(s["output"]) for s in steps if s["capability"] == "contextualize" and s["status"] == "done"),
                   None) or {}
    sources = []
    for m in ctx_out.get("metrics") or []:
        spec = m.get("spec") or {}
        sources.append({"kind": "metric", "name": spec.get("name"), "dataset": spec.get("dataset"),
                        "dataset_version": m.get("dataset_version")})
    sources += [{"kind": "memory", "id": m.get("id"), "trust": m.get("trust")} for m in ctx_out.get("memory") or []]
    sources += [{"kind": "document", "id": p.get("id") or p.get("chunk_id") or p.get("document_id")}
                for p in ctx_out.get("passages") or [] if isinstance(p, dict)]
    if ctx_out.get("prior_answers"):
        sources.append({"kind": "prior_answers", "count": len(ctx_out["prior_answers"])})
    sup = (result or {}).get("supporting") or {}
    read_versions = {f"{s.get('dataset')}": s.get("version") for s in sup.get("sources") or [] if s.get("dataset")}
    try:
        policy = runtime.stores.workspace(ws).scalar("SELECT max(version) FROM data_policies") or 0
    except Exception:  # noqa: BLE001 - an unreadable workspace file must not lose the receipt
        policy = None
    vchecks = ((result or {}).get("confidence") or {}).get("checks") or []
    tally = {k: sum(1 for c in vchecks if _cstat(c) == k) for k in ("pass", "warn", "fail")}
    retry_events = db.scalar("SELECT count(*) FROM task_events WHERE task_id = ? AND kind = 'retry'", [task["id"]]) or 0
    mutating = []
    for s in steps:
        try:
            cap = runtime.services.registry.get(s["capability"])
        except Exception:  # noqa: BLE001,S112 - a capability retired since the run
            continue
        if cap.mutating and s["status"] == "done":
            mutating.append(s["capability"])
    artifact = None
    if result:
        for k in ("artifact_id", "dashboard_id", "publication_id", "export_id", "metric", "workflow_id"):
            v = result.get(k) or (result.get("supporting") or {}).get(k)
            if isinstance(v, str | int) and v:
                artifact = {"kind": k.removesuffix("_id"), "id": v}
                break
        if artifact is None and task["status"] == "done":
            artifact = {"kind": "answer", "id": task["id"], "status": result.get("status")}
    return {
        "task_id": task["id"],
        "run": run,
        "state": task["status"],
        "contract": {"sha256": contract["sha256"], "intact": contract["intact"], "met": met(results) if results else None,
                     "checks": results},
        "context_sources": sources,
        "policy_version": {"data_policy": policy, "harness_rules": rules_version(db, ws),
                           "rules_applied": contract.get("rules_applied") or []},
        "model_route": sorted({f"{c['workload'] or c['purpose']}→{c['model']}" for c in calls if c["model"]}),
        "tools_used": [s["capability"] for s in steps if s["status"] == "done"],
        "checks": {"validation": tally, "contract_passed": sum(1 for r in results if r["passed"] is True),
                   "contract_failed": sum(1 for r in results if r["passed"] is False)},
        "retries": {"model_calls": sum(1 for c in calls if c["attempt"] > 1), "task_attempts": int(task["attempts"] or 0),
                    "worker_takeovers": int(task["takeovers"] or 0), "person_retries": int(retry_events)},
        "human_corrections": {"approvals": [{"action": a["action"], "status": a["status"]} for a in approvals
                                            if a["status"] != "pending"],
                              "retries": int(retry_events)},
        "cost_usd": round(cost, 6),
        "tokens": {"input": sum(c["input_tokens"] or 0 for c in calls),
                   "output": sum(c["output_tokens"] or 0 for c in calls)},
        "active_seconds": float(task.get("active_seconds") or 0),
        "accepted_artifact": artifact if task["status"] == "done" and met(results) else None,
        "artifact": artifact,
        "rollback_point": {
            "changed": mutating,
            "dataset_versions_read": read_versions,
            "how": ("nothing was changed: the answer is reproducible from the dataset versions read"
                    if not mutating else "each change is versioned; restore the version before this run's "
                                         f"{', '.join(mutating)} from its history"),
        },
        "error": task.get("error"),
    }


def _record_failure(runtime, task, contract, run, failure, result, vchecks) -> None:
    db = runtime.db
    ws = task["workspace_id"]
    hclass = failure["class"]
    fix = FIX_FOR.get(hclass)
    cap = failure.get("capability")
    signature = f"{hclass}:{cap or '-'}:{_sig(failure.get('evidence') or '')}"
    fid = "hf_" + uuid.uuid4().hex[:12]
    db.execute("INSERT INTO harness_failures (id, workspace_id, task_id, run, job_key, failure_class, fix_kind, "
               "capability, signature, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               [fid, ws, task["id"], run, contract["job"], hclass, fix, cap, signature,
                (failure.get("evidence") or "")[:1000]])
    runtime.event(task["id"], "harness_failure", f"Failure class: {hclass.replace('_', ' ')}",
                  {"class": hclass, "fix": fix, "capability": cap})
    if not fix:
        return
    threshold = max(1, int(os.environ.get("LYST_HARNESS_PROPOSE_AFTER", "2")))
    # a person's rejection of a step is one signal enough: the fix (ask first) is the safe one
    need = 1 if hclass == "unsafe_action" else threshold
    seen = db.scalar("SELECT count(*) FROM harness_failures WHERE workspace_id = ? AND job_key = ? AND signature = ?",
                     [ws, contract["job"], signature]) or 0
    if seen < need:
        return
    if fix in ("tool", "permission") and not cap:
        return  # nothing to route away from or gate
    existing = db.query("SELECT id, status FROM harness_rules WHERE workspace_id = ? AND job_key = ? AND kind = ? "
                        "AND signature = ? AND status IN ('proposed', 'active', 'rejected')",
                        [ws, contract["job"], fix, signature])
    if existing:
        db.execute("UPDATE harness_failures SET rule_id = ? WHERE id = ?", [existing[0]["id"], fid])
        return  # already proposed, in force, or a person said no: never re-proposed
    text, spec = _draft(fix, failure, contract, result, vchecks)
    rid = "hr_" + uuid.uuid4().hex[:12]
    sources = [r["id"] for r in db.query("SELECT id FROM harness_failures WHERE workspace_id = ? AND job_key = ? "
                                         "AND signature = ? ORDER BY created_at", [ws, contract["job"], signature])]
    aid = runtime.request_approval(ws, task["created_by"], "harness.update",
                                   f"Harness {fix} rule for \"{contract['goal'][:80]}\": {text[:200]}",
                                   {"rule_id": rid, "kind": fix, "class": hclass}, None)
    db.execute("INSERT INTO harness_rules (id, workspace_id, kind, job_key, capability, signature, text, spec, status, "
               "source_failures, approval_id, proposed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?)",
               [rid, ws, fix, contract["job"], cap, signature, text, json.dumps(spec), json.dumps(sources), aid,
                task["created_by"]])
    db.execute(f"UPDATE harness_failures SET rule_id = ? WHERE id IN ({', '.join('?' * len(sources))})",
               [rid, *sources])
    runtime.event(task["id"], "harness_proposal", f"Proposed a {fix} rule for approval", {"rule_id": rid})
    try:
        from .notices import raise_notice  # noqa: PLC0415

        raise_notice(runtime.stores, ws, source="harness", kind="harness_proposal",
                     label=f"Harness {fix} rule proposed", summary=text[:400], priority=0.5,
                     dedupe_key=f"harness:{rid}")
    except Exception:  # noqa: BLE001,S110 - a notice never stops the record
        pass


# -- durable state ----------------------------------------------------------------------------------------------------


def state(runtime, task_id: str) -> dict[str, Any]:
    """The state of the work, rebuilt from records: what a new session (or a person) needs to continue it."""
    db = runtime.db
    t = db.query("SELECT * FROM tasks WHERE id = ?", [task_id])
    if not t:
        raise KeyError(task_id)
    task = t[0]
    steps = db.query("SELECT seq, capability, status, error, started_at, finished_at FROM task_steps "
                     "WHERE task_id = ? ORDER BY seq", [task_id])
    plan = _loads(task["plan"]) or {}
    planned = 3 + len(plan.get("steps") or [])
    current = next((s for s in steps if s["status"] == "running"), None)
    finished = [s for s in steps if s["status"] in ("done", "skipped")]
    nxt = len(finished) if task["status"] not in ("done", "failed", "cancelled") else None
    contract = contract_of(db, task_id)
    pending = db.query("SELECT id, action, summary, created_at FROM approvals WHERE task_id = ? AND status = 'pending'",
                       [task_id])
    decided = db.query("SELECT action, status, decided_by, decided_at FROM approvals WHERE task_id = ? AND "
                       "status <> 'pending' ORDER BY decided_at", [task_id])
    failures = [{"step": s["capability"], "seq": s["seq"], "error": s["error"], "status": s["status"]}
                for s in steps if s["status"] in ("failed", "skipped") and s["error"]]
    failures += [{"class": f["failure_class"], "capability": f["capability"], "evidence": f["evidence"],
                  "fix": f["fix_kind"], "rule_id": f["rule_id"], "run": f["run"]}
                 for f in db.query("SELECT * FROM harness_failures WHERE task_id = ? ORDER BY created_at", [task_id])]
    receipts = db.query("SELECT run, state, contract_met, created_at FROM task_receipts WHERE task_id = ? ORDER BY run",
                        [task_id])
    last = db.query("SELECT receipt FROM task_receipts WHERE task_id = ? ORDER BY run DESC LIMIT 1", [task_id])
    last_receipt = _loads(last[0]["receipt"]) if last else None
    cons = (contract or {}).get("constraints") or {}
    return {
        "task_id": task_id,
        "status": task["status"],
        "current_step": current["capability"] if current else (task["status"] if nxt is None else "next step"),
        "steps": {"finished": len(finished), "planned": planned if task["plan"] else None,
                  "failed": sum(1 for s in steps if s["status"] == "failed")},
        "contract": contract,
        "artifacts": ([last_receipt["artifact"]] if last_receipt and last_receipt.get("artifact") else []),
        "decisions": [{"approval": d["action"], "status": d["status"], "by": d["decided_by"], "at": d["decided_at"]}
                      for d in decided] + list(cons.get("decisions") or []),
        "failures": failures,
        "pending": [{"approval_id": p["id"], "action": p["action"], "summary": p["summary"]} for p in pending]
                   + ([{"waiting": "a person's approval"}] if task["status"] == "awaiting_approval" and not pending else []),
        "budgets": {"steps": {"used": len(finished), "limit": task["max_steps"]},
                    "seconds": {"used": round(float(task["active_seconds"] or 0), 1), "limit": task["budget_seconds"]},
                    "usd": {"used": round(task_cost(db, task_id), 6), "limit": cons.get("budget_usd")},
                    "retries": {"used": int(db.scalar("SELECT count(*) FROM task_events WHERE task_id = ? AND "
                                                      "kind = 'retry'", [task_id]) or 0),
                                "limit": cons.get("max_retries")}},
        "resume": {
            "resumable": task["status"] in ("queued", "running", "paused", "awaiting_approval", "failed"),
            "from_step": nxt,
            "how": {"paused": "resume it; finished steps are kept and not re-run",
                    "failed": "retry it; finished steps are kept and the failed step runs again",
                    "awaiting_approval": "decide the pending approval; the task continues from the gated step",
                    "running": "if the worker stops, another worker takes over from the last finished step",
                    "queued": "a worker will start it"}.get(task["status"], "finished; ask again to run it anew"),
        },
        "receipts": receipts,
    }


# -- the service: rules a person approves, edits and retires; the workspace's harness report --------------------------


CHECKLIST = (
    ("success_defined", "Is success defined before execution begins?"),
    ("knowledge_map", "Can the agent find the right project knowledge without loading everything?"),
    ("tool_contracts", "Does every tool have a clear contract and failure state?"),
    ("isolated", "Is execution isolated from production systems?"),
    ("decisions_stored", "Are important decisions stored outside the conversation?"),
    ("evidence", "Does every risky transition have evidence?"),
    ("approvals", "Are irreversible actions protected by approval?"),
    ("bounded_loops", "Does every loop have a retry cap and budget?"),
    ("resumable", "Can the run resume after interruption?"),
    ("explainable", "Can you explain every tool call and state change?"),
    ("failure_upgrades", "Does failure update a guide, test, tool, or policy?"),
    ("rollback", "Can the final artifact be rolled back?"),
)


class HarnessService:
    def __init__(self, stores, guard, runtime, memory=None) -> None:
        self.stores = stores
        self.guard = guard
        self.runtime = runtime
        self.memory = memory
        self.db = stores.control

    # -- per task ---------------------------------------------------------------------------------------------------

    def _task(self, actor, task_id: str) -> dict[str, Any]:
        t = self.db.query("SELECT id, workspace_id FROM tasks WHERE id = ?", [task_id])
        if not t:
            raise KeyError(task_id)
        self.guard.require(actor, t[0]["workspace_id"], "workspace.read", task_id)
        return t[0]

    def contract(self, actor, task_id: str) -> dict[str, Any]:
        """The task's contract, and (once a run has ended) how each ``done_when`` check came out."""
        self._task(actor, task_id)
        c = contract_of(self.db, task_id)
        if c is None:
            raise KeyError(f"{task_id} has no contract (submitted before contracts existed)")
        last = self.db.query("SELECT receipt FROM task_receipts WHERE task_id = ? ORDER BY run DESC LIMIT 1", [task_id])
        r = _loads(last[0]["receipt"]) if last else None
        return {"task_id": task_id, "contract": c, "checks": (r or {}).get("contract", {}).get("checks"),
                "met": (r or {}).get("contract", {}).get("met")}

    def receipts(self, actor, task_id: str) -> list[dict[str, Any]]:
        self._task(actor, task_id)
        rows = self.db.query("SELECT run, state, contract_met, receipt, sha256, created_at FROM task_receipts "
                             "WHERE task_id = ? ORDER BY run", [task_id])
        return [{**_loads(r["receipt"]), "sha256": r["sha256"], "recorded_at": r["created_at"]} for r in rows]

    def state(self, actor, task_id: str) -> dict[str, Any]:
        self._task(actor, task_id)
        return state(self.runtime, task_id)

    # -- per workspace ----------------------------------------------------------------------------------------------

    def failures(self, actor, ws: str, limit: int = 100) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "audit.read")
        return self.db.query("SELECT * FROM harness_failures WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?",
                             [ws, limit])

    def rules(self, actor, ws: str, status: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        if status and status not in RULE_STATUS:
            raise ValueError(f"status is one of {', '.join(RULE_STATUS)}")
        rows = self.db.query("SELECT * FROM harness_rules WHERE workspace_id = ? AND (? IS NULL OR status = ?) "
                             "ORDER BY created_at DESC", [ws, status, status])
        for r in rows:
            r["spec"] = _loads(r["spec"]) or {}
            r["source_failures"] = _loads(r["source_failures"]) or []
        return rows

    def _rule(self, ws: str, rule_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM harness_rules WHERE workspace_id = ? AND id = ?", [ws, rule_id])
        if not rows:
            raise KeyError(rule_id)
        return rows[0]

    def edit(self, actor, ws: str, rule_id: str, text: str) -> dict[str, Any]:
        """Reword a proposed rule before deciding it (a guide or decision is stored as written)."""
        self.guard.require(actor, ws, "approval.decide", rule_id)
        r = self._rule(ws, rule_id)
        if r["status"] != "proposed":
            raise ValueError(f"the rule is {r['status']}; only a proposed rule can be edited")
        text = " ".join((text or "").split())
        if not 8 <= len(text) <= 1000:
            raise ValueError("the rule text is 8 to 1000 characters")
        self.db.execute("UPDATE harness_rules SET text = ? WHERE id = ?", [text, rule_id])
        self.guard.audit(actor, ws, "harness.edit", rule_id, "ok")
        return {"id": rule_id, "text": text}

    def on_decided(self, actor, ws: str, rule_id: str, approve: bool) -> None:
        """Called by the runtime when a person decides a harness.update approval. Approving makes the rule active for
        every later run of its job; a guide or decision is also stored in workspace memory, where context recalls it."""
        r = self._rule(ws, rule_id)
        if r["status"] != "proposed":
            return
        mid = None
        if approve and r["kind"] in ("guide", "state") and self.memory is not None:
            spec = _loads(r["spec"]) or {}
            mid = self.memory.remember(actor, ws, spec.get("layer") or ("procedural" if r["kind"] == "guide" else "team"),
                                       r["text"], provenance={"source": "harness", "rule_id": rule_id,
                                                              "failures": _loads(r["source_failures"])},
                                       trust="approved")
        self.db.execute("UPDATE harness_rules SET status = ?, decided_by = ?, decided_at = now(), memory_id = ? "
                        "WHERE id = ?", ["active" if approve else "rejected", actor.user_id, mid, rule_id])
        self.guard.audit(actor, ws, "harness.decide", rule_id, "active" if approve else "rejected")

    def retire(self, actor, ws: str, rule_id: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "approval.decide", rule_id)
        r = self._rule(ws, rule_id)
        if r["status"] != "active":
            raise ValueError(f"the rule is {r['status']}; only an active rule can be retired")
        if r["memory_id"] and self.memory is not None:
            self.memory.revoke(actor, ws, r["memory_id"])
        self.db.execute("UPDATE harness_rules SET status = 'retired', retired_by = ?, retired_at = now() WHERE id = ?",
                        [actor.user_id, rule_id])
        self.guard.audit(actor, ws, "harness.retire", rule_id, "ok")
        return {"id": rule_id, "status": "retired"}

    def report(self, actor, ws: str, days: int = 30) -> dict[str, Any]:
        """The harness checklist answered for this workspace from its own records: yes, partly or no, with the
        evidence counted and where the mechanism lives. Nothing is claimed that the records do not show."""
        self.guard.require(actor, ws, "audit.read")
        db = self.db
        since = f"now() - INTERVAL {max(1, int(days))} DAY"
        n_tasks = db.scalar(f"SELECT count(*) FROM tasks WHERE workspace_id = ? AND created_at > {since}", [ws]) or 0
        n_contract = db.scalar(f"SELECT count(*) FROM tasks t JOIN task_contracts c ON c.task_id = t.id WHERE "
                               f"t.workspace_id = ? AND t.created_at > {since}", [ws]) or 0
        ended = db.scalar(f"SELECT count(*) FROM tasks WHERE workspace_id = ? AND status IN ('done', 'failed', "
                          f"'cancelled') AND created_at > {since}", [ws]) or 0
        receipted = db.scalar(f"SELECT count(DISTINCT r.task_id) FROM task_receipts r JOIN tasks t ON t.id = r.task_id "
                              f"WHERE t.workspace_id = ? AND t.created_at > {since}", [ws]) or 0
        missed = db.scalar(f"SELECT count(*) FROM task_receipts WHERE workspace_id = ? AND contract_met = false AND "
                           f"created_at > {since}", [ws]) or 0
        fails = db.query(f"SELECT failure_class, count(*) AS n FROM harness_failures WHERE workspace_id = ? AND "
                         f"created_at > {since} GROUP BY 1 ORDER BY 2 DESC", [ws])
        rules = {r["status"]: r["n"] for r in db.query("SELECT status, count(*) AS n FROM harness_rules WHERE "
                                                       "workspace_id = ? GROUP BY 1", [ws])}
        gated = db.scalar(f"SELECT count(*) FROM approvals WHERE workspace_id = ? AND action LIKE 'step:%' AND "
                          f"created_at > {since}", [ws]) or 0
        recovered = db.scalar(f"SELECT count(*) FROM task_events e JOIN tasks t ON t.id = e.task_id WHERE "
                              f"t.workspace_id = ? AND e.kind IN ('recovered', 'resumed', 'retry') AND "
                              f"e.occurred_at > {since}", [ws]) or 0
        mem = 0
        try:
            mem = self.stores.workspace(ws).scalar("SELECT count(*) FROM memory WHERE revoked_at IS NULL") or 0
        except Exception:  # noqa: BLE001 - an unreadable workspace file reads as no memory
            mem = 0

        def pct(a, b):
            return f"{a} of {b}" if b else "no tasks yet"

        def ans(ok, partly=False):
            return "yes" if ok else ("partly" if partly else "no")

        items = {
            "success_defined": (ans(n_tasks == 0 or n_contract == n_tasks, n_contract > 0),
                                f"{pct(n_contract, n_tasks)} tasks have a contract; {missed} runs missed theirs",
                                "src/lyst/harness.py build_contract, check_contract"),
            "knowledge_map": ("yes", "context selects the metric definitions, up to 5 memory entries and 3 document "
                                     "passages the question needs; AGENTS.md maps the code for coding agents",
                              "src/lyst/analyst/engine.py contextualize; AGENTS.md"),
            "tool_contracts": ("yes", "each capability declares its permission, phase, approval need and whether it "
                                      "changes anything; failures are classified permission, input, data, model or "
                                      "transient", "src/lyst/analyst/registry.py Capability; runtime classify_failure"),
            "isolated": ("partly", "queries run read-only through the SQL guard and the sandbox; connectors write only "
                                   "through approved actions. There is no separate production copy of the data",
                         "src/lyst/data/sqlguard.py, src/lyst/sandbox.py"),
            "decisions_stored": (ans(mem > 0 or rules.get("active", 0) > 0, True),
                                 f"{mem} memory entries, {rules.get('active', 0)} active harness rules, every approval "
                                 "kept", "src/lyst/memory.py, harness_rules, approvals"),
            "evidence": ("yes", "an answer is complete only when its validation checks pass; the evidence graph traces "
                                "each figure to its query and dataset version", "src/lyst/evidence.py, engine validate"),
            "approvals": ("yes", f"{gated} steps waited for a person in the last {days} days; a closed graph lane never "
                                 "opens without approval", "runtime _approval_gate; docs/GRAPH_RUNS.md"),
            "bounded_loops": ("yes", "every task has step, time and cost budgets and a retry cap; steps retry a "
                                     "transient failure once; graph branches stop after max attempts",
                              "runtime _run_task, retry; harness default_budget_usd"),
            "resumable": ("yes", f"{recovered} resumes, retries or recoveries in the last {days} days; finished steps "
                                 "are never re-run", "runtime checkpoints, recover_orphans; GET /v1/tasks/{id}/state"),
            "explainable": (ans(ended == 0 or receipted >= ended, receipted > 0),
                            f"{pct(receipted, ended)} ended tasks have a receipt; every step, model call and query is "
                            "in the task trace", "GET /v1/tasks/{id}/trace, /receipts"),
            "failure_upgrades": (ans(rules.get("active", 0) > 0, bool(fails)),
                                 f"{sum(f['n'] for f in fails)} classified failures; rules: "
                                 + (", ".join(f"{v} {k}" for k, v in sorted(rules.items())) or "none yet"),
                                 "harness record_run, harness_rules"),
            "rollback": ("partly", "datasets, definitions and published pages are versioned and each receipt names the "
                                   "versions read and what changed; restoring is a person's step, not automatic",
                         "receipt rollback_point"),
        }
        return {
            "workspace_id": ws,
            "days": days,
            "checklist": [{"id": k, "question": q, "answer": items[k][0], "evidence": items[k][1],
                           "where": items[k][2]} for k, q in CHECKLIST],
            "failures_by_class": {f["failure_class"]: f["n"] for f in fails},
            "rules": rules,
            "contracts_missed": missed,
        }
