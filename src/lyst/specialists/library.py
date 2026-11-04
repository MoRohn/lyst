# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The shared computational capability library: one typed entry per analytical method family.

Profiles never carry their own code. They reference these entries, and each entry points at what actually implements
it in Lyst: a registered analyst capability (``cap:<name>``, run through the registry's permission check) or a
module function (``mod:<module>:<attr>``). Availability is computed by resolving those references at runtime, so an
entry whose implementation is missing reads as missing, whatever the catalog says.

``status`` is the honest state of the method family as a whole:
- ``available``: the implementation covers the family's common cases end to end.
- ``partial``: something real exists but covers only part of the family (``coverage`` says which part).
- ``missing``: nothing in Lyst implements it yet; requests that need it get a limitation and a Foundry proposal.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from functools import cache
from typing import Literal

Status = Literal["available", "partial", "missing"]


@dataclass(frozen=True)
class LibraryCapability:
    id: str
    title: str
    description: str
    status: Status
    implemented_by: tuple[str, ...]
    inputs: str
    outputs: str
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]
    coverage: str = ""
    permission: str = "data.query"
    task_classes: tuple[str, ...] = field(default_factory=tuple)


def _c(id: str, title: str, description: str, status: Status, implemented_by: list[str], inputs: str, outputs: str,
       assumptions: list[str], limitations: list[str], coverage: str = "", permission: str = "data.query",
       task_classes: list[str] | None = None) -> LibraryCapability:
    return LibraryCapability(id, title, description, status, tuple(implemented_by), inputs, outputs, tuple(assumptions),
                             tuple(limitations), coverage, permission, tuple(task_classes or ()))


