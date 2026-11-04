# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The specialization router: which expertise does this request need?

Layers, each recorded in the decision so a person can see why a discipline was chosen:

1. **Task intent**: the request is classified into task classes (``schema.TASK_CLASSES``) by phrase rules, so a
   profile is chosen for the *kind of work* and not only for words that resemble its title.
2. **Candidate retrieval**: BM25 over each profile's name, titles, routing keywords, terminology, description and
   example requests (an in-memory index built once per catalog version; nothing is loaded into a model context).
3. **Domain context**: the workspace's business profile (its approved domain packages) nudges related disciplines.
4. **Tools and data**: the shared capabilities each candidate needs are resolved; fully equipped candidates rank a
   little higher, and what is missing is reported, never hidden.
5. **Permissions**: each candidate's required permissions are checked against the actor's role. Choosing a
   discipline never adds a permission: a candidate the actor cannot fully use is kept as a *perspective* with the
   blocked permissions listed.
6. **Compatibility**: task-class overlap between the request and the candidate.
7. **Cost**: at most ``MAX_SELECTED`` disciplines are assembled, into one execution (see ``assembly.py``); more
   disciplines never mean more model calls.
8. **Selection**: a primary discipline plus complementary ones that add task classes the primary lacks.

A person can ask for a perspective ("as a Cloud FinOps specialist"); the override is a preference, resolved against
the catalog and subject to the same permission check. Profile text is never executed or followed as instructions:
the router only scores it.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from .catalog import Catalog, load
from .library import LIBRARY, availability
from .maturity import assess
from .schema import TASK_CLASSES, SpecializationProfile

MAX_SELECTED = 4
MIN_SCORE = 2.2  # below this the request reads as general analysis, and no discipline is assembled

_TOKEN = re.compile(r"[a-z0-9][a-z0-9&+/.-]*")
_STOP = set("a an the of to for in on and or our we my me i is are was were be been by with as at it its this that these "
            "those what which who how why when where while do does did can could should would will from into than then so any all "
            "us you your please give show tell".split())

