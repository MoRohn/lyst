# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A labelled routing set and its measurement: does the router pick an acceptable discipline, and how fast?

Each case lists the specializations a reviewer would accept as the primary choice. ``measure`` reports top-1 (the
primary is acceptable) and any-hit (an acceptable discipline is among those selected), plus routing latency. The
test suite fails if accuracy drops below the recorded floor, so a catalog or router change that degrades routing is
caught as a regression.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

from .router import route

CASES: list[tuple[str, set[str]]] = [
    ("Why did revenue fall last quarter?", {"data_analyst", "financial_data_analyst", "fpa_analyst"}),
    ("Build a dashboard for weekly sales by region", {"business_intelligence_analyst"}),
    ("Draft requirements and acceptance criteria for the new order-to-cash report", {"business_analyst"}),
    ("Create a data contract for the invoices dataset and a cleaned derived table", {"analytics_engineer"}),
    ("How complete and valid is the customers dataset? Lots of nulls and duplicates", {"data_quality_analyst"}),
    ("Which datasets have no owner or classification in the glossary?", {"data_governance_analyst"}),
    ("Which datasets are late or shrinking unexpectedly after the schema drift?", {"data_observability_analyst"}),
    ("Is the difference in conversion between regions statistically significant?", {"statistical_analyst", "experimentation_analyst"}),
    ("Forecast revenue for the next two quarters", {"forecasting_analyst", "fpa_analyst"}),
    ("What effect did the March price change have on orders? Was it causal?", {"causal_inference_analyst", "pricing_analyst"}),
    ("Did variant B of the A/B test improve checkout conversion?", {"experimentation_analyst", "digital_experimentation_analyst"}),
    ("Simulate the range of outcomes for next year's profit with Monte Carlo", {"simulation_analyst"}),
    ("Which stores are more than 50 km from a warehouse? Map the distances", {"geospatial_analyst"}),
    ("When did the trend in weekly signups change? Is it seasonal?", {"time_series_analyst", "forecasting_analyst"}),
    ("Evaluate our agent workflow's task completion, tool selection and inference cost", {"agentic_ai_analyst", "agent_reliability_analyst", "ai_cost_inference_analyst"}),
    ("Did the new model configuration regress on our LLM evaluation set?", {"llm_evaluation_analyst"}),
    ("How resistant is our assistant to prompt injection and jailbreaks?", {"ai_safety_alignment_analyst"}),
    ("Which workloads could move to a cheaper model tier to cut token cost?", {"ai_cost_inference_analyst"}),
    ("Analyze this repository for architecture risks, test failures and performance bottlenecks", {"software_systems_analyst", "technical_debt_analyst", "software_qa_analyst"}),
    ("Which tests are flaky in CI over the last 30 days?", {"test_automation_analyst"}),
    ("How much error budget has the checkout service used against its SLO?", {"site_reliability_analyst"}),
    ("What is our change failure rate and deployment lead time (DORA)?", {"devops_analyst"}),
    ("Where do new users drop off in the onboarding funnel? Feature adoption and retention", {"product_analyst"}),
    ("Summarize our WCAG accessibility audit findings", {"digital_accessibility_analyst"}),
    ("Investigate increasing cloud expenses and recommend measurable resource optimizations", {"cloud_infrastructure_analyst", "cloud_finops_analyst"}),
    ("Why did our cloud bill rise 18% this month? Showback by team", {"cloud_finops_analyst"}),
    ("When will our database storage run out of capacity?", {"capacity_planning_analyst", "storage_data_infrastructure_analyst"}),
    ("Which Kubernetes workloads request far more CPU than they use?", {"kubernetes_container_analyst"}),
    ("Which categories breach SLA most often on the service desk?", {"it_service_management_analyst"}),
    ("Analyze authorized security telemetry and identify suspicious login patterns", {"cybersecurity_analyst", "soc_analyst"}),
    ("Which internet-facing assets have critical CVE vulnerabilities past SLA?", {"vulnerability_analyst"}),
    ("Which leavers still have active accounts? Access review of entitlements", {"iam_analyst"}),
    ("Which datasets contain personal data (PII) under GDPR?", {"privacy_data_protection_analyst"}),
    ("Which accounts show unusual transaction velocity that could be fraud?", {"fraud_detection_analyst"}),
    ("Explain our Q3 actuals versus budget", {"fpa_analyst"}),
    ("Investigate procurement expenditure variances with suppliers and reconcile multiple sources", {"procurement_analyst"}),
    ("Why did on-time delivery (OTIF) drop and lead times grow?", {"supply_chain_analyst"}),
    ("Which items have more than a year of inventory supply?", {"inventory_analyst"}),
    ("What is our attrition rate by department this year?", {"workforce_analytics_analyst"}),
    ("Find duplicate vendor payments and test approval controls", {"risk_controls_analyst"}),
    ("How much of the revenue change was price versus volume? Discount leakage", {"pricing_analyst"}),
    ("What is our pipeline coverage and win rate for next quarter?", {"revenue_operations_analyst"}),
    ("How much credit does paid social get under different attribution models?", {"marketing_attribution_analyst"}),
    ("Which pages lost the most organic search traffic and rankings?", {"search_seo_analyst"}),
    ("Break down this quarter's ARR change into new, expansion and churn", {"subscription_monetization_analyst"}),
    ("How has 90-day retention changed by signup cohort? Churn", {"customer_lifecycle_analyst"}),
    ("Which post formats get the highest engagement rate on social media?", {"social_media_analyst"}),
    ("Extract the tables from these supplier contract PDFs into a dataset", {"document_intelligence_analyst"}),
    ("Where do viewers drop off in our videos? Watch time and completion", {"video_analytics_analyst"}),
    ("Why did OEE on line 3 drop? Scrap and downtime", {"manufacturing_analytics_analyst"}),
    ("Which pumps show unusual vibration in sensor telemetry?", {"industrial_iot_analyst", "predictive_maintenance_analyst"}),
    ("Calculate our scope 2 emissions and ESG metrics", {"environmental_data_analyst"}),
    ("What is our 30-day readmission rate by hospital unit?", {"health_informatics_analyst"}),
    ("Which genes are differentially expressed in this RNA-seq data?", {"bioinformatics_analyst"}),
    ("Which processes are the best RPA automation candidates?", {"automation_rpa_analyst"}),
    ("Trace where funds from this wallet went on-chain", {"blockchain_analytics_analyst"}),
    ("What are customers complaining about most in support ticket comments? Themes", {"nlp_text_analytics_analyst", "customer_insights_analyst"}),
    ("Compare three vendors for build vs buy of our tool", {"technology_research_analyst"}),
]


