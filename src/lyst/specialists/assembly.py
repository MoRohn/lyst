# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Expertise assembly: the selected disciplines composed into one execution profile.

A composite request ("why did cloud cost rise while growth slowed?") does not start one agent per discipline. The
selected profiles are merged into a single assembly that the one analyst runs with: one objective, explicit
responsibilities per discipline, the union of their methods, only the capabilities the actor may use, every
verification rule any of them requires (automated ones are enforced, declared ones are disclosed as unchecked), and
the strictest approval and sensitivity of the set. Independent review is a separate, explicit flag, not a second
copy of the same work.

``brief`` is the only part of an assembly that may reach a model, it is small (a few hundred tokens whatever the
number of disciplines) and it is wrapped as untrusted data: profile text is reference material, never instructions.
"""

from __future__ import annotations

from typing import Any

from ..security import untrusted
from .catalog import Catalog, load
from .library import LIBRARY, availability
from .router import RoutingDecision
from .verification import RULES

# Which knowledge wins when sources disagree (1 is weakest). Catalog profiles are levels 1-3; the workspace's own
# approved definitions, glossary and documents (4-5) and the evidence computed in the task (6) always outrank them.
KNOWLEDGE_ORDER = ["universal analytical knowledge", "cross-domain methodology", "specialization expertise",
                   "industry context", "organization-approved knowledge", "task evidence"]

_SENS = {"standard": 0, "confidential": 1, "restricted": 2}
BRIEF_LIMIT = 1800  # characters


def _uniq(items, limit: int | None = None) -> list:
    out, seen = [], set()
    for x in items:
        k = x.lower() if isinstance(x, str) else x
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out[:limit] if limit else out


def assemble(decision: RoutingDecision, objective: str, catalog: Catalog | None = None) -> dict[str, Any]:
    cat = catalog or load()
    av = availability()
    if decision.general or not decision.selected:
        return {"general": True, "objective": objective, "roles": [], "brief": "",
                "note": "No specialized discipline was needed; Lyst answers as a general analyst.",
                "knowledge_order": KNOWLEDGE_ORDER}
    profiles = [cat.get(c.id) for c in decision.selected]
    roles = []
    for c, p in zip(decision.selected, profiles, strict=True):
        roles.append({"id": p.id, "name": p.name, "domain": p.identity.domain, "level": c.level,
                      "responsibilities": p.expertise.responsibilities[:3], "why": c.reasons[:3],
                      "requested": c.requested, "authorized": c.authorized, "blocked_permissions": c.blocked_permissions})
    caps: dict[str, set[str]] = {}
    for p in profiles:
        for cap in [*p.resources.required_capabilities, *p.resources.optional_capabilities]:
            caps.setdefault(cap, set()).add(p.id)
    blocked_perms = {perm for c in decision.selected for perm in c.blocked_permissions}
    usable, partial, missing, not_permitted = [], [], [], []
    for cap, by in caps.items():
        lib, status = LIBRARY[cap], av[cap]["status"]
        row = {"id": cap, "title": lib.title, "for": sorted(by)}
        if status == "missing":
            missing.append(row | {"limitation": lib.limitations[0]})
        elif lib.permission in blocked_perms:
            not_permitted.append(row | {"permission": lib.permission})
        elif status == "partial":
            partial.append(row | {"coverage": lib.coverage})
        else:
            usable.append(row)
    rules = _uniq(r for p in profiles for r in p.execution.verification_rules)
    if decision.independent_review:
        rules = _uniq([*rules, "human_review"])
    verification = {"automated": [{"id": r, "title": RULES[r].title, "enforced_by": RULES[r].enforced_by} for r in rules if RULES[r].automated],
                    "not_yet_automated": [{"id": r, "title": RULES[r].title} for r in rules if not RULES[r].automated]}
    sensitivity = max((p.governance.sensitivity for p in profiles), key=lambda s: _SENS[s])
    depth = max((p.execution.planning_depth for p in profiles), key=["shallow", "standard", "deep"].index)
    limitations = _uniq([lim for p in profiles for lim in p.quality.known_limitations[1:]]
                        + [m["limitation"] for m in missing]
                        + [f"{u['description']} Lyst has no capability for this yet." for u in decision.unsupported])
    out = {
        "general": False,
        "objective": objective,
        "roles": roles,
        "primary": roles[0]["id"],
        "composite": len(roles) > 1,
        "execution": "single" if not decision.independent_review else "single_with_independent_review",
        "task_classes": decision.request_task_classes,
        "methods": _uniq((m for p in profiles for m in p.expertise.methodologies), 10),
        "capabilities": {"usable": usable, "partial": partial, "missing": missing, "not_permitted": not_permitted},
        "verification": verification,
        "approvals": _uniq((a for p in profiles for a in p.execution.approval_conditions), 6),
        "outputs": _uniq((o for p in profiles for o in p.execution.outputs), 6),
        "planning_depth": depth,
        "sensitivity": sensitivity,
        "sensitive_handling": _uniq((h for p in profiles for h in p.governance.sensitive_handling), 6),
        "limitations": limitations[:8],
        "unsupported": decision.unsupported,
        "knowledge_order": KNOWLEDGE_ORDER,
        "catalog_version": decision.catalog_version,
    }
    out["brief"] = brief(out, profiles)
    return out


def brief(assembly: dict[str, Any], profiles) -> str:
    """The compact, model-facing summary of the assembled expertise, fenced as untrusted reference data."""
    lines = [f"Disciplines: {', '.join(r['name'] for r in assembly['roles'])}."]
    for p in profiles:
        lines.append(f"- {p.name}: {'; '.join(p.expertise.responsibilities[:2])}. Methods: {', '.join(p.expertise.methodologies[:3])}.")
    if assembly["verification"]["automated"] or assembly["verification"]["not_yet_automated"]:
        lines.append("Checks required: " + ", ".join(r["title"] for r in [*assembly["verification"]["automated"],
                                                                           *assembly["verification"]["not_yet_automated"]][:6]) + ".")
    if assembly["limitations"]:
        lines.append("Known limits: " + " ".join(assembly["limitations"][:2]))
    text = "\n".join(lines)
    if len(text) > BRIEF_LIMIT:
        text = text[: BRIEF_LIMIT - 1].rsplit(" ", 1)[0] + "…"
    return untrusted("specialization_profiles", text)
