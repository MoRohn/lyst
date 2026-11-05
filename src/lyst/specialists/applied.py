# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""How the assembled expertise changes a run: stronger verification, rule-by-rule results, and honest disclosure.

The engine calls three small functions when a task carries ``request["expertise"]`` (tasks routed through the
Universal Analyst Core); tasks without it run exactly as before.

- ``intensity``: raises the ``validate`` step to high intensity when the assembled disciplines require an
  independent recompute or deep planning, so the expertise makes verification stricter, never looser.
- ``evaluate``: maps each verification rule the disciplines require onto the checks ``validate`` actually ran, and
  says for every rule whether it passed, failed, was enforced elsewhere (the SQL guard, delivery), did not apply to
  this answer, or is not checked automatically yet.
- ``disclosure``: what the answer states about it: which disciplines shaped it and which required checks Lyst did
  not run, so a reader is never left to assume a check happened.
"""

from __future__ import annotations

from typing import Any

from .verification import RULES

# verification rule -> names (or name prefixes, ending in ":") of checks the validate capability records
RULE_CHECKS: dict[str, tuple[str, ...]] = {
    "independent_recompute": ("independent_recompute", "recompute"),
    "period_coverage": ("coverage:", "freshness", "source_age"),
    "join_fanout": ("join_fanout",),
    "duplicates_nulls": ("duplicates", "nulls", "duplicate_rows"),
    "bridge_closes": ("bridge_closes:",),
    "parts_reconcile": ("parts_reconcile",),
    "single_currency": ("single_currency",),
    "forecast_backtest": ("forecast_backtest", "forecast_interval"),
    "uncertainty_stated": ("forecast_interval", "forecast_backtest"),
    "hypotheses_decided": ("hypotheses_decided",),
}
# rules enforced outside validate, on every task: they hold whenever the task completed
ELSEWHERE = {"numbers_backed", "access_scope", "contract_conformance", "restricted_data_masked", "causal_claims_guarded",
             "fiscal_period", "lineage_recorded", "conversion_definition", "source_cited", "eval_reproducible", "human_review"}


def _rules(expertise: dict[str, Any] | None) -> list[str]:
    if not expertise or expertise.get("general"):
        return []
    v = expertise.get("verification") or {}
    return [r["id"] for r in [*(v.get("automated") or []), *(v.get("not_yet_automated") or [])] if r.get("id") in RULES]


def intensity(current: str, expertise: dict[str, Any] | None) -> str:
    if current == "high" or not expertise or expertise.get("general"):
        return current
    if "independent_recompute" in _rules(expertise) or expertise.get("planning_depth") == "deep":
        return "high"
    return current


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(name.startswith(p) if p.endswith(":") else name == p for p in patterns)


def evaluate(expertise: dict[str, Any] | None, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for rid in _rules(expertise):
        rule = RULES[rid]
        row = {"id": rid, "title": rule.title}
        if rid in RULE_CHECKS:
            hits = [c for c in checks if _matches(c["name"], RULE_CHECKS[rid])]
            if not hits:
                row |= {"status": "not_applicable", "detail": "this answer did not involve what the check covers"}
            elif all(c["passed"] for c in hits):
                row |= {"status": "passed", "detail": "; ".join(c["detail"] for c in hits[:2])}
            else:
                bad = [c for c in hits if not c["passed"]]
                row |= {"status": "failed", "detail": "; ".join(c["detail"] for c in bad[:2])}
        elif rid in ELSEWHERE and rule.automated:
            row |= {"status": "enforced", "detail": rule.enforced_by}
        else:
            row |= {"status": "not_automated", "detail": rule.description}
        out.append(row)
    return out


def disclosure(expertise: dict[str, Any] | None, rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not expertise or expertise.get("general"):
        return None
    unchecked = [r["title"] for r in rules if r["status"] == "not_automated"]
    failed = [r["title"] for r in rules if r["status"] == "failed"]
    note = None
    if unchecked:
        note = ("This kind of analysis also calls for checks Lyst does not run automatically yet: "
                + ", ".join(t.lower() for t in unchecked) + ". Review those before relying on the result.")
    return {"disciplines": [r["name"] for r in expertise.get("roles") or []],
            "primary": expertise.get("primary"),
            "rules": rules,
            "passed": sum(1 for r in rules if r["status"] in ("passed", "enforced")),
            "failed": failed,
            "not_automated": unchecked,
            "note": note,
            "limitations": list(expertise.get("limitations") or [])[:4]}