LIBRARY: dict[str, LibraryCapability] = {c.id: c for c in [
    _c("data_profiling", "Data profiling", "Column roles, types, completeness, distinct counts, distributions and correlations.",
       "available", ["cap:profile_dataset", "mod:lyst.analytics.profile:profile"], "a dataset the actor may read",
       "a profile with per-column statistics and issues", ["the dataset fits in memory for profiling"],
       ["profiles a sample-free full read; very wide tables are capped at a correlation subset"], permission="data.read",
       task_classes=["data_profiling"]),
    _c("data_cleaning", "Data cleaning and derivation", "Governed SQL transforms that derive cleaned datasets with lineage.",
       "partial", ["mod:lyst.transforms:TransformService", "mod:lyst.transforms:lint"], "SQL over workspace datasets",
       "a derived, versioned dataset", ["cleaning rules can be expressed in SQL"],
       ["no automatic imputation or entity resolution; rules are written, reviewed and versioned"],
       coverage="rule-based SQL transforms; no fuzzy matching or imputation models", permission="data.ingest",
       task_classes=["data_quality"]),
    _c("schema_validation", "Schema and contract validation", "Versioned data contracts checked on every ingest.",
       "available", ["mod:lyst.contracts:ContractService"], "a dataset and its contract", "pass/fail per rule with the breaking change",
       ["the contract states the expected columns, types and rules"], ["checks run at ingest, not continuously on sources"],
       permission="data.read", task_classes=["data_quality", "data_modeling"]),
    _c("sql_query", "Guarded SQL execution", "Read-only SQL through the SQL guard with row policies and fan-out checks.",
       "available", ["cap:run_sql"], "a SELECT statement", "rows with the query recorded as evidence",
       ["the question can be answered from workspace datasets"], ["read-only; DDL and writes are refused by the guard"],
       task_classes=["sql_authoring", "descriptive_reporting", "process_analysis"]),
    _c("metric_computation", "Governed metric computation", "Values, breakdowns and series of approved metric definitions.",
       "available", ["cap:metric_value", "cap:metric_breakdown", "cap:metric_series"], "an approved metric, period and filters",
       "values with the compiled SQL as evidence", ["the metric is defined in the semantic layer"],
       ["only approved metrics; ad hoc measures go through SQL"], task_classes=["descriptive_reporting", "trend_analysis"]),
    _c("statistical_testing", "Statistical testing", "Two-proportion and Welch tests, bootstrap intervals and hypothesis tests on metrics.",
       "available", ["cap:test_hypotheses", "mod:lyst.analytics.stats:welch", "mod:lyst.analytics.stats:two_proportions",
                     "mod:lyst.analytics.stats:bootstrap_ci"], "samples or metric splits", "test statistic, p-value, interval and verdict",
       ["independent observations", "samples large enough for the chosen test"],
       ["no multiple-comparison correction across separate requests; no Bayesian procedures"], task_classes=["statistical_testing", "experiment_analysis"]),
    _c("regression", "Regression and correlation", "Correlation between metrics with lag and significance.",
       "partial", ["cap:correlate_metrics"], "two metric series", "correlation, lag and significance",
       ["aligned series at a shared grain"], ["no multivariate regression model fitting or diagnostics yet"],
       coverage="pairwise correlation only", task_classes=["statistical_testing"]),
    _c("time_series_forecasting", "Time-series forecasting", "Six backtested methods (naive to ETS) with intervals and method comparison.",
       "available", ["cap:forecast_metric", "cap:forecast_period", "mod:lyst.experiments:backtest"], "a metric series with a regular grain",
       "forecast, prediction interval and backtest scores", ["the past pattern carries forward", "a regular grain without long gaps"],
       ["no exogenous regressors or hierarchical reconciliation"], task_classes=["forecasting", "trend_analysis"]),
    _c("anomaly_detection", "Anomaly detection", "Robust-score anomaly scan over a metric series.", "available",
       ["cap:anomaly_scan"], "a metric series", "flagged points with scores", ["a stable baseline exists"],
       ["univariate only; no multivariate or streaming detection"], task_classes=["anomaly_detection"]),
    _c("change_point_detection", "Change-point detection", "Penalized segmentation of a series into regimes.", "available",
       ["cap:change_points", "mod:lyst.analytics.changepoint:change_points"], "a series", "break dates and segment means",
       ["shifts in level, not gradual drift"], ["level shifts only"], task_classes=["trend_analysis", "anomaly_detection"]),
    _c("driver_decomposition", "Driver decomposition", "Contribution, price-volume-mix and rate decomposition across dimensions.",
       "available", ["cap:driver_analysis", "mod:lyst.analytics.decomp:decompose", "mod:lyst.analytics.drivers:explain"],
       "a metric in two periods with dimensions", "ranked contributions that sum to the change",
       ["the measure is additive or a ratio of additive measures"], ["attribution is arithmetic, not causal"],
       task_classes=["variance_explanation", "root_cause"]),
    _c("causal_impact", "Causal impact estimation", "Interrupted time series and difference-in-differences around an event.",
       "available", ["cap:event_impact", "mod:lyst.analytics.causal:interrupted_time_series",
                     "mod:lyst.analytics.causal:difference_in_differences"], "a series and an event date (and a control series)",
       "effect estimate with interval", ["no other change coincides with the event", "parallel trends for DiD"],
       ["no synthetic control or instrumental variables"], task_classes=["causal_estimation"]),
    _c("classification", "Supervised classification", "Train and evaluate classifiers on labelled rows.", "missing", [],
       "labelled rows", "a model with held-out metrics", ["labels are correct and representative"],
       ["not implemented: Lyst has no model-training capability yet"], task_classes=["model_evaluation"]),
    _c("clustering", "Clustering and segmentation", "Unsupervised grouping of entities by features.", "missing", [],
       "feature rows per entity", "segments with profiles", ["features are comparable in scale"],
       ["not implemented; rule-based segments can be built with SQL breakdowns"], task_classes=["segmentation"]),
    _c("optimization", "Optimization", "Allocation of a budget across channels by marginal return.", "partial",
       ["mod:lyst.analytics.planning:allocate", "mod:lyst.analytics.planning:minimum_staff"], "options with returns and limits",
       "an allocation with its expected outcome", ["returns are known or estimated per option"],
       ["greedy allocation and staffing only; no general linear or integer programming solver"],
       coverage="budget allocation and minimum staffing", task_classes=["optimization", "capacity_planning"]),
    _c("simulation", "Monte Carlo simulation", "Sampling of driver models and staffing scenarios.", "available",
       ["cap:staffing_scenarios", "mod:lyst.scenarios:simulate", "mod:lyst.analytics.planning:staffing_scenarios"],
       "a driver model with distributions", "outcome distribution and percentiles", ["inputs are independent unless modeled"],
       ["no discrete-event or agent-based simulation"], task_classes=["simulation", "capacity_planning"]),
    _c("scenario_modeling", "Scenario and what-if modeling", "Versioned driver models with sensitivity and best/base/worst cases.",
       "available", ["cap:what_if", "mod:lyst.scenarios:sensitivity", "mod:lyst.scenarios:cases"], "an approved driver model",
       "scenario outcomes and sensitivities", ["the model's formulas describe the business"], ["models are defined by people, not learned"],
       task_classes=["simulation", "recommendation"]),
    _c("geospatial_analysis", "Geospatial analysis", "Distance, containment and regional aggregation.", "missing", [],
       "coordinates or geometries", "spatial joins and aggregates", ["a known coordinate reference system"],
       ["not implemented: no spatial extension or geometry types are loaded"], task_classes=["geospatial_analysis"]),
    _c("text_analytics", "Text analytics", "Passage retrieval over governed documents.", "partial",
       ["mod:lyst.knowledge:KnowledgeService"], "text collections", "ranked passages with sources",
       ["documents are ingested into the knowledge store"], ["retrieval only; no topic modeling, sentiment or entity extraction"],
       coverage="search and retrieval", permission="data.read", task_classes=["text_analytics", "research_synthesis"]),
    _c("document_extraction", "Document extraction", "Text and tables from PDF, DOCX, PPTX, HTML and Markdown.", "available",
       ["mod:lyst.ingest.documents:read_document"], "a document file", "paragraphs and tables with page references",
       ["text-based documents"], ["no OCR for scanned images"], permission="data.ingest", task_classes=["document_extraction"]),
    _c("knowledge_retrieval", "Knowledge retrieval", "Workspace documents, glossary, memory and prior verified answers.", "available",
       ["mod:lyst.knowledge:KnowledgeService", "mod:lyst.memory:MemoryService"], "a question", "passages and memories with trust levels",
       ["the knowledge is approved for the workspace"], ["advisory; numbers still come from data"], permission="data.read",
       task_classes=["research_synthesis"]),
    _c("model_evaluation", "Model evaluation", "Rolling-origin backtests and method comparison; Lyst's own eval bank.", "partial",
       ["mod:lyst.experiments:backtest", "mod:lyst.experiments:compare_methods", "mod:lyst.evals:run"],
       "a model's predictions or a method and a series", "error metrics, calibration and a significance-checked winner",
       ["held-out data is not leaked into fitting"], ["forecast models and Lyst's own answers; no classifier or LLM benchmark harness"],
       coverage="forecast backtests and Lyst's eval bank", task_classes=["model_evaluation"]),
    _c("process_mining", "Process mining", "Discover process maps and conformance from event logs.", "missing", [],
       "an event log (case, activity, timestamp)", "process map, variants and cycle times", ["events carry a case id"],
       ["not implemented"], task_classes=["process_analysis"]),
    _c("financial_reconciliation", "Reconciliation", "Independent recompute of reported figures and integrity checks.", "partial",
       ["cap:validate", "cap:quality_scan"], "a computed figure and its source", "agreement or the size of the difference",
       ["both sides are in the workspace"], ["recomputes Lyst's own figures; no general two-source ledger matching yet"],
       coverage="independent recompute of answers", task_classes=["reconciliation"]),
    _c("root_cause_analysis", "Root-cause analysis", "Hypotheses generated and tested against the data, with drivers.", "available",
       ["cap:hypothesize", "cap:test_hypotheses", "cap:driver_analysis"], "a metric change", "tested hypotheses with evidence",
       ["candidate causes are observable in the data"], ["finds associated drivers; causal claims need causal_impact"],
       task_classes=["root_cause", "variance_explanation"]),
    _c("data_quality_monitoring", "Data quality monitoring", "Quality scans, contract checks and monitors with alerts.", "available",
       ["cap:quality_scan", "mod:lyst.monitoring:MonitorService"], "datasets and rules", "findings and alerts",
       ["rules are agreed"], ["batch checks at ingest and on schedule, not streaming"], task_classes=["data_quality", "anomaly_detection"]),
    _c("concentration_analysis", "Concentration and inequality", "Herfindahl, Gini and Pareto via Foundry-templated skills.",
       "partial", ["mod:lyst.analyst.foundry:TEMPLATES"], "a measure across members", "index with the members' shares",
       ["members are comparable"], ["drafted as skills that need an owner's approval before they run"],
       coverage="templates; registration requires approval", task_classes=["segmentation", "risk_assessment"]),
    _c("visualization", "Visualization and dashboards", "Governed dashboards generated as apps from approved metrics.", "available",
       ["cap:design_dashboard", "cap:build_dashboard"], "approved metrics", "a dashboard with its sources",
       ["metrics are approved"], ["dashboards are built from metrics, not arbitrary SQL"], permission="data.read",
       task_classes=["dashboard_design"]),
    _c("reporting", "Reporting and narrative", "Answer, evidence, implications and actions with figures checked against computed values.",
       "available", ["cap:deliver"], "computed outputs", "a written answer whose numbers are all backed",
       ["outputs were computed in this task"], ["the narrative cannot introduce a number that was not computed"],
       permission="analyst.ask", task_classes=["descriptive_reporting", "recommendation"]),
    _c("artifact_generation", "Artifact generation", "PDF, PPTX, spreadsheet and Markdown deliverables with provenance.", "available",
       ["mod:lyst.deliverables:ArtifactService"], "a finished task", "a versioned artifact", ["the task is verified"],
       ["export is subject to the data policy"], permission="data.read", task_classes=["descriptive_reporting"]),
    _c("code_analysis", "Source code analysis", "Static analysis, test execution and trace evidence over a repository.", "missing", [],
       "a repository", "findings with file and line evidence", ["the repository is reachable by an approved connection"],
       ["not implemented: Lyst has no repository connector or code execution sandbox for third-party code"],
       task_classes=["code_analysis"]),
    _c("log_analysis", "Log and telemetry analysis", "Parsing and aggregation of logs, traces and security telemetry.", "missing", [],
       "log or event records", "aggregates, sequences and indicators", ["timestamps are synchronized"],
       ["not implemented as a dedicated capability; tabular logs can be queried with SQL"],
       task_classes=["security_investigation", "performance_analysis", "root_cause"]),
    _c("graph_analysis", "Graph analysis", "Paths, centrality and communities over entity relationships.", "missing", [],
       "nodes and edges", "graph metrics", ["edges are typed and deduplicated"], ["not implemented"],
       task_classes=["segmentation", "research_synthesis"]),
    _c("web_research", "External research", "Search and read external sources with citations.", "missing", [],
       "a research question", "cited findings", ["external access is approved"],
       ["not implemented: Lyst does not browse the web; research uses workspace knowledge only"],
       task_classes=["research_synthesis"]),
]}


