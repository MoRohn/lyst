# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Specialization maturity, computed from what is actually installed and what has actually been demonstrated.

- Level 1, Defined: the profile validates (``catalog.load`` refuses anything less).
- Level 2, Equipped: every *required* shared capability resolves and is ``available``. A required capability that is
  only ``partial`` keeps the profile at Level 1 and is named, so "equipped" never means "half-equipped".
- Level 3, Operational: representative tasks for the profile have a recorded passing run (``evidence.json``).
- Level 4, Validated: recorded domain benchmarks *and* adversarial cases passed.
- Level 5, Production qualified: a recorded release sign-off for a stated deployment scope.

Levels 3 to 5 are read from ``evidence.json`` and only count if the profile is also Level 2 now: evidence recorded
before a capability was removed does not keep the level.
"""

from __future__ import annotations

import json
import logging
from functools import cache
from importlib import resources
from typing import Any

from .library import LIBRARY, availability
from .schema import Maturity, SpecializationProfile


@cache
def evidence() -> dict[str, dict[str, Any]]:
    f = resources.files("lyst.specialists").joinpath("evidence.json")
    try:
        doc = json.loads(f.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:  # truncated or invalid: assess without evidence rather than fail every call
        logging.getLogger("lyst.specialists").warning("evidence.json is unreadable (%s); no specialization is "
                                                      "assessed above Equipped until it is fixed", exc)
        return {}
    if not isinstance(doc, dict):
        logging.getLogger("lyst.specialists").warning("evidence.json is not an object; ignoring it")
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict)}


def assess(p: SpecializationProfile) -> dict[str, Any]:
    av = availability()
    req = {c: av[c]["status"] for c in p.resources.required_capabilities}
    opt = {c: av[c]["status"] for c in p.resources.optional_capabilities}
    missing = sorted(c for c, s in req.items() if s == "missing")
    partial = sorted(c for c, s in req.items() if s == "partial")
    level = Maturity.EQUIPPED if not missing and not partial else Maturity.DEFINED
    ev = evidence().get(p.id, {})
    if level == Maturity.EQUIPPED:
        if ev.get("operational"):
            level = Maturity.OPERATIONAL
            if ev.get("validated"):
                level = Maturity.VALIDATED
                if ev.get("production"):
                    level = Maturity.PRODUCTION
    usable = [c for c, s in {**req, **opt}.items() if s != "missing"]
    blockers = []
    if missing:
        blockers.append("needs " + ", ".join(LIBRARY[c].title.lower() for c in missing) + ", which Lyst does not have yet")
    if partial:
        blockers.append("relies on " + ", ".join(LIBRARY[c].title.lower() for c in partial) + ", which Lyst covers only in part")
    if level == Maturity.EQUIPPED:
        blockers.append("no representative tasks have been run and recorded for it yet")
    return {"level": int(level), "label": level.label, "required": req, "optional": opt, "missing": missing,
            "partial": partial, "usable_capabilities": usable, "next_step": blockers[0] if blockers else None,
            "evidence": ev or None}


def summary(profiles) -> dict[str, Any]:
    levels: dict[int, int] = {i: 0 for i in range(1, 6)}
    gaps: dict[str, int] = {}
    for p in profiles:
        a = assess(p)
        levels[a["level"]] += 1
        for c in a["missing"] + a["partial"]:
            gaps[c] = gaps.get(c, 0) + 1
    return {"levels": {Maturity(k).label: v for k, v in levels.items()},
            "capability_gaps": sorted(({"capability": k, "title": LIBRARY[k].title, "status": availability()[k]["status"],
                                        "profiles_blocked": v} for k, v in gaps.items()), key=lambda g: -g["profiles_blocked"])}