TASK_RULES: list[tuple[str, re.Pattern]] = [(k, re.compile(p, re.I)) for k, p in [
    ("variance_explanation", r"\b(why (did|has|is|are|have)|explain\w*|variance|what drove|drivers? of|bridge|versus (budget|plan)|vs\.? (budget|plan)|increased?|decreased?|rose|fell|dropp?ed)\b"),
    ("forecasting", r"\b(forecast\w*|predict\w*|projection|next (month|quarter|year)|outlook|will (be|we))\b"),
    ("trend_analysis", r"\b(trend\w*|over time|seasonal\w*|year over year|month over month|growth rate)\b"),
    ("anomaly_detection", r"\b(anomal\w*|unusual|spike\w*|outlier\w*|suspicious|abnormal|drift\w*)\b"),
    ("root_cause", r"\b(root cause|caus(e|ed|ing) (the|of)|what went wrong|failures?|incidents?|outage)\b"),
    ("data_quality", r"\b(data quality|nulls?|duplicates?|missing (values|data)|completeness|validity|stale|freshness)\b"),
    ("data_profiling", r"\b(profile|profiling|describe (the|this) (dataset|table)|columns? in)\b"),
    ("data_modeling", r"\b(data model\w*|schema design|grain|star schema|ontology|entity model)\b"),
    ("sql_authoring", r"\b(sql|query|queries)\b"),
    ("statistical_testing", r"\b(significan\w*|hypothes\w*|p-?value|statistical\w*|confidence interval|t-?test|correlat\w*)\b"),
    ("experiment_analysis", r"\b(a/?b test\w*|experiment\w*|variant\w*|control group|holdout|split test)\b"),
    ("causal_estimation", r"\b(causal\w*|effect of|impact of|incremental\w*|counterfactual|did .{0,40} (work|cause))\b"),
    ("optimization", r"\b(optimi[sz]\w*|best (allocation|split|mix)|maximi[sz]e|minimi[sz]e|allocate)\b"),
    ("simulation", r"\b(simulat\w*|monte carlo|what if|scenario\w*|range of outcomes)\b"),
    ("segmentation", r"\b(segment\w*|cohorts?|cluster\w*|personas?|tiers?)\b"),
    ("attribution", r"\b(attribut\w*|which channel|credit for|touchpoints?)\b"),
    ("reconciliation", r"\b(reconcil\w*|tie out|agree with|match(es)? the ledger|two sources|multiple sources)\b"),
    ("cost_analysis", r"\b(costs?|spend\w*|expens\w*|bill\w*|savings|budget)\b"),
    ("capacity_planning", r"\b(capacity|headroom|run out|staffing|how many (agents|people|servers|nodes))\b"),
    ("risk_assessment", r"\b(risks?|exposure|vulnerab\w*|threats?|controls?)\b"),
    ("security_investigation", r"\b(security|suspicious|malicious|breach\w*|intrusion|telemetry|threat\w*|forensic\w*|fraud\w*)\b"),
    ("compliance_review", r"\b(complian\w*|regulat\w*|audit\w*|policy|policies|gdpr|soc 2|wcag|sox)\b"),
    ("code_analysis", r"\b(code|codebase|repository|repo|tests? fail\w*|technical debt|architecture risks?|pull requests?)\b"),
    ("performance_analysis", r"\b(latency|throughput|slow(er)? (quer(y|ies)|requests?|pages?|responses?|endpoints?|jobs?|builds?)|performance|utili[sz]ation|bottleneck\w*|reliab\w*)\b"),
    ("model_evaluation", r"\b(evaluat\w* (the |our |a )?(model|agent|llm|classifier|workflow)|benchmark\w*|accuracy|precision|recall|model performance|tool selection)\b"),
    ("document_extraction", r"\b(extract\w*|pdf|contracts?|invoices?|documents?)\b"),
    ("text_analytics", r"\b(themes?|comments|feedback|tickets? text|sentiment|topics?|mentions)\b"),
    ("geospatial_analysis", r"\b(map|maps|geospatial|location\w*|distance|km|miles|latitude|region map)\b"),
    ("process_analysis", r"\b(process\w*|workflow\w*|cycle time|bottleneck\w*|handoffs?|funnel)\b"),
    ("research_synthesis", r"\b(research|brief(ing)?|summari[sz]e (what|the)|what do (our|the) (documents|reports) say|compare (vendors|tools|options))\b"),
    ("dashboard_design", r"\b(dashboard\w*|scorecard|kpi pack|recurring report)\b"),
    ("recommendation", r"\b(recommend\w*|should we|what should|corrective actions?|next steps?|options?)\b"),
    ("descriptive_reporting", r"\b(report\w*|how (much|many)|what (is|was|were) (our|the)|total|summary)\b"),
]]
assert {k for k, _ in TASK_RULES} <= set(TASK_CLASSES)

# business-profile domain packages (domains/packages) -> catalog domains they make more likely
PACKAGE_DOMAINS = {
    "finance": ["finance_enterprise_ops"], "financial_services": ["finance_enterprise_ops", "security_risk_compliance"],
    "procurement": ["finance_enterprise_ops"], "logistics": ["finance_enterprise_ops", "scientific_industrial"],
    "workforce": ["finance_enterprise_ops"], "sales": ["finance_enterprise_ops", "marketing_customer_commerce"],
    "marketing": ["marketing_customer_commerce", "media_content_information"],
    "customer_service": ["marketing_customer_commerce", "ai_ml_agentic"], "product": ["software_digital_products", "marketing_customer_commerce"],
    "it_operations": ["cloud_infrastructure", "software_digital_products"], "healthcare": ["scientific_industrial"],
    "manufacturing": ["scientific_industrial"], "professional_services": ["data_analytics"],
}

