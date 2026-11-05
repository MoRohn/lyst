# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Read models for the Specializations catalog surface: domain browsing, search, a profile's detail and fit."""

from __future__ import annotations

from typing import Any

from . import library, verification
from .catalog import load
from .maturity import assess, summary
from .router import _bm25, _index, domain_affinity, tokens

RELATIVE_FLOOR = 0.45


def row(pid: str) -> dict[str, Any]:
    p = load().get(pid)
    m = assess(p)
    return {"id": p.id, "name": p.name, "domain": p.identity.domain, "subdomain": p.identity.subdomain,
            "title_kind": p.identity.title_kind, "description": p.identity.description,
            "level": m["level"], "level_label": m["label"], "task_classes": list(p.analytics.task_classes),
            "example": p.quality.example_requests[0]}


def domains() -> list[dict[str, Any]]:
    cat = load()
    out = []
    for d in sorted(cat.domains.values(), key=lambda d: d.letter):
        members = cat.in_domain(d.id)
        levels = [assess(p)["level"] for p in members]
        out.append({"id": d.id, "letter": d.letter, "title": d.title, "summary": d.summary, "count": len(members),
                    "equipped": sum(1 for x in levels if x >= 2), "operational": sum(1 for x in levels if x >= 3)})
    return out


def search(q: str | None = None, domain: str | None = None, min_level: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    cat = load()
    ids = list(cat.order)
    if domain:
        ids = [i for i in ids if cat.get(i).identity.domain == domain]
    if q and q.strip():
        ix = _index(cat)
        qt = tokens(q)
        aff = domain_affinity(qt)
        lowered = " " + " ".join(q.lower().split()) + " "

        def score(i: str) -> float:
            p = cat.get(i)
            s = _bm25(ix, qt, i)
            if s <= 0:
                return 0.0
            matched = sum(1 for t in set(qt) if t in ix.docs[i])
            s *= (matched / len(set(qt))) ** 0.5  # matching every word beats matching one
            s += 1.5 * sum(1 for ph in ix.phrases[i] if " " in ph and f" {ph} " in lowered)
            return s * (1 + aff.get(p.identity.domain, 0.0))  # the business vocabulary says which domain the words belong to

        scored = sorted(((i, score(i)) for i in ids), key=lambda x: -x[1])
        top = scored[0][1] if scored else 0.0
        # weak tails (one shared word with an unrelated discipline) are noise in a search box
        ids = [i for i, s in scored if s > 0 and s >= RELATIVE_FLOOR * top]
    rows = [row(i) for i in ids]
    if min_level:
        rows = [r for r in rows if r["level"] >= min_level]
    return rows[:limit]


def detail(pid: str, can=None) -> dict[str, Any]:
    """The full profile with its computed maturity, what each capability really is, and (with ``can``) which of
    its permissions the viewer holds. Viewing a profile never changes what the viewer may do."""
    cat = load()
    p = cat.get(pid)
    m = assess(p)
    av = library.availability()
    caps = [{"id": c, "title": library.LIBRARY[c].title, "required": c in p.resources.required_capabilities,
             "status": av[c]["status"], "coverage": library.LIBRARY[c].coverage, "limitations": list(library.LIBRARY[c].limitations)}
            for c in [*p.resources.required_capabilities, *p.resources.optional_capabilities]]
    checks = [{"id": r, "title": verification.RULES[r].title, "automated": verification.RULES[r].automated,
               "enforced_by": verification.RULES[r].enforced_by} for r in p.execution.verification_rules]
    out = {"profile": p.model_dump(), "maturity": m, "capabilities": caps, "verification": checks,
           "related": [row(r) for r in p.related], "catalog_version": cat.version}
    if can is not None:
        perms = {perm: bool(can(perm)) for perm in p.governance.required_permissions}
        out["access"] = {"permissions": perms, "full_use": all(perms.values()),
                         "note": None if all(perms.values()) else
                         "Your role lets this discipline advise but not run the steps that need: "
                         + ", ".join(k for k, v in perms.items() if not v)}
    return out


def overview() -> dict[str, Any]:
    cat = load()
    return {"catalog_version": cat.version, "profiles": len(cat.profiles), "domains": len(cat.domains),
            **summary(cat.profiles.values()),
            "library": {s: sum(1 for c in library.describe() if c["status"] == s) for s in ("available", "partial", "missing")}}
