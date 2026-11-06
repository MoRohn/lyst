# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Representative tasks with known answers: the evidence behind Level 3 (Operational).

Each scenario is a request a practitioner of the discipline would ask, run end to end on the demo workspace plus the
synthetic samples (``samples.py``), with checks on the *content* of the answer: the planted pattern must be found
(EMEA drives the margin decline; the approval step is the bottleneck; user07 bursts; INV005 and INV010 break). A
scenario passes only when the discipline is among those selected, the task finishes with a verified answer and
every expected fact appears in it.

Scenarios come from two places: the hand-written ones below (on the demo data and ``samples.py``) and one per
discipline from the domain packs (``packs.py``), whose answers must name the planted cause.

``scripts/evidence_specializations.py --write`` runs them and records ``evidence.json``; the test suite re-runs them,
so a change that breaks a discipline's representative task fails CI instead of silently keeping its level.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .catalog import load
from .core import AnalystRequest

# profile id -> [(request, facts that must appear in the answer, evidence or tables)]
SCENARIOS: dict[str, list[tuple[str, list[str]]]] = {
    "financial_data_analyst": [("Why did operating margin decline last quarter?", ["operating margin", "EMEA"])],
    "data_analyst": [("What is our revenue by region last quarter?", ["North America", "EMEA", "APAC", "LATAM"])],
    "forecasting_analyst": [("Forecast revenue for the next two quarters", ["80% interval", "backtest"])],
    "business_intelligence_analyst": [("Build a dashboard for weekly revenue by region", ["Built", "verified"])],
    "statistical_analyst": [("Is the correlation between revenue and support tickets significant?", ["detrended", "not causation"])],
    "data_quality_analyst": [("Profile the pnl dataset for data quality", ["396 rows", "7 columns"])],
    "process_mining_analyst": [("Where does our order process get stuck? Use the order events.",
                                ["created → approved", "10 of 60 cases"])],
    "financial_systems_analyst": [("Reconcile ledger erp against ledger bank",
                                   ["do not agree", "1 only in ledger_erp", "1 only in ledger_bank", "INV005"])],
    "crm_analyst": [("Segment our customers into groups", ["3 segments", "support_tickets"])],
    "predictive_analytics_analyst": [("Which customers are likely to churn?", ["AUC", "support_tickets"])],
    "cybersecurity_analyst": [("Analyze the login events for suspicious patterns", ["1 of 12", "user07"])],
    "geospatial_analyst": [("Which stores are more than 300 km from a warehouse?", ["2 of 6", "Freiburg", "Munich"])],
    # experiments on the samples
    "experimentation_analyst": [("Did variant B of the checkout experiment improve conversion?", ["B is higher than A", "p ="])],
    "digital_experimentation_analyst": [("Read out the checkout experiment A/B test: did the variant win?", ["B is higher than A"])],
    "marketing_attribution_analyst": [("Did the email campaign drive incremental purchases versus the holdout in the email holdout "
                                       "test?", ["treated is higher than holdout"])],
    "clinical_data_analyst": [("Compare adverse events between the treatment and control arm in the clinical trial",
                               ["treatment is lower than control", "p ="])],
    "scientific_computing_analyst": [("Run a reproducible statistical comparison of the checkout experiment groups: is the "
                                      "difference significant?", ["B is higher than A", "95% interval"])],
    # evaluation results on the samples
    "model_performance_analyst": [("Evaluate the model predictions accuracy by segment", ["Enterprise"])],
    "llm_evaluation_analyst": [("Evaluate the llm eval results pass rate by model", ["model-b"])],
    "rag_retrieval_analyst": [("Evaluate the retrieval eval results: how often is the right passage retrieved, by query type?",
                               ["numeric"])],
    "ai_safety_alignment_analyst": [("Evaluate the AI safety eval results: jailbreak and prompt injection pass rate by harm "
                                     "category", ["prompt injection"])],
    "speech_audio_ai_analyst": [("Evaluate the transcription eval results accuracy by accent", ["Scottish"])],
    "multimodal_ai_analyst": [("Evaluate the multimodal eval results by modality", ["image"])],
    # data roles: profiling and quality on named datasets
    "data_modeling_analyst": [("Profile the order_events dataset structure, keys and grain", ["order_events has", "rows"])],
    "master_data_analyst": [("Check the customers master data for duplicates and nulls", ["customers has 240 rows"])],
    "data_warehouse_analyst": [("Profile the shop_orders warehouse table", ["shop_orders has", "rows"])],
    "data_observability_analyst": [("Profile the api_gateway dataset freshness and data quality", ["api_gateway has", "rows"])],
    "reporting_analyst": [("Produce this month's management report of revenue by region", ["North America", "EMEA"])],
    "audience_intelligence_analyst": [("Which audience segments do we have? Segment our customers", ["3 segments"])],
    # logs, graphs, text, optimization and classification on the samples
    "soc_analyst": [("Triage the raw auth logs: any brute force or suspicious logins?", ["admin", "8 failures"])],
    "digital_forensics_analyst": [("Reconstruct a timeline from the raw auth logs for the account that was guessed",
                                   ["admin", "8 failures"])],
    "network_analyst": [("Analyze the firewall logs for unusual traffic bursts by source", ["10.0.0.66"])],
    "distributed_systems_analyst": [("Analyze the service logs: which service had a failure burst?", ["payments"])],
    "blockchain_analytics_analyst": [("Trace the wallet transfers network: which wallet connects the most?", ["0xHUB"])],
    "knowledge_graph_analyst": [("Which entity is the main broker in the service dependencies graph?", ["auth-service"])],
    "nlp_text_analytics_analyst": [("What are the main themes and sentiment in the ticket comments?", ["refund"])],
    "optimization_analyst": [("Optimize our budget of 100,000 across the channel options for the best return", ["Search"])],
    "operations_research_analyst": [("Operations research: allocate a budget of 100k across the channel options to maximize "
                                     "return", ["Search"])],
    "machine_learning_analyst": [("Train a classifier on the customers dataset to predict churned", ["AUC", "support_tickets"])],
    "predictive_maintenance_analyst": [("Predict the failure mode from the maintenance history: classify failure_mode",
                                        ["3 classes", "accuracy"])],
    "bioinformatics_analyst": [("Classify condition from the gene expression samples", ["gene_brca1"])],
    "computer_vision_analyst": [("Evaluate the detection results accuracy by class group", ["pedestrian"])],
    "engineering_simulation_analyst": [("Evaluate the rig tests: how well do simulation predictions match measured results by "
                                        "load case?", ["torsion"])],
    "digital_twin_analyst": [("Evaluate the twin vs sensor results: where does the digital twin diverge from measurements?",
                              ["Chiller 2"])],
    "mlops_analyst": [("MLOps model monitoring: evaluate the deployed model predictions for drift by segment", ["Enterprise"])],
    "causal_inference_analyst": [("What was the causal impact of the March 2026 price change on revenue? Estimate the effect "
                                  "since March 2026", ["pre-event trend", "95% interval"])],
    # existing analysis paths on the demo data and the packs
    "time_series_analyst": [("When did the trend in revenue change over time? Any anomalies?", ["anomaly"])],
    "fpa_analyst": [("Build a rolling forecast of operating income for the FP&A plan, next two quarters", ["80% interval"])],
    "capacity_planning_analyst": [("Forecast GPU hours capacity for the next two quarters", ["80% interval"])],
    "simulation_analyst": [("Run a scenario simulation: what if labor costs increase 6% while revenue grows 3%? Show the "
                            "operating margin",
                            ["hypothetical", "labor costs +6%"])],
    "decision_intelligence_analyst": [("Decision: should we accept it if labor costs increase 6% while revenue grows 3%? "
                                       "Compare the options for operating margin", ["hypothetical", "labor costs +6%"])],
    "data_integration_analyst": [("Reconcile ledger erp against ledger bank after the sync", ["INV005", "INV010"])],
    "erp_systems_analyst": [("Reconcile the ledger erp postings against the ledger bank", ["INV005", "INV010"])],
    "risk_controls_analyst": [("Test controls: reconcile ledger erp and ledger bank and list the exceptions", ["INV005"])],
    "customer_insights_analyst": [("Segment our customers into groups by value and behaviour", ["3 segments"])],
    "growth_analyst": [("Segment our customers to find growth opportunities for acquisition and activation", ["3 segments"])],
    "agentic_ai_analyst": [("Why did agent task failures increase last month? Which tool did the agent select?", ["run_sql"])],
    # questions about the workspace's own documents (extractive briefs)
    "data_governance_analyst": [("What does our data retention policy say about customer records?", ["seven years"])],
    "privacy_data_protection_analyst": [("What does our privacy deletion standard say about deleting personal data?", ["30 days"])],
    "ai_governance_analyst": [("What does our AI use policy require before deploying a model?", ["impact assessment"])],
    "security_compliance_analyst": [("Which SOC 2 controls lack current evidence according to the control review?", ["CC6.1"])],
    "threat_intelligence_analyst": [("What does the threat report say about phishing campaigns?", ["invoice-themed"])],
    "news_information_analyst": [("Brief me on what our documents say about the Data Act regulation", ["1 March 2027"])],
    "communications_analyst": [("What did the staff communications survey say about the internal newsletter?", ["41%"])],
    "document_intelligence_analyst": [("What do our supplier contracts say about payment terms?", ["net 45"])],
    "technology_research_analyst": [("What did our vector database evaluation recommend?", ["pgvector"])],
    "quantum_computing_analyst": [("What does our quantum readiness note say about RSA-2048?", ["ML-KEM"])],
    "digital_transformation_analyst": [("What benefits did the onboarding program business case promise?", ["20%"])],
    "user_experience_analyst": [("Summarize the findings from the usability study notes", ["address form"])],
    "digital_accessibility_analyst": [("Summarize our accessibility audit findings", ["contrast"])],
    "business_analyst": [("What do the business requirements for the order to cash report say?", ["DSO"])],
    "ai_systems_analyst": [("What does the AI system architecture review of the support assistant say about failure modes?",
                            ["scanned PDFs"])],
    "prompt_context_analyst": [("What do our prompt guidelines recommend for few-shot prompts?", ["three examples"])],
    "generative_ai_analyst": [("What did the generative AI use case assessment recommend?", ["Contract summarization"])],
    "security_risk_analyst": [("What does our security risk register say exceeds our risk appetite?", ["Ransomware"])],
}