# Paraphrases written after the vocabulary above, without its keywords. On first run the router got 5 of 10 right;
# the misses were fixed by adding vocabulary to the profiles, so this set is no longer unseen. A set written by
# someone other than the catalog's author is still needed for an honest accuracy figure (docs/SPECIALIZATIONS.md).
PARAPHRASES: list[tuple[str, set[str]]] = [
    ("Our AWS invoice jumped, which teams are responsible?", {"cloud_finops_analyst", "cloud_infrastructure_analyst"}),
    ("Are people leaving the company faster than last year?", {"workforce_analytics_analyst"}),
    ("Is our demand model getting worse over time?", {"model_performance_analyst", "mlops_analyst", "forecasting_analyst"}),
    ("Does this login activity look like a compromised account?", {"cybersecurity_analyst", "soc_analyst", "iam_analyst"}),
    ("How do we cut shipping delays to customers?", {"supply_chain_analyst", "transportation_mobility_analyst"}),
    ("Which customers are worth the most to us over their lifetime?", {"customer_lifecycle_analyst", "customer_insights_analyst"}),
    ("Our nightly loads keep missing rows from the CRM", {"data_integration_analyst", "data_observability_analyst", "data_quality_analyst"}),
    ("Are we paying suppliers more than the contract price?", {"procurement_analyst", "risk_controls_analyst"}),
    ("How long do tickets sit before someone picks them up?", {"it_service_management_analyst", "application_analyst"}),
    ("Is the chatbot actually resolving questions or just passing them to agents?", {"conversational_ai_analyst"}),
]


def measure(cases: list[tuple[str, set[str]]] | None = None) -> dict[str, Any]:
    cases = cases or CASES
    route("warm up the index")  # the first call builds the cached index; time steady-state routing
    top1 = hit = 0
    times, misses = [], []
    for text, ok in cases:
        t0 = time.perf_counter()
        d = route(text, can=lambda perm: True)
        times.append((time.perf_counter() - t0) * 1000)
        ids = [c.id for c in d.selected]
        if ids and ids[0] in ok:
            top1 += 1
        if set(ids) & ok:
            hit += 1
        else:
            misses.append({"request": text, "expected": sorted(ok), "selected": ids})
    n = len(cases)
    return {"cases": n, "top1": top1 / n, "any_hit": hit / n, "p50_ms": statistics.median(times),
            "p95_ms": sorted(times)[max(0, int(0.95 * n) - 1)], "max_ms": max(times), "misses": misses}
