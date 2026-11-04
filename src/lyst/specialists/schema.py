# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The specialization contract: one typed, versioned profile per analytical discipline.

A profile describes expertise Lyst can *assemble*: what the discipline is responsible for, the methods it uses, which
shared computational capabilities it draws on, the data it expects, how its results must be verified and what it may
never do. It is configuration and reference knowledge, never authority: a profile cannot grant a permission, name a
capability that is not in the shared library, or change Lyst's system instructions.

Maturity is computed, not claimed (``maturity.py``): a profile that validates is Level 1 (Defined); it is Level 2
(Equipped) only when every capability it requires is actually available; Levels 3 to 5 need recorded evidence.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class Maturity(IntEnum):
    DEFINED = 1        # complete profile, knowledge description and task taxonomy
    EQUIPPED = 2       # every required computational capability is available
    OPERATIONAL = 3    # representative tasks executed successfully under controlled conditions
    VALIDATED = 4      # domain benchmarks and adversarial tests passed
    PRODUCTION = 5     # security, reliability, observability and release criteria met for a stated scope

    @property
    def label(self) -> str:
        return {1: "Defined", 2: "Equipped", 3: "Operational", 4: "Validated", 5: "Production qualified"}[int(self)]


# The task taxonomy: what kind of analytical work a request is. Routing classifies requests into these and profiles
# declare which ones they support, so selection compares like with like instead of matching job titles to words.
TASK_CLASSES: dict[str, str] = {
    "descriptive_reporting": "Report what a measure was for a period or segment.",
    "variance_explanation": "Explain why a measure moved between periods or against plan.",
    "root_cause": "Trace an outcome or incident to its contributing causes.",
    "trend_analysis": "Describe direction, seasonality and breaks in a series over time.",
    "forecasting": "Project a measure forward with stated uncertainty.",
    "anomaly_detection": "Find observations that depart from expected behaviour.",
    "data_profiling": "Describe a dataset's structure, types, completeness and distributions.",
    "data_quality": "Check data against rules for completeness, validity, consistency and timeliness.",
    "data_modeling": "Design or review entities, keys, grain and relationships.",
    "sql_authoring": "Write, review or optimize a query.",
    "statistical_testing": "Test a hypothesis with an appropriate statistical procedure.",
    "experiment_analysis": "Read out a controlled experiment or A/B test.",
    "causal_estimation": "Estimate the effect of an intervention beyond correlation.",
    "optimization": "Choose the best allocation or configuration under constraints.",
    "simulation": "Explore outcomes under uncertainty by sampling or modeling a system.",
    "segmentation": "Group entities into meaningful segments or cohorts.",
    "attribution": "Assign credit for an outcome across channels, touchpoints or factors.",
    "reconciliation": "Prove that totals agree across sources, periods or systems.",
    "cost_analysis": "Allocate, explain or reduce spend.",
    "capacity_planning": "Size resources against expected demand.",
    "risk_assessment": "Identify, rate and prioritize risks and controls.",
    "security_investigation": "Examine telemetry and evidence for threats or misuse.",
    "compliance_review": "Check practices or records against a policy or regulation.",
    "code_analysis": "Assess source code, tests or architecture for defects and risk.",
    "performance_analysis": "Measure latency, throughput, utilization or efficiency.",
    "model_evaluation": "Measure the accuracy, robustness or cost of a predictive or generative model.",
    "document_extraction": "Pull structured facts and tables out of documents.",
    "text_analytics": "Classify, summarize or mine themes from text.",
    "geospatial_analysis": "Analyse data by location, distance or region.",
    "process_analysis": "Reconstruct and measure how work flows through a process.",
    "research_synthesis": "Gather and synthesize evidence on a question from sources.",
    "dashboard_design": "Design a governed dashboard or recurring report.",
    "recommendation": "Recommend actions with their expected effect and trade-offs.",
}

TaskClass = Literal[tuple(TASK_CLASSES)]  # type: ignore[valid-type]

TitleKind = Literal["job_title", "functional", "lyst_profile"]
Lifecycle = Literal["draft", "active", "deprecated", "retired"]
Sensitivity = Literal["standard", "confidential", "restricted"]
Origin = Literal["catalog", "workspace"]


def _nonempty(v: list[str], name: str) -> list[str]:
    items = [x.strip() for x in v if isinstance(x, str) and x.strip()]
    if not items:
        raise ValueError(f"{name} must not be empty")
    if len({x.lower() for x in items}) != len(items):
        raise ValueError(f"{name} has duplicates")
    return items


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Identity(_Strict):
    id: str
    name: str
    description: str = Field(min_length=60, max_length=900)
    domain: str
    subdomain: str
    title_kind: TitleKind
    alternative_titles: list[str] = Field(default_factory=list)
    version: str = "1.0.0"
    lifecycle: Lifecycle = "active"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID.match(v):
            raise ValueError(f"invalid specialization id {v!r}")
        return v