_OVERRIDE = [
    re.compile(r"\b(?:as|like) an? ([a-z0-9&/ .-]{3,60}?) (?:specialist|analyst|expert|would)\b", re.I),
    re.compile(r"\bfrom an? ([a-z0-9&/ .-]{3,60}?) (?:perspective|point of view|standpoint|lens)\b", re.I),
    re.compile(r"\busing an? ([a-z0-9&/ .-]{3,60}?) (?:perspective|lens|approach)\b", re.I),
    re.compile(r"\bhave an? (?:independent )?([a-z0-9&/ .-]{3,60}?) (?:specialist|analyst|expert) (?:review|evaluate|check|assess)\b", re.I),
]
_GENERIC = {"descriptive_reporting", "recommendation", "cost_analysis", "risk_assessment"}
_INDEPENDENT = re.compile(r"\b(independent\w*|second opinion|challenge|red[- ]team|separate reviewer)\b", re.I)


def _stem(t: str) -> str:
    for suf in ("ations", "ation", "ings", "ing", "ies", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def tokens(text: str) -> list[str]:
    return [_stem(t.strip(".-/")) for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t.strip(".-/")) > 1]


@dataclass
class _Index:
    docs: dict[str, Counter]
    lengths: dict[str, int]
    avg: float
    df: Counter
    names: dict[str, Counter]
    phrases: dict[str, list[str]]


def _doc(p: SpecializationProfile) -> Counter:
    weighted = [(p.name, 4), (" ".join(p.identity.alternative_titles), 3), (" ".join(p.keywords), 3),
                (" ".join(p.expertise.terminology), 1), (p.identity.description, 1), (" ".join(p.quality.example_requests), 1),
                (p.identity.subdomain, 2), (" ".join(p.expertise.methodologies), 1)]
    c: Counter = Counter()
    for text, w in weighted:
        for t in tokens(text):
            c[t] += w
    return c


@cache
def _index(version: str) -> _Index:  # keyed on the catalog version so a new catalog rebuilds it
    cat = load()
    docs = {pid: _doc(p) for pid, p in cat.profiles.items()}
    df: Counter = Counter()
    for c in docs.values():
        df.update(set(c))
    lengths = {k: sum(v.values()) for k, v in docs.items()}
    names = {pid: Counter(tokens(" ".join([p.name, *p.identity.alternative_titles]))) for pid, p in cat.profiles.items()}
    phrases = {pid: [re.sub(r"[^a-z0-9&/+-]+", " ", k.lower()).strip() for k in p.keywords if " " in k or len(k) > 3]
               for pid, p in cat.profiles.items()}
    return _Index(docs, lengths, sum(lengths.values()) / len(lengths), df, names, phrases)


def _bm25(ix: _Index, q: list[str], pid: str, k1: float = 1.2, b: float = 0.75) -> float:
    doc, n = ix.docs[pid], len(ix.docs)
    s = 0.0
    for t in set(q):
        f = doc.get(t)
        if not f:
            continue
        idf = math.log(1 + (n - ix.df[t] + 0.5) / (ix.df[t] + 0.5))
        s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * ix.lengths[pid] / ix.avg))
    return s


def task_classes_of(text: str) -> list[str]:
    return [k for k, pat in TASK_RULES if pat.search(text)]


def resolve_name(text: str, catalog: Catalog | None = None) -> str | None:
    """A specialization named in free text ("cloud finops", "statistical research"), or None. Rarer name words
    count for more, so "statistical research" means the statistical discipline, not any research one."""
    cat = catalog or load()
    ix = _index(cat.version)
    q = set(tokens(text)) - {"analyst", "specialist", "expert"}
    if not q:
        return None
    n = len(ix.names)
    name_df = Counter(t for c in ix.names.values() for t in set(c))
    idf = {t: math.log(1 + n / (1 + name_df.get(t, 0))) for t in q}
    total = sum(idf.values())
    best, best_s = None, 0.0
    for pid in cat.order:
        name_toks = ix.names[pid]
        s = sum(idf[t] for t in q if t in name_toks) / total
        s += 0.15 * sum(1 for t in q if t in ix.docs[pid] and t not in name_toks) / len(q)
        if s > best_s:
            best, best_s = pid, s
    return best if best_s >= 0.5 else None