def all_scenarios() -> dict[str, list[tuple[str, list[str]]]]:
    """The hand-written scenarios above plus one per discipline from the domain sample packs (packs.py)."""
    from .packs import scenarios as pack_scenarios  # noqa: PLC0415

    out = {k: list(v) for k, v in SCENARIOS.items()}
    for pid, rows in pack_scenarios().items():
        out.setdefault(pid, []).extend(rows)
    return out


def _text(task: dict[str, Any]) -> str:
    res = task.get("result") or {}
    parts = [res.get("answer") or "", *(res.get("evidence") or [])]
    for t in res.get("tables") or []:
        parts += [" ".join(str(c) for c in row) for row in t.get("rows") or []]
    return "\n".join(str(p) for p in parts)


def run(lyst, actor, workspace_id: str, ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    scen = all_scenarios()
    for pid in ids or list(scen):
        rows = []
        for request, facts in scen[pid]:
            resp = lyst.analyst_core.handle(actor, AnalystRequest(workspace_id=workspace_id, text=request))
            lyst.runtime.drain()
            task = lyst.runtime.get(actor, resp.task_id)
            text = _text(task)
            selected = [r["id"] for r in resp.expertise.get("roles") or []]
            checks = [{"check": "selected", "passed": pid in selected, "detail": ", ".join(selected) or "general"},
                      {"check": "verified answer", "passed": task.get("outcome") == "complete", "detail": str(task.get("outcome"))}]
            checks += [{"check": f"finds: {f}", "passed": f.lower() in text.lower(), "detail": ""} for f in facts]
            rows.append({"request": request, "passed": all(c["passed"] for c in checks), "checks": checks})
        out[pid] = {"passed": all(r["passed"] for r in rows), "scenarios": rows}
    return out


def evidence_entries(results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The ``evidence.json`` content for the scenarios that passed: Level 3 is recorded only for those."""
    version = load().version
    return {pid: {"operational": {"recorded": date.today().isoformat(), "catalog_version": version,
                                  "scenarios": [s["request"] for s in r["scenarios"]]}}
            for pid, r in sorted(results.items()) if r["passed"]}
