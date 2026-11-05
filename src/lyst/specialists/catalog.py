# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The specialization catalog: 136 profiles across 11 domains, loaded once, validated, cached and versioned.

Each ``catalog/<letter>_<domain>.json`` file holds one domain: its shared defaults (the parts of a profile that are
common to the discipline family, such as its standards, its data-handling rules and the checks every result in the
family needs) and its roles (what is particular to each specialization). Loading merges the two into complete
``SpecializationProfile`` objects and then checks the catalog as a whole: unique ids, related ids that exist, and
every capability, verification rule and permission a profile names must exist in Lyst. A catalog that fails any of
these does not load.

The catalog is general professional knowledge. Nothing in it is a fact about any organization; workspace-approved
knowledge lives in the semantic layer, glossary and documents, and outranks it (see ``assembly.KNOWLEDGE_ORDER``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from ..security import PERMISSIONS
from .library import LIBRARY
from .schema import SpecializationProfile
from .verification import RULES

# short keys used in the role entries -> (section, field) in the profile
SHORT: dict[str, tuple[str, str]] = {
    "name": ("identity", "name"), "desc": ("identity", "description"), "alt": ("identity", "alternative_titles"),
    "sub": ("identity", "subdomain"), "kind": ("identity", "title_kind"),
    "resp": ("expertise", "responsibilities"), "know": ("expertise", "knowledge_domains"),
    "terms": ("expertise", "terminology"), "methods": ("expertise", "methodologies"),
    "concepts": ("expertise", "technical_concepts"),
    "tasks": ("analytics", "task_classes"), "techniques": ("analytics", "investigation_techniques"),
    "req": ("resources", "required_capabilities"), "opt": ("resources", "optional_capabilities"),
    "sources": ("data", "source_systems"), "entities": ("data", "entities"), "fields": ("data", "required_fields"),
    "verify": ("execution", "verification_rules"), "outputs": ("execution", "outputs"),
    "depth": ("execution", "planning_depth"),
    "limits": ("quality", "known_limitations"), "examples": ("quality", "example_requests"),
    "sensitivity": ("governance", "sensitivity"), "perms": ("governance", "required_permissions"),
}
# fields where a role adds to its domain's defaults instead of replacing them
UNION = {("execution", "verification_rules"), ("quality", "known_limitations"), ("governance", "required_permissions"),
         ("expertise", "technical_concepts")}
# true for every profile in the catalog
UNIVERSAL = {
    ("execution", "verification_rules"): ["numbers_backed", "access_scope"],
    ("quality", "known_limitations"): [
        "The profile is general professional practice; it is not this organization's policy or a substitute for its experts."],
    ("governance", "required_permissions"): ["analyst.ask"],
}
SECTIONS = ("identity", "expertise", "analytics", "data", "resources", "execution", "quality", "governance", "provenance")
DOMAIN_LETTERS = "ABCDEFGHIJK"


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Domain:
    id: str
    letter: str
    title: str
    summary: str


def _merge(a: list, b: list) -> list:
    seen, out = set(), []
    for x in [*a, *b]:
        k = x.lower() if isinstance(x, str) else x
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def build_profile(domain: dict[str, Any], role: dict[str, Any], *, origin: str = "catalog", owner: str = "lyst") -> SpecializationProfile:
    """Merge a domain's defaults with one role entry into a validated profile."""
    body: dict[str, dict[str, Any]] = {s: {} for s in SECTIONS}
    for key, value in (domain.get("defaults") or {}).items():
        section, _, field = key.partition(".")
        body[section][field] = value
    for (section, field), value in UNIVERSAL.items():
        body[section][field] = _merge(value, body[section].get(field, []))
    extra: dict[str, Any] = {}
    for key, value in role.items():
        if key in ("id", "related", "keywords"):
            extra[key] = value
            continue
        if key in SHORT:
            section, field = SHORT[key]
        elif "." in key:
            section, _, field = key.partition(".")
        else:
            raise CatalogError(f"{role.get('id')}: unknown field {key!r}")
        if (section, field) in UNION:
            body[section][field] = _merge(body[section].get(field, []), value)
        else:
            body[section][field] = value
    body["identity"].update(id=role["id"], domain=domain["id"])
    body["provenance"].setdefault("origin", origin)
    body["provenance"].setdefault("owner", owner)
    body["provenance"].setdefault("revisions", ["1.0.0: initial catalog definition"])
    return SpecializationProfile(**body, related=extra.get("related", []), keywords=extra.get("keywords", []))


@dataclass(frozen=True)
class Catalog:
    profiles: dict[str, SpecializationProfile]
    domains: dict[str, Domain]
    order: tuple[str, ...]
    version: str

    def get(self, sid: str) -> SpecializationProfile:
        try:
            return self.profiles[sid]
        except KeyError:
            raise KeyError(f"no specialization named {sid}") from None

    def in_domain(self, domain_id: str) -> list[SpecializationProfile]:
        return [self.profiles[i] for i in self.order if self.profiles[i].identity.domain == domain_id]


def check_references(p: SpecializationProfile, known_ids: set[str] | None = None) -> list[str]:
    """Problems with what a profile points at: shared-library capabilities, verification rules, permissions, relations."""
    problems = []
    for cap in [*p.resources.required_capabilities, *p.resources.optional_capabilities]:
        if cap not in LIBRARY:
            problems.append(f"{p.id}: unknown capability {cap}")
    for rule in p.execution.verification_rules:
        if rule not in RULES:
            problems.append(f"{p.id}: unknown verification rule {rule}")
    for perm in p.governance.required_permissions:
        if perm not in PERMISSIONS:
            problems.append(f"{p.id}: unknown permission {perm}")
    if known_ids is not None:
        for rel in p.related:
            if rel not in known_ids:
                problems.append(f"{p.id}: related id {rel} does not exist")
    return problems


_LOAD_FAILURE: list[CatalogError] = []


def load() -> Catalog:
    """The packaged catalog, read and validated once. A failure is remembered too, so a bad catalog raises the same
    error on every call without re-reading and re-hashing every file."""
    if _LOAD_FAILURE:
        raise _LOAD_FAILURE[0]
    try:
        return _load()
    except CatalogError as exc:
        _LOAD_FAILURE.append(exc)
        raise


@cache
def _load() -> Catalog:
    files = sorted(f for f in resources.files("lyst.specialists").joinpath("catalog").iterdir() if f.name.endswith(".json"))
    digest = hashlib.sha256()
    profiles: dict[str, SpecializationProfile] = {}
    domains: dict[str, Domain] = {}
    order: list[str] = []
    for f in files:
        raw = f.read_bytes()
        digest.update(raw)
        doc = json.loads(raw)
        d = doc["domain"]
        if d["letter"] not in DOMAIN_LETTERS or d["id"] in domains:
            raise CatalogError(f"bad or duplicate domain {d['id']}")
        domains[d["id"]] = Domain(d["id"], d["letter"], d["title"], d["summary"])
        for role in doc["roles"]:
            try:
                p = build_profile(d, role)
            except Exception as e:  # noqa: BLE001 - report which role failed
                raise CatalogError(f"{role.get('id')}: {e}") from e
            if p.id in profiles:
                raise CatalogError(f"duplicate specialization id {p.id}")
            profiles[p.id] = p
            order.append(p.id)
    problems = [x for p in profiles.values() for x in check_references(p, set(profiles))]
    if problems:
        raise CatalogError("; ".join(problems[:20]))
    return Catalog(profiles, domains, tuple(order), digest.hexdigest()[:16])