@dataclass
class Candidate:
    id: str
    name: str
    domain: str
    score: float
    retrieval: float
    task_overlap: list[str]
    phrase_hits: list[str]
    level: int
    missing_capabilities: list[str]
    partial_capabilities: list[str]
    blocked_permissions: list[str]
    reasons: list[str] = field(default_factory=list)
    requested: bool = False
    matched: frozenset = frozenset()

    @property
    def authorized(self) -> bool:
        return not self.blocked_permissions

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "domain": self.domain, "score": round(self.score, 3),
                "task_overlap": self.task_overlap, "level": self.level, "missing_capabilities": self.missing_capabilities,
                "partial_capabilities": self.partial_capabilities, "blocked_permissions": self.blocked_permissions,
                "authorized": self.authorized, "requested": self.requested, "reasons": self.reasons}


@dataclass
class RoutingDecision:
    request_task_classes: list[str]
    selected: list[Candidate]
    considered: list[Candidate]
    overrides: list[str]
    unresolved_overrides: list[str]
    independent_review: bool
    general: bool
    unsupported: list[dict[str, Any]]
    catalog_version: str
    latency_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {"task_classes": self.request_task_classes, "selected": [c.as_dict() for c in self.selected],
                "considered": [c.as_dict() for c in self.considered], "overrides": self.overrides,
                "unresolved_overrides": self.unresolved_overrides, "independent_review": self.independent_review,
                "general": self.general, "unsupported": self.unsupported, "catalog_version": self.catalog_version,
                "latency_ms": round(self.latency_ms, 2)}


def overrides_in(text: str) -> list[str]:
    found = []
    for pat in _OVERRIDE:
        for m in pat.finditer(text):
            found.append(m.group(1).strip())
    return found


