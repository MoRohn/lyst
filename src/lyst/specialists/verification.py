# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Domain verification rules that profiles reference.

Each rule names a check a specialist's result must pass. ``enforced_by`` says where Lyst enforces it today (a check in
the ``validate`` capability, the SQL guard, the delivery number check, the contract service); a rule with no
``enforced_by`` is a declared requirement that is disclosed to the person as *not yet automatically checked* instead
of being silently assumed. Generic model self-evaluation never counts as enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    family: str
    description: str
    enforced_by: str | None = None

    @property
    def automated(self) -> bool:
        return self.enforced_by is not None


def _r(id: str, family: str, title: str, description: str, enforced_by: str | None = None) -> Rule:
    return Rule(id, title, family, description, enforced_by)


RULES: dict[str, Rule] = {r.id: r for r in [
    # universal
    _r("numbers_backed", "universal", "Every figure is computed", "Each number in the written answer matches a value computed in the task.",
       "deliver: unbacked_numbers check"),
    _r("independent_recompute", "universal", "Independent recompute", "The headline figure is recomputed from raw rows in Python and must match the SQL.",
       "validate: independent_recompute / recompute"),
    _r("period_coverage", "universal", "Period coverage and freshness", "The compared periods contain data and the data is fresh enough for the grain.",
       "validate: coverage, freshness, source_age"),
    _r("join_fanout", "universal", "No join inflation", "Joins do not multiply rows and inflate additive measures.", "sql guard: fan-out probe"),
    _r("access_scope", "universal", "Access scope respected", "Only datasets, rows and columns the actor may read were used.",
       "data.access_checks + sql guard row policies"),
    _r("contract_conformance", "data", "Contract conformance", "Inputs satisfy their versioned data contracts.", "contracts: after_ingest"),
    _r("duplicates_nulls", "data", "Duplicates and nulls disclosed", "Duplicate rows and null cells in the inputs are reported.",
       "validate: duplicates, nulls"),
    _r("lineage_recorded", "data", "Lineage recorded", "Derived datasets record their inputs and transformation.", "transforms: lineage"),
    # financial
    _r("bridge_closes", "financial", "Bridge closes", "Contributions by member sum exactly to the total change.", "validate: bridge_closes"),
    _r("parts_reconcile", "financial", "Ratio parts reconcile", "Numerator over denominator equals the reported ratio in every period.",
       "validate: parts_reconcile"),
    _r("single_currency", "financial", "Currency consistency", "Amounts are not summed across currencies.", "validate: single_currency"),
    _r("fiscal_period", "financial", "Fiscal period alignment", "Periods follow the workspace's fiscal calendar.", "contextualize: fiscal calendar"),
    _r("two_source_reconciliation", "financial", "Two-source reconciliation", "Totals agree between two independent systems within tolerance."),
    # statistical
    _r("hypotheses_decided", "statistical", "Hypotheses decided by computation", "Each candidate explanation is accepted or rejected by a test.",
       "validate: hypotheses_decided"),
    _r("uncertainty_stated", "statistical", "Uncertainty stated", "Estimates carry an interval or a significance statement.",
       "forecast/test capabilities return intervals"),
    _r("forecast_backtest", "statistical", "Forecast backtested", "Forecast accuracy is measured on held-out history before it is used.",
       "validate: forecast_backtest, forecast_interval"),
    _r("sample_size", "statistical", "Sample size adequate", "Tests are run only with enough observations for the procedure."),
    _r("multiple_comparisons", "statistical", "Multiple comparisons controlled", "Many simultaneous tests are corrected for false discoveries."),
    _r("causal_claims_guarded", "statistical", "Causal language guarded", "Causal wording requires a causal design; correlation is reported as association.",
       "understand: causal_unsupported intent"),
    _r("model_assumptions", "statistical", "Model assumptions checked", "Residuals and assumptions of a fitted model are checked and disclosed."),
    # security
    _r("evidence_chronology", "security", "Evidence chronology", "Events are ordered on synchronized timestamps before a sequence is inferred."),
    _r("observed_vs_inferred", "security", "Observed vs inferred", "Observed indicators are kept apart from inferred threats in the finding."),
    _r("restricted_data_masked", "security", "Restricted data masked", "Secrets and personal data are redacted from outputs.", "security: redact_obj + credential redactor"),
    # cloud / cost
    _r("cost_allocation_complete", "cost", "Cost allocation complete", "Allocated cost sums to the billed total for the period."),
    _r("rate_assumptions_stated", "cost", "Rate assumptions stated", "Savings estimates state the rates and usage they assume."),
    # software / performance
    _r("reproducible_failure", "software", "Reproducible evidence", "Code findings are backed by a test, trace or reproducible run."),
    _r("measurement_conditions", "performance", "Measurement conditions stated", "Performance figures state load, environment and percentile."),
    # scientific
    _r("units_consistent", "scientific", "Units consistent", "Quantities are combined only in compatible units."),
    _r("numerical_stability", "scientific", "Numerical method checked", "Numerical results are checked for convergence or stability."),
    # marketing
    _r("attribution_limits", "marketing", "Attribution limits disclosed", "The attribution model's assumptions and blind spots are stated."),
    _r("conversion_definition", "marketing", "Conversion definition fixed", "Conversions use the governed metric definition across all compared groups.",
       "semantic layer: approved metrics"),
    # AI
    _r("eval_leakage", "ai", "No evaluation leakage", "Evaluation data is disjoint from data used to build or tune the system."),
    _r("eval_reproducible", "ai", "Evaluation reproducible", "Benchmarks record model, configuration, seed and cases so they can be re-run.",
       "experiments: recorded inputs and environment"),
    # documents / text
    _r("source_cited", "knowledge", "Sources cited", "Statements drawn from documents cite the passage they came from.", "knowledge: passages carry sources"),
    _r("extraction_checked", "knowledge", "Extraction checked", "Extracted tables are checked against the source page for row and column counts."),
    # human review
    _r("human_review", "governance", "Human review", "A person reviews the result before it is acted on or shared.", "runtime: approvals"),
]}


def describe() -> list[dict]:
    return [{"id": r.id, "title": r.title, "family": r.family, "description": r.description, "automated": r.automated,
             "enforced_by": r.enforced_by} for r in RULES.values()]