class Expertise(_Strict):
    responsibilities: list[str]
    knowledge_domains: list[str]
    industry_context: list[str]
    terminology: list[str]
    technical_concepts: list[str]
    methodologies: list[str]
    standards: list[str]

    @model_validator(mode="after")
    def _complete(self):
        for name in ("responsibilities", "knowledge_domains", "industry_context", "terminology", "technical_concepts",
                     "methodologies", "standards"):
            _nonempty(getattr(self, name), name)
        return self


class Analytics(_Strict):
    task_classes: list[TaskClass]  # type: ignore[valid-type]
    investigation_techniques: list[str]
    statistical_methods: list[str]
    computational_methods: list[str]
    modeling_approaches: list[str]
    data_transformations: list[str]
    visualization_methods: list[str]
    reporting: list[str]

    @model_validator(mode="after")
    def _complete(self):
        if not self.task_classes:
            raise ValueError("task_classes must not be empty")
        for name in ("investigation_techniques", "statistical_methods", "computational_methods", "modeling_approaches",
                     "data_transformations", "visualization_methods", "reporting"):
            _nonempty(getattr(self, name), name)
        return self


class DataRequirements(_Strict):
    source_systems: list[str]
    structures: list[str]
    required_fields: list[str]
    entities: list[str]
    quality_requirements: list[str]
    temporal: list[str]
    semantic_assumptions: list[str]

    @model_validator(mode="after")
    def _complete(self):
        for name in type(self).model_fields:
            _nonempty(getattr(self, name), name)
        return self


class Resources(_Strict):
    required_capabilities: list[str]   # shared-library ids (library.py); maturity depends on these being available
    optional_capabilities: list[str] = Field(default_factory=list)
    sql: list[str]
    python_packages: list[str]
    models: list[str]                  # model workloads used (classify, structured, chat ...), never a provider name
    mcp_integrations: list[str] = Field(default_factory=list)
    external_apis: list[str] = Field(default_factory=list)
    environment: list[str]

    @model_validator(mode="after")
    def _complete(self):
        for name in ("required_capabilities", "sql", "python_packages", "models", "environment"):
            _nonempty(getattr(self, name), name)
        if set(self.required_capabilities) & set(self.optional_capabilities):
            raise ValueError("a capability cannot be both required and optional")
        return self


class Execution(_Strict):
    planning_depth: Literal["shallow", "standard", "deep"]
    decomposition: list[str]
    verification_rules: list[str]      # ids from verification.py
    resource_constraints: list[str]
    outputs: list[str]
    approval_conditions: list[str]

    @model_validator(mode="after")
    def _complete(self):
        for name in ("decomposition", "verification_rules", "resource_constraints", "outputs", "approval_conditions"):
            _nonempty(getattr(self, name), name)
        return self


class Quality(_Strict):
    correctness_criteria: list[str]
    domain_tests: list[str]
    expected_evidence: list[str]
    reproducibility: list[str]
    performance_measures: list[str]
    known_limitations: list[str]
    example_requests: list[str]

    @model_validator(mode="after")
    def _complete(self):
        for name in type(self).model_fields:
            _nonempty(getattr(self, name), name)
        return self


class Governance(_Strict):
    required_permissions: list[str]    # Lyst permission names (security.PERMISSIONS); never granted by the profile
    sensitivity: Sensitivity
    sensitive_handling: list[str]
    network: Literal["none", "workspace_connections", "approved_external"]
    classification_notes: list[str]
    allowed_external_operations: list[str] = Field(default_factory=list)
    review_requirements: list[str]

    @model_validator(mode="after")
    def _complete(self):
        for name in ("required_permissions", "sensitive_handling", "classification_notes", "review_requirements"):
            _nonempty(getattr(self, name), name)
        return self


class Provenance(_Strict):
    origin: Origin
    owner: str
    last_validated: str | None = None  # ISO date of the last recorded validation run, None until one exists
    revisions: list[str]
    dependencies: list[str] = Field(default_factory=list)
    compatibility: str = ">=0.1"
    deprecated_by: str | None = None


class SpecializationProfile(_Strict):
    schema_version: str = SCHEMA_VERSION
    identity: Identity
    expertise: Expertise
    analytics: Analytics
    data: DataRequirements
    resources: Resources
    execution: Execution
    quality: Quality
    governance: Governance
    provenance: Provenance
    related: list[str] = Field(default_factory=list)
    keywords: list[str]                 # routing vocabulary: phrases a request in this discipline tends to use

    @property
    def id(self) -> str:
        return self.identity.id

    @property
    def name(self) -> str:
        return self.identity.name

    @model_validator(mode="after")
    def _keywords(self):
        _nonempty(self.keywords, "keywords")
        if self.id in self.related:
            raise ValueError("a profile cannot list itself as related")
        return self