def route(text: str, *, can=None, prefer: list[str] | None = None, workspace_domains: list[str] | None = None,
          catalog: Catalog | None = None, limit: int = MAX_SELECTED) -> RoutingDecision:
    """Choose the expertise for ``text``. ``can(permission) -> bool`` answers for the actor (no callable: nothing is
    authorized beyond analyst.ask, so the decision shows what would be blocked); ``prefer`` lists specialization ids
    the person asked for; ``workspace_domains`` are the business profile's domain package ids."""
    t0 = time.perf_counter()
    cat = catalog or load()
    ix = _index(cat.version)
    can = can or (lambda perm: perm == "analyst.ask")
    classes = task_classes_of(text)
    q = tokens(text)
    lowered = " " + re.sub(r"[^a-z0-9&/+-]+", " ", text.lower()) + " "
    boost_domains = {d for pkg in (workspace_domains or []) for d in PACKAGE_DOMAINS.get(pkg, [])}

    requested: list[str] = []
    unresolved: list[str] = []
    for pid in prefer or []:
        (requested if pid in cat.profiles else unresolved).append(pid)
    for phrase in overrides_in(text):
        pid = resolve_name(phrase, cat)
        if pid and pid not in requested:
            requested.append(pid)
        elif not pid:
            unresolved.append(phrase)

    av = availability()
    cands: list[Candidate] = []
    for pid, p in cat.profiles.items():
        r = _bm25(ix, q, pid)
        hits = [ph for ph in ix.phrases[pid] if f" {ph} " in lowered or f" {ph}s " in lowered]
        overlap = [c for c in classes if c in p.analytics.task_classes]
        if r <= 0 and not hits and pid not in requested:
            continue
        score = r + 1.5 * len(hits) + 0.8 * len(overlap)
        reasons = []
        if hits:
            reasons.append("the request uses its vocabulary: " + ", ".join(f'"{h}"' for h in hits[:3]))
        if overlap:
            reasons.append("it covers the kind of work asked for: " + ", ".join(o.replace("_", " ") for o in overlap[:3]))
        if p.identity.domain in boost_domains:
            score *= 1.1
            reasons.append("it matches the workspace's business profile")
        req_caps = p.resources.required_capabilities
        missing = [c for c in req_caps if av[c]["status"] == "missing"]
        partial = [c for c in req_caps if av[c]["status"] == "partial"]
        score *= 1.0 - 0.15 * (len(missing) + 0.5 * len(partial)) / max(1, len(req_caps))
        blocked = [perm for perm in p.governance.required_permissions if not can(perm)]
        is_req = pid in requested
        if is_req:
            score += 1000.0  # a requested perspective is always selected; it still gets no extra permission
            reasons.insert(0, "you asked for this perspective")
        matched = frozenset(t for t in q if t in ix.docs[pid])
        if not is_req and not hits and len(matched) < 2 and not [o for o in overlap if o not in _GENERIC]:
            continue  # one shared word is not evidence of a discipline ("weather" alone is not an energy question)
        cands.append(Candidate(pid, p.name, p.identity.domain, score, r, overlap, hits, assess(p)["level"], missing, partial,
                               blocked, reasons, is_req, matched))
    cands.sort(key=lambda c: -c.score)
    considered = cands[:12]

    selected: list[Candidate] = [c for c in cands if c.requested][:limit]
    top = next((c for c in cands if not c.requested), None)
    general = False
    if not selected:
        if not top or top.score < MIN_SCORE:
            general = True
        else:
            selected.append(top)
    if selected:
        covered = {tc for c in selected for tc in cat.get(c.id).analytics.task_classes}
        words = {t for c in selected for t in c.matched}
        free = [c for c in selected if not c.requested]
        anchor = max(c.score for c in free) if free else (top.score if top else 0.0)
        for c in cands:
            if len(selected) >= limit:
                break
            if c in selected or c.score < max(MIN_SCORE, 0.4 * anchor):
                continue
            adds = [tc for tc in c.task_overlap if tc not in covered]
            new_words = sorted(c.matched - words)
            if not c.task_overlap:
                continue
            if not requested and c.score >= 0.65 * anchor and (c.phrase_hits or len(c.task_overlap) >= 2):
                c.reasons.append("it is nearly as relevant as the first choice")
            elif adds and (c.phrase_hits or c.score >= 0.5 * anchor):
                c.reasons.append("it adds " + ", ".join(a.replace("_", " ") for a in adds))
            elif len(new_words) >= 2 and c.domain not in {x.domain for x in selected}:
                c.reasons.append("it covers part of the request the others do not: " + ", ".join(new_words[:3]))
            else:
                continue
            selected.append(c)
            covered |= set(cat.get(c.id).analytics.task_classes)
            words |= c.matched
    for c in selected:
        if c.blocked_permissions:
            c.reasons.append("your role does not include " + ", ".join(c.blocked_permissions)
                             + ", so it contributes its perspective but not the work that needs those permissions")

    # task classes the request needs that no available capability can carry
    unsupported = []
    for tc in classes:
        caps = [c for c in LIBRARY.values() if tc in c.task_classes]
        if caps and all(av[c.id]["status"] == "missing" for c in caps):
            unsupported.append({"task_class": tc, "description": TASK_CLASSES[tc],
                                "missing": [c.id for c in caps],
                                "next_step": "Lyst can draft this method as a skill through the Capability Foundry; it runs only "
                                             "after its tests pass and an owner approves it."})
    return RoutingDecision(classes, selected, considered, requested, unresolved, bool(_INDEPENDENT.search(text)), general,
                           unsupported, cat.version, (time.perf_counter() - t0) * 1000)