def _resolves(ref: str) -> bool:
    kind, _, target = ref.partition(":")
    if kind == "cap":
        from ..analyst import engine  # noqa: F401, PLC0415 - registers the core capabilities
        from ..analyst.registry import REGISTRY  # noqa: PLC0415

        try:
            REGISTRY.get(target)
            return True
        except KeyError:
            return False
    if kind == "mod":
        module, _, attr = target.partition(":")
        try:
            return hasattr(importlib.import_module(module), attr)
        except ImportError:
            return False
    return False


@cache
def availability() -> dict[str, dict]:
    """Each entry's effective state: a declared ``available``/``partial`` entry whose implementation does not resolve
    is reported missing, so the library cannot overstate what is installed."""
    out = {}
    for c in LIBRARY.values():
        broken = [r for r in c.implemented_by if not _resolves(r)]
        status = c.status if not broken else "missing"
        if c.status != "missing" and not c.implemented_by:
            status = "missing"
        out[c.id] = {"status": status, "declared": c.status, "unresolved": broken}
    return out


def describe() -> list[dict]:
    av = availability()
    return [{"id": c.id, "title": c.title, "description": c.description, "status": av[c.id]["status"],
             "coverage": c.coverage, "implemented_by": list(c.implemented_by), "inputs": c.inputs, "outputs": c.outputs,
             "assumptions": list(c.assumptions), "limitations": list(c.limitations), "permission": c.permission,
             "task_classes": list(c.task_classes)} for c in LIBRARY.values()]
