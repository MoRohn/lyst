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
    _c("data_cleaning", "Data cleaning and derivation", "Governed SQL transforms, and cleaned copies: whitespace and types fixed, "
       "exact and near-duplicate names merged (difflib similarity with blocking), missing numbers filled with flags.",
       "available", ["cap:clean_dataset", "mod:lyst.analytics.methods:clean_frame", "mod:lyst.transforms:TransformService"],
       "a dataset", "a new <name>_clean dataset version and a report of every change", ["the first of near-duplicates is the record to keep"],
       ["near-duplicate merging is name similarity only; it never edits the source dataset"],
       permission="data.ingest", task_classes=["data_quality"]),
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
    _c("classification", "Supervised classification", "Logistic regression, binary or multinomial, with a time-ordered holdout.",
       "available", ["cap:run_method", "mod:lyst.analytics.methods:classify"], "rows with a target (2 to 12 classes) and numeric features",
       "held-out accuracy, per-class precision and recall, AUC (binary), standardized coefficients",
       ["roughly linear log-odds", "labels are correct"],
       ["linear models only: no trees or ensembles; coefficients are associations, not causes"], task_classes=["model_evaluation", "forecasting"]),

    _c("clustering", "Clustering and segmentation", "k-means on standardized numeric features, k chosen by silhouette.",
       "available", ["cap:run_method", "mod:lyst.analytics.methods:segment"], "numeric feature rows per entity",
       "segments with sizes and feature profiles, silhouette", ["features are comparable once standardized"],
       ["k-means only (spherical groups); categorical features are not used"], task_classes=["segmentation"]),

    _c("optimization", "Optimization", "Exact linear and integer programs for budget allocation (HiGHS), plus marginal-return "
       "allocation and minimum staffing.", "available",
       ["cap:run_method", "mod:lyst.analytics.methods:optimize_allocation", "mod:lyst.analytics.planning:allocate",
        "mod:lyst.analytics.planning:minimum_staff"], "options with cost and value per unit, limits and a budget",
       "the optimal allocation, its value and the binding limits", ["value per unit is constant", "options are independent"],
       ["linear objectives only: no diminishing returns or non-linear constraints"], task_classes=["optimization", "capacity_planning"]),

    _c("simulation", "Monte Carlo simulation", "Sampling of driver models and staffing scenarios.", "available",
       ["cap:staffing_scenarios", "mod:lyst.scenarios:simulate", "mod:lyst.analytics.planning:staffing_scenarios"],
       "a driver model with distributions", "outcome distribution and percentiles", ["inputs are independent unless modeled"],
       ["no discrete-event or agent-based simulation"], task_classes=["simulation", "capacity_planning"]),
    _c("scenario_modeling", "Scenario and what-if modeling", "Versioned driver models with sensitivity and best/base/worst cases.",
       "available", ["cap:what_if", "mod:lyst.scenarios:sensitivity", "mod:lyst.scenarios:cases"], "an approved driver model",
       "scenario outcomes and sensitivities", ["the model's formulas describe the business"], ["models are defined by people, not learned"],
       task_classes=["simulation", "recommendation"]),
    _c("geospatial_analysis", "Geospatial analysis", "Great-circle distances and nearest neighbours, point-in-polygon regions "
       "(GeoJSON or coordinate rings) and shortest travel time over a supplied road or route network (Dijkstra).",
       "available", ["cap:run_method", "mod:lyst.analytics.methods:distances", "mod:lyst.analytics.methods:regions",
                     "mod:lyst.analytics.methods:travel_times"], "points with coordinates, region polygons, or a network of links",
       "nearest targets, points per region, travel time and route from an origin", ["decimal-degree coordinates (WGS84)"],
       ["no map projections or rendering, polygon holes are ignored; travel times come from the network supplied, not live traffic"],
       task_classes=["geospatial_analysis"]),
    _c("text_analytics", "Text analytics", "Themes (TF-IDF), lexicon sentiment and simple entities in a text column, and "
       "passage retrieval over governed documents.", "available",
       ["cap:run_method", "mod:lyst.analytics.methods:text_themes", "mod:lyst.knowledge:KnowledgeService"], "a text column or documents",
       "themes with counts, negative share by group, entities; ranked passages", ["English text"],
       ["no trained topic or sentiment model: sentiment is word-list based and misses sarcasm and negation"],
       permission="data.read", task_classes=["text_analytics", "research_synthesis"]),

    _c("document_extraction", "Document extraction", "Text and tables from PDF, DOCX, PPTX, HTML and Markdown.", "available",
       ["mod:lyst.ingest.documents:read_document"], "a document file", "paragraphs and tables with page references",
       ["text-based documents"], ["no OCR for scanned images"], permission="data.ingest", task_classes=["document_extraction"]),
    _c("knowledge_retrieval", "Knowledge retrieval", "Workspace documents, glossary, memory and prior verified answers.", "available",
       ["mod:lyst.knowledge:KnowledgeService", "mod:lyst.memory:MemoryService"], "a question", "passages and memories with trust levels",
       ["the knowledge is approved for the workspace"], ["advisory; numbers still come from data"], permission="data.read",
       task_classes=["research_synthesis"]),
    _c("model_evaluation", "Model evaluation", "Scores recorded predictions or eval results: accuracy and per-class precision and "
       "recall, regression error and bias, pass rates with intervals, by segment; plus forecast backtests and Lyst's eval bank.",
       "available", ["cap:run_method", "mod:lyst.analytics.methods:evaluate_predictions", "mod:lyst.experiments:backtest",
                     "mod:lyst.experiments:compare_methods", "mod:lyst.evals:run"],
       "predictions with actuals, or eval results with pass/fail, optionally a segment", "overall and per-segment scores",
       ["predictions were recorded before outcomes were known"],
       ["scores what was recorded; it does not run, retrain or red-team models itself"], task_classes=["model_evaluation"]),
    _c("process_mining", "Process mining", "Variants, directly-follows handoffs, waiting times, rework and cycle times from an event log.",
       "available", ["cap:run_method", "mod:lyst.analytics.methods:process_map"], "an event log (case, activity, timestamp)",
       "paths, handoff waits, rework share and cycle-time percentiles", ["events carry a case id and an accurate timestamp"],
       ["no conformance checking against a reference model yet"], task_classes=["process_analysis"]),

    _c("financial_reconciliation", "Reconciliation", "Independent recompute of answers and key-level matching of two sources with a tolerance.",
       "available", ["cap:validate", "cap:quality_scan", "cap:run_method", "mod:lyst.analytics.methods:reconcile"],
       "a computed figure and its source, or two datasets sharing a key", "agreement, breaks by key and the unexplained difference",
       ["both sides use the same currency and period"], ["exact key matching; no fuzzy matching of descriptions or dates"],
       task_classes=["reconciliation"]),

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
    _c("code_analysis", "Source code analysis", "Python syntax-tree analysis (McCabe complexity, length, nesting, Bandit-style "
       "security rules), Slither-style Solidity rules, duplicated blocks and TODOs in any language, change hotspots, and the "
       "files' Python tests run in Lyst's OS sandbox with failure tracebacks.", "available",
       ["cap:run_method", "mod:lyst.analytics.code:analyze", "mod:lyst.analytics.code:run_tests"],
       "source files as rows (path, source) and optionally a change history (path, changes)",
       "findings with file and line, complex functions, hotspots, test outcomes",
       ["the files are the code under review", "the sandbox is available on the host for test runs"],
       ["deep rules for Python and Solidity only; other languages get size, duplication and marker metrics",
        "pattern matches, not data-flow proofs: review each finding", "tests run only where the OS sandbox is available"],
       task_classes=["code_analysis"]),
    _c("log_analysis", "Log and telemetry analysis", "Raw lines in common formats (JSON, key=value, web access, syslog) or "
       "tabular events: event mix, rare events, per-entity bursts, repeated-failures-then-success sequences and timelines.",
       "available", ["cap:run_method", "mod:lyst.analytics.methods:log_patterns", "mod:lyst.analytics.methods:parse_logs"],
       "raw log lines or events (timestamp, event, entity)", "bursts, sequences, timelines and the event mix",
       ["timestamps are synchronized"], ["formats beyond the four common ones are counted as unparsed, not guessed; no "
                                         "multi-stage kill-chain correlation"],
       task_classes=["security_investigation", "performance_analysis", "root_cause", "anomaly_detection"]),

    _c("graph_analysis", "Graph analysis", "Components, degree, betweenness (brokers), communities and shortest paths.",
       "available", ["cap:run_method", "mod:lyst.analytics.methods:graph"], "an edge list (two entity columns)",
       "groups, brokers, communities and paths", ["edges are deduplicated"],
       ["undirected and unweighted; exact betweenness up to about 2,000 entities"],
       task_classes=["segmentation", "research_synthesis", "data_modeling"]),

    _c("web_research", "External research", "Fetch https pages a request names, from hosts an administrator approved, and quote "
       "what they say with URL, time and checksum; corroboration across sources.", "available",
       ["cap:run_method", "mod:lyst.specialists.research:research"], "https URLs in the request",
       "quotes per source, corroborated or single-source", ["the approved hosts are the right sources"],
       ["no search engine and no logins: only named pages on approved hosts are read"], task_classes=["research_synthesis"]),
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
