# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Domain sample packs: one small governed dataset per discipline family, each with a planted, known answer.

A pack is a monthly panel (24 months to September 2026) of one measure split by one business dimension and a second,
neutral one. In the last month a single member of the business dimension moves by a planted factor (GPU spend
nearly doubles, the checkout service's incidents jump, organic sessions halve). Each pack is defined as a governed
metric, exactly as an administrator would define one, and carries the representative requests of the disciplines
that work on that kind of data. A request passes only if the discipline is selected, the answer is verified, and the
planted member is named as the driver: the pipeline must find the actual cause, not just return a number.

Everything here is synthetic. It exists so each discipline's representative task runs end to end under controlled
conditions (maturity Level 3); it says nothing about any real organization and is never loaded into a real workspace
except by the evidence and audit scripts and the tests.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MONTHS = pd.date_range("2024-10-01", "2026-09-01", freq="MS")


@dataclass(frozen=True)
class Pack:
    dataset: str
    metric: str
    title: str
    dim: str
    members: tuple[str, ...]
    planted: str
    factor: float
    scenarios: tuple[tuple[str, str], ...]          # (specialization id, request)
    fmt: str = "number"
    higher_is_better: bool = True
    synonyms: tuple[str, ...] = ()
    base: float = 1000.0
    other_dim: str = "region"
    others: tuple[str, ...] = ("North", "South")
    extra_facts: tuple[str, ...] = field(default_factory=tuple)


def _p(dataset, metric, title, dim, members, planted, factor, scenarios, **kw) -> Pack:
    return Pack(dataset, metric, title, dim, tuple(members), planted, factor, tuple(scenarios), **kw)


PACKS: list[Pack] = [
    # cloud and infrastructure
    _p("cloud_billing", "cloud_cost", "Cloud cost", "service", ["Compute", "Storage", "Database", "Network", "GPU"], "GPU", 2.4,
       [("cloud_finops_analyst", "Why did our cloud cost rise last month?"),
        ("cloud_infrastructure_analyst", "Which cloud services drove the increase in cloud infrastructure cost last month?")],
       fmt="currency", higher_is_better=False, synonyms=("cloud spend", "cloud bill", "aws bill", "infrastructure costs",
                                                         "cloud infrastructure cost", "infrastructure cost")),
    _p("gpu_fleet", "gpu_hours", "GPU hours", "workload", ["training", "inference", "notebooks", "batch"], "inference", 2.4,
       [("gpu_computing_analyst", "Why did GPU hours rise last month? Which workloads are using the accelerators?")],
       higher_is_better=False, synonyms=("gpu usage", "accelerator hours")),
    _p("cluster_usage", "cluster_cpu_cost", "Cluster CPU cost", "namespace", ["web", "batch", "ml-training", "monitoring"],
       "ml-training", 2.2, [("kubernetes_container_analyst", "Why did Kubernetes cluster CPU cost by namespace increase last month?")],
       fmt="currency", higher_is_better=False, synonyms=("kubernetes cost", "k8s cost")),
    _p("storage_usage", "storage_used_tb", "Storage used", "tier", ["hot", "warm", "archive", "backups"], "backups", 2.0,
       [("storage_data_infrastructure_analyst", "Why did storage used grow last month? Which storage tier?")],
       higher_is_better=False, synonyms=("storage growth", "storage capacity", "storage used tb")),
    _p("host_metrics", "cpu_saturation_hours", "CPU saturation hours", "host_group", ["api", "workers", "db", "cache"], "db", 2.5,
       [("infrastructure_performance_analyst", "Why did CPU saturation increase last month across host groups?")],
       higher_is_better=False, synonyms=("saturation", "cpu saturation")),
    _p("service_incidents", "incidents", "Incidents", "service", ["checkout", "search", "accounts", "catalog"], "checkout", 2.6,
       [("site_reliability_analyst", "Why did incidents increase last month? Which service hurt our SLO?"),
        ("systems_operations_analyst", "Which systems drove the rise in operational incidents and alerts last month?")],
       higher_is_better=False, synonyms=("outages", "incident count")),
    _p("service_desk", "service_desk_requests", "Service desk requests", "category", ["Access", "Hardware", "VPN", "Email"], "VPN", 2.3,
       [("it_service_management_analyst", "Why did service desk requests increase last month? Which ITSM category?")],
       higher_is_better=False, synonyms=("itsm volume", "help desk requests")),
    _p("db_workload", "query_seconds", "Database query time", "table_name", ["orders", "customers", "events", "invoices"], "orders",
       2.4, [("database_performance_analyst", "Why did database query time increase last month? Which table is slow?")],
       higher_is_better=False, synonyms=("query time", "slow queries")),
    # software delivery
    _p("deployments", "failed_deployments", "Failed deployments", "team", ["Payments", "Search", "Mobile", "Platform"], "Payments",
       2.5, [("devops_analyst", "Why did failed deployments rise last month? Check the DevOps release pipeline.")],
       higher_is_better=False, synonyms=("change failures", "deployment failures")),
    _p("ci_runs", "test_failures", "Test failures", "suite", ["unit", "integration", "e2e", "contract"], "integration", 2.6,
       [("test_automation_analyst", "Why did CI test failures increase last month? Which test suite is flaky?")],
       higher_is_better=False, synonyms=("flaky tests", "ci failures")),
    _p("defects", "escaped_defects", "Escaped defects", "component", ["Billing", "Search", "Profile", "Checkout"], "Billing", 2.5,
       [("software_qa_analyst", "Why did escaped defects rise last month? Which component has the QA problem?")],
       higher_is_better=False, synonyms=("production bugs", "defects")),
    _p("api_gateway", "api_errors", "API errors", "endpoint", ["/payments", "/search", "/users", "/orders"], "/payments", 2.8,
       [("api_integration_analyst", "Why did API errors spike last month? Which endpoint broke the integration?")],
       higher_is_better=False, synonyms=("error rate", "integration errors")),
    _p("app_latency", "slow_requests", "Slow requests", "endpoint", ["/search", "/cart", "/login", "/home"], "/search", 2.4,
       [("software_performance_analyst", "Why did slow requests and latency increase last month? Which endpoint is the bottleneck?")],
       higher_is_better=False, synonyms=("latency", "p95 latency")),
    _p("pull_requests", "pr_wait_hours", "Pull request wait hours", "team", ["Web", "Mobile", "Data", "Core"], "Mobile", 2.3,
       [("developer_productivity_analyst", "Why did pull request cycle time and review wait increase last month?")],
       higher_is_better=False, synonyms=("cycle time", "review latency")),
    _p("platform_usage", "golden_path_deploys", "Golden path deploys", "team", ["Web", "Mobile", "Data", "Core"], "Data", 0.4,
       [("platform_engineering_analyst", "Why did golden path deploys on the developer platform drop last month?")],
       synonyms=("platform adoption",)),
    _p("app_issues", "application_issues", "Application issues", "application", ["HR Portal", "Expenses", "Intranet", "CRM"],
       "HR Portal", 2.5, [("application_analyst", "Why did application issues rise last month after the release?")],
       higher_is_better=False, synonyms=("app issues",)),
    # product, marketing and commerce
    _p("product_usage", "active_users", "Active users", "feature", ["Search", "Reports", "Sharing", "Mobile"], "Search", 0.45,
       [("product_analyst", "Why did active users drop last month? Which feature lost adoption?")],
       synonyms=("feature adoption", "dau", "product usage")),
    _p("web_traffic", "web_sessions", "Web sessions", "channel", ["Organic", "Direct", "Referral", "Email"], "Organic", 0.45,
       [("web_analytics_analyst", "Why did web analytics sessions fall last month? Which traffic channel?")],
       synonyms=("website traffic", "sessions")),
    _p("search_console", "organic_clicks", "Organic search clicks", "page", ["/pricing", "/blog", "/docs", "/home"], "/pricing", 0.4,
       [("search_seo_analyst", "Why did organic search clicks fall last month? Which page lost SEO rankings?")],
       synonyms=("seo traffic",)),
    _p("ad_spend", "paid_media_spend", "Paid media spend", "channel", ["Paid Search", "Paid Social", "Display", "Video"], "Paid Social",
       2.3, [("paid_media_analyst", "Why did paid media spend rise last month? Which channel is pacing over?")],
       fmt="currency", higher_is_better=False, synonyms=("ad spend",)),
    _p("campaign_results", "campaign_conversions", "Campaign conversions", "campaign_channel", ["Email", "Search", "Social", "Affiliate"],
       "Email", 0.45, [("digital_marketing_analyst", "Why did marketing campaign conversions fall last month?")],
       synonyms=("marketing conversions",)),
    _p("shop_orders", "online_orders", "Online orders", "category", ["Shoes", "Bags", "Jackets", "Accessories"], "Shoes", 0.45,
       [("ecommerce_analyst", "Why did online store orders fall last month? Which category?")],
       synonyms=("ecommerce orders", "webshop orders", "online store orders", "store orders")),
    _p("subscriptions", "mrr", "MRR", "plan", ["Starter", "Team", "Business", "Enterprise"], "Enterprise", 1.9,
       [("subscription_monetization_analyst", "Why did subscription MRR rise last month? Which plan drove expansion?")],
       fmt="currency", synonyms=("monthly recurring revenue",)),
    _p("sales_pipeline", "pipeline_value", "Pipeline value", "segment", ["SMB", "Mid-market", "Enterprise", "Public"], "Mid-market",
       0.4, [("revenue_operations_analyst", "Why did sales pipeline value drop last month? Which segment?")],
       fmt="currency", synonyms=("pipeline",)),
    _p("customer_churn", "churned_customers", "Churned customers", "segment", ["SMB", "Mid-market", "Enterprise", "Consumer"], "SMB",
       2.4, [("customer_lifecycle_analyst", "Why did customer churn increase last month? Which segment?")],
       higher_is_better=False, synonyms=("customer churn", "lost customers")),
    _p("crm_contacts", "unsubscribes", "Email unsubscribes", "list_name", ["Newsletter", "Product", "Events", "Partners"], "Events",
       2.6, [("crm_analyst", "Why did CRM email unsubscribes rise last month? Which list?")],
       higher_is_better=False, synonyms=("opt-outs",)),
    _p("discounts", "discount_amount", "Discount amount", "deal_region", ["Americas", "EMEA", "APAC", "Japan"], "APAC", 2.4,
       [("pricing_analyst", "Why did discounts and discount leakage rise last month? Which region?")],
       fmt="currency", higher_is_better=False, synonyms=("discount leakage",)),
    # media and communications
    _p("social_metrics", "social_engagements", "Social engagements", "platform", ["Instagram", "LinkedIn", "TikTok", "YouTube"],
       "TikTok", 2.3, [("social_media_analyst", "Why did social media engagement rise last month? Which platform?")],
       synonyms=("engagement rate", "social media engagement", "social engagement")),
    _p("content_stats", "content_pageviews", "Content pageviews", "topic", ["AI", "Finance", "Health", "Travel"], "AI", 2.2,
       [("content_performance_analyst", "Why did content pageviews rise last month? Which topic performed?")],
       synonyms=("article views",)),
    _p("video_stats", "watch_hours", "Watch hours", "series", ["Tutorials", "Interviews", "Shorts", "Live"], "Tutorials", 0.45,
       [("video_analytics_analyst", "Why did video watch time drop last month? Which series?")],
       synonyms=("watch time",)),
    _p("podcast_stats", "podcast_downloads", "Podcast downloads", "show", ["Weekly Brief", "Deep Dive", "Founders", "Q&A"],
       "Weekly Brief", 2.2, [("audio_podcast_analyst", "Why did podcast downloads rise last month? Which show?")],
       synonyms=("listens",)),
    _p("media_audience", "media_audience", "Media audience", "property", ["News app", "Website", "Newsletter", "Radio"], "News app",
       0.45, [("digital_media_analyst", "Why did our digital media audience fall last month? Which property?")],
       synonyms=("audience reach",)),
    _p("media_mentions", "press_mentions", "Press mentions", "outlet_type", ["National", "Trade", "Local", "Blogs"], "National",
       2.8, [("media_monitoring_analyst", "Why did media coverage and press mentions spike last month?")],
       synonyms=("coverage", "mentions")),
    # operations and finance
    _p("purchase_orders", "procurement_spend", "Procurement spend", "supplier", ["Globex", "Initech", "Umbrella", "Stark"], "Globex",
       2.2, [("procurement_analyst", "Why did procurement spend with suppliers rise last month?")],
       fmt="currency", higher_is_better=False, synonyms=("supplier spend", "purchasing spend")),
    _p("shipments", "late_shipments", "Late shipments", "carrier", ["DHL", "UPS", "FedEx", "DPD"], "DHL", 2.6,
       [("supply_chain_analyst", "Why did late shipments and delivery delays increase last month? Which carrier?")],
       higher_is_better=False, synonyms=("late deliveries", "otif misses")),
    _p("stock_levels", "inventory_units", "Inventory units", "warehouse", ["Halle", "Berlin", "Hamburg", "Munich"], "Halle", 1.9,
       [("inventory_analyst", "Why did inventory stock levels rise last month? Which warehouse?")],
       higher_is_better=False, synonyms=("stock on hand", "inventory stock levels", "stock levels", "inventory levels")),
    _p("hr_leavers", "leavers", "Leavers", "department", ["Sales", "Engineering", "Support", "Finance"], "Sales", 2.6,
       [("workforce_analytics_analyst", "Why did employee attrition and leavers increase last month? Which department?")],
       higher_is_better=False, synonyms=("attrition", "turnover")),
    # industrial, health and environment
    _p("production_quality", "scrap_units", "Scrap units", "line", ["Line 1", "Line 2", "Line 3", "Line 4"], "Line 3", 2.6,
       [("manufacturing_analytics_analyst", "Why did manufacturing scrap increase last month? Which production line?")],
       higher_is_better=False, synonyms=("scrap", "defective units")),
    _p("sensor_alarms", "sensor_alarms", "Sensor alarms", "asset", ["Pump 7", "Chiller 2", "Boiler 1", "Fan 4"], "Pump 7", 2.8,
       [("industrial_iot_analyst", "Why did industrial sensor alarms in the telemetry spike last month? Which asset?")],
       higher_is_better=False, synonyms=("alarms",)),
    _p("energy_meters", "energy_kwh", "Energy consumption", "site", ["Plant A", "Plant B", "Office", "Warehouse"], "Plant B",
       2.0, [("energy_analytics_analyst", "Why did energy consumption rise last month? Which site?")],
       higher_is_better=False, synonyms=("energy use", "electricity")),
    _p("site_emissions", "co2_tonnes", "CO2 emissions", "site", ["Site A", "Site B", "Site C", "Site D"], "Site C", 2.3,
       [("environmental_data_analyst", "Why did CO2 emissions rise last month? Which site?")],
       higher_is_better=False, synonyms=("emissions", "carbon")),
    _p("hospital_stays", "readmissions", "Readmissions", "unit", ["Cardiology", "Oncology", "Surgery", "Pediatrics"], "Cardiology",
       2.5, [("health_informatics_analyst", "Why did hospital readmissions rise last month? Which unit?")],
       higher_is_better=False, synonyms=("30-day readmissions",)),
    _p("case_reports", "reported_cases", "Reported cases", "district", ["North", "East", "West", "Central"], "East", 2.7,
       [("epidemiological_data_analyst", "Why did reported disease cases rise last month? Which district?")],
       higher_is_better=False, synonyms=("incidence", "cases"), other_dim="age_band", others=("0-39", "40+")),
    _p("robot_fleet", "robot_interventions", "Robot interventions", "fleet", ["Fleet A", "Fleet B", "Fleet C", "Fleet D"], "Fleet B",
       2.5, [("robotics_autonomous_systems_analyst", "Why did robot interventions increase last month? Which fleet?")],
       higher_is_better=False, synonyms=("disengagements",)),
    # security
    _p("vuln_scans", "critical_vulnerabilities", "Critical vulnerabilities", "asset_group",
       ["Internet-facing", "Internal", "Endpoints", "Cloud"], "Internet-facing", 2.5,
       [("vulnerability_analyst", "Why did critical vulnerabilities increase last month? Which asset group?")],
       higher_is_better=False, synonyms=("critical cves", "open vulnerabilities")),
    _p("cloud_posture", "misconfigurations", "Cloud misconfigurations", "account", ["prod", "staging", "dev", "shared"], "prod", 2.6,
       [("cloud_security_analyst", "Why did cloud security misconfigurations rise last month? Which account?")],
       higher_is_better=False, synonyms=("posture findings",)),
    _p("access_grants", "privileged_grants", "Privileged access grants", "system", ["ERP", "CRM", "Cloud", "HR"], "ERP", 2.5,
       [("iam_analyst", "Why did privileged access grants increase last month? Which system needs an access review?")],
       higher_is_better=False, synonyms=("privileged access",)),
    _p("payments_risk", "chargebacks", "Chargebacks", "merchant", ["Merchant 12", "Merchant 42", "Merchant 7", "Merchant 99"],
       "Merchant 42", 2.8, [("fraud_detection_analyst", "Why did chargebacks and suspected fraud spike last month? Which merchant?")],
       higher_is_better=False, synonyms=("fraud losses",)),
    # AI and automation
    _p("model_calls", "model_spend", "Model inference spend", "workload", ["summarize", "classify", "extract", "chat"], "summarize",
       2.4, [("ai_cost_inference_analyst", "Why did model inference cost and token spend rise last month? Which workload?")],
       fmt="currency", higher_is_better=False, synonyms=("token spend", "llm cost")),
    _p("agent_runs", "agent_task_failures", "Agent task failures", "capability", ["run_sql", "forecast", "deliver", "profile"],
       "run_sql", 2.6, [("agent_reliability_analyst", "Why did agent task failures and retries increase last month?")],
       higher_is_better=False, synonyms=("task failures",)),
    _p("bot_sessions", "chatbot_escalations", "Chatbot escalations", "intent", ["billing", "shipping", "returns", "account"],
       "billing", 2.6, [("conversational_ai_analyst", "Why did chatbot escalations to agents rise last month? Which intent?")],
       higher_is_better=False, synonyms=("handoffs to agents",)),
    _p("rpa_runs", "bot_exceptions", "Bot exceptions", "process", ["Invoice processing", "Onboarding", "Payroll", "Claims"],
       "Invoice processing", 2.6, [("automation_rpa_analyst", "Why did RPA bot exceptions rise last month? Which automated process?")],
       higher_is_better=False, synonyms=("automation exceptions",)),
]


def frame(p: Pack) -> pd.DataFrame:
    rng = np.random.default_rng(zlib.crc32(p.dataset.encode()))
    rows = []
    for i, m in enumerate(MONTHS):
        season = 1 + 0.04 * np.sin(2 * np.pi * m.month / 12)
        for j, member in enumerate(p.members):
            level = p.base * (1 + 0.15 * j) * (1 + 0.005 * i) * season
            if member == p.planted and i == len(MONTHS) - 1:
                level *= p.factor
            for o in p.others:
                rows.append({"month": m.date(), p.dim: member, p.other_dim: o,
                             "value": round(level / len(p.others) * float(rng.uniform(0.98, 1.02)), 2)})
    return pd.DataFrame(rows).rename(columns={"value": p.metric})


def load_all(lyst, actor, workspace_id: str) -> list[str]:
    """Ingest every pack and define its governed metric."""
    from ..demo import csv_bytes  # noqa: PLC0415
    from ..semantic import Measure, MetricSpec  # noqa: PLC0415

    for p in PACKS:
        lyst.data.ingest(actor, workspace_id, p.dataset, csv_bytes(frame(p)), filename=f"{p.dataset}.csv",
                         title=p.dataset.replace("_", " ").title())
        lyst.semantic.define(actor, workspace_id, MetricSpec(
            name=p.metric, title=p.title, dataset=p.dataset, description=f"Sum of {p.title.lower()} (synthetic sample).",
            measure=Measure(agg="sum", column=p.metric), time_column="month", dimensions=[p.dim, p.other_dim],
            format=p.fmt, higher_is_better=p.higher_is_better, synonyms=list(p.synonyms)), note="sample pack")
    return [p.dataset for p in PACKS]


def scenarios() -> dict[str, list[tuple[str, list[str]]]]:
    """Representative requests per discipline, with the planted member as the fact the answer must name."""
    out: dict[str, list[tuple[str, list[str]]]] = {}
    for p in PACKS:
        for sid, request in p.scenarios:
            out.setdefault(sid, []).append((request, [p.planted, *p.extra_facts]))
    return out
