# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Deterministic sample datasets with known answers, used to test the specialist methods end to end.

Each generator plants a pattern the matching method must find (a slow approval step, one account's login burst, two
stores far from any warehouse, three ledger breaks, two connected supplier groups, three customer segments, churn
driven by support tickets). Tests assert the method finds exactly the planted pattern; the audit uses the same data
to give profiles representative tasks. The data is synthetic and says nothing about any real organization.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def order_events(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    t0 = pd.Timestamp("2026-09-01 08:00", tz="UTC")
    for i in range(n):
        t = t0 + pd.Timedelta(hours=int(rng.integers(0, 600)))
        steps = ["created", "approved", "shipped", "invoiced", "paid"]
        if i % 6 == 0:
            steps = ["created", "approved", "rejected", "approved", "shipped", "invoiced", "paid"]  # rework
        for s in steps:
            rows.append({"order_id": f"O{i:03d}", "activity": s, "event_time": t})
            # approval is the planted bottleneck: about two days of waiting; other steps take hours
            t = t + pd.Timedelta(hours=float(rng.uniform(40, 56)) if s == "created" else float(rng.uniform(1, 8)))
    return pd.DataFrame(rows)


def logins(n_users: int = 12, hours: int = 72) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    rows = []
    t0 = pd.Timestamp("2026-10-01", tz="UTC")
    for u in range(n_users):
        for h in range(hours):
            for _ in range(int(rng.poisson(2))):
                rows.append({"username": f"user{u:02d}", "event": "login_success",
                             "occurred_at": t0 + pd.Timedelta(hours=h, minutes=int(rng.integers(0, 60)))})
    for m in range(60):  # planted: one account, sixty failed logins inside one hour
        rows.append({"username": "user07", "event": "login_failed", "occurred_at": t0 + pd.Timedelta(hours=50, minutes=m)})
    rows.append({"username": "user03", "event": "password_reset", "occurred_at": t0 + pd.Timedelta(hours=10)})
    return pd.DataFrame(rows)


def stores() -> pd.DataFrame:
    return pd.DataFrame([
        {"store": "Berlin Mitte", "lat": 52.520, "lon": 13.405}, {"store": "Potsdam", "lat": 52.391, "lon": 13.064},
        {"store": "Hamburg", "lat": 53.551, "lon": 9.994}, {"store": "Leipzig", "lat": 51.340, "lon": 12.375},
        {"store": "Munich", "lat": 48.137, "lon": 11.575}, {"store": "Freiburg", "lat": 47.999, "lon": 7.842},
    ])


def warehouses() -> pd.DataFrame:
    return pd.DataFrame([{"warehouse": "Berlin DC", "lat": 52.45, "lon": 13.30},
                         {"warehouse": "Hamburg DC", "lat": 53.50, "lon": 10.10},
                         {"warehouse": "Halle DC", "lat": 51.48, "lon": 11.97}])


def ledgers() -> tuple[pd.DataFrame, pd.DataFrame]:
    erp = pd.DataFrame({"invoice_id": [f"INV{i:03d}" for i in range(40)], "amount": [100.0 + 10 * i for i in range(40)]})
    bank = erp.copy()
    bank.loc[bank["invoice_id"] == "INV005", "amount"] += 25.0       # an amount break
    bank = bank[bank["invoice_id"] != "INV010"]                       # missing from the bank
    bank = pd.concat([bank, pd.DataFrame({"invoice_id": ["INV999"], "amount": [80.0]})])  # only in the bank
    return erp.rename(columns={"amount": "amount"}), bank


def supplier_links() -> pd.DataFrame:
    a = [("Acme", "Bolt Co"), ("Acme", "Cogs Ltd"), ("Bolt Co", "Dyna"), ("Cogs Ltd", "Dyna"), ("Acme", "Dyna")]
    b = [("Ever", "Flux"), ("Flux", "Gear")]
    return pd.DataFrame(a + b, columns=["source", "target"])


def customers(n: int = 240) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    centers = [(200.0, 2.0, 1.0), (1200.0, 12.0, 2.0), (500.0, 5.0, 9.0)]  # spend, orders, support tickets
    rows = []
    for i in range(n):
        c = centers[i % 3]
        spend, orders, tickets = (max(0.0, rng.normal(c[0], c[0] * 0.08)), max(0.0, rng.normal(c[1], 1.0)),
                                  max(0.0, rng.normal(c[2], 0.8)))
        p = 1 / (1 + np.exp(-(tickets - 5) * 1.2))  # churn driven by support tickets
        rows.append({"customer_id": f"C{i:04d}", "annual_spend": round(spend, 2), "orders": round(orders, 1),
                     "support_tickets": round(tickets, 1), "signup_date": pd.Timestamp("2025-01-01") + pd.Timedelta(days=i),
                     "churned": int(rng.random() < p)})
    return pd.DataFrame(rows)


def experiment(n: int = 2000, *, groups=("A", "B"), lift: float = 0.03, base: float = 0.10, seed: int = 5,
               group_col: str = "variant", outcome: str = "converted") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        g = groups[i % 2]
        p = base + (lift if g == groups[1] else 0.0)
        rows.append({"user_id": f"U{i:05d}", group_col: g, outcome: int(rng.random() < p)})
    return pd.DataFrame(rows)


def eval_results(name: str, segment: str, segments: list[str], weakest: str, *, n: int = 400, seed: int = 21) -> pd.DataFrame:
    """Pass/fail evaluation results where one segment is planted to be clearly weakest."""
    rng = np.random.default_rng(seed + len(name))
    rows = []
    for i in range(n):
        seg = segments[i % len(segments)]
        p = 0.55 if seg == weakest else 0.9
        rows.append({"case_id": f"{name[:3]}{i:04d}", segment: seg, "passed": int(rng.random() < p)})
    return pd.DataFrame(rows)


def predictions(n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(31)
    rows = []
    for i in range(n):
        seg = ["SMB", "Mid-market", "Enterprise"][i % 3]
        actual = ["renew", "churn"][int(rng.random() < 0.3)]
        wrong = rng.random() < (0.4 if seg == "Enterprise" else 0.08)
        pred = ("churn" if actual == "renew" else "renew") if wrong else actual
        rows.append({"account_id": f"A{i:04d}", "segment": seg, "actual": actual, "predicted": pred})
    return pd.DataFrame(rows)


def clinical_trial(n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(41)
    return pd.DataFrame([{"subject_id": f"S{i:04d}", "arm": ["control", "treatment"][i % 2],
                          "adverse_event": int(rng.random() < (0.22 if i % 2 == 0 else 0.10))} for i in range(n)])


EVALS = {  # dataset -> (segment column, segments, planted weakest)
    "llm_eval_results": ("model", ["model-a", "model-b", "model-c"], "model-b"),
    "retrieval_eval_results": ("query_type", ["keyword", "numeric", "paraphrase"], "numeric"),
    "safety_eval_results": ("harm_category", ["prompt injection", "data leakage", "unsafe advice"], "prompt injection"),
    "transcription_eval_results": ("accent", ["Scottish", "Texan", "Received"], "Scottish"),
    "multimodal_eval_results": ("modality", ["image", "audio", "table"], "image"),
}


# title -> text. Each document plants one fact the matching discipline's representative request must surface.
DOCUMENTS: dict[str, str] = {
    "Data Retention Policy": "Customer records are retained for seven years after the end of the relationship. "
        "Application logs are kept for 13 months. Backups follow the retention of the data they contain.",
    "Privacy Deletion Standard": "Personal data is deleted within 30 days of a verified deletion request. "
        "Deletion covers backups at their next rotation. Requests are logged with the date of completion.",
    "AI Use Policy": "Before deploying a model, the owner completes an AI impact assessment and records the model card. "
        "Customer-facing outputs need human review until the evaluation pass rate exceeds the agreed threshold.",
    "SOC 2 Control Review 2026": "Control CC6.1 (logical access) lacks current evidence: the last access review is from 2025. "
        "Control CC7.2 (monitoring) passed with evidence from September 2026.",
    "Threat Report October 2026": "An invoice-themed phishing campaign is targeting finance teams with fake supplier payment "
        "changes. Indicators include look-alike supplier domains registered in the last 30 days.",
    "Regulation Brief Data Act": "The Data Act applies from 1 March 2027. Connected-product data must be made available to "
        "users on request, and switching between cloud providers must be supported.",
    "Staff Communications Survey": "Only 41% of staff read the internal newsletter regularly, while the team chat channel reaches "
        "78% of frontline staff within a day.",
    "Supplier Master Agreement Globex": "Payment terms are net 45 days from a valid invoice. Price changes need 60 days notice "
        "and written approval from procurement.",
    "Vector Database Evaluation": "After benchmarking three options, the team recommends pgvector inside the existing Postgres "
        "service: recall was within 2% of the dedicated engines at a fraction of the cost.",
    "Quantum Readiness Note": "RSA-2048 is used by the payments gateway and the partner VPN. Both must migrate to ML-KEM "
        "key exchange by 2029 because the data they protect stays sensitive for more than ten years.",
    "Onboarding Program Business Case": "The digital onboarding program promised 20% faster customer onboarding and a 15% "
        "reduction in manual document checks within the first year.",
    "Usability Study Notes": "Five of eight participants struggled with the checkout address form; postcode validation errors "
        "were the most common friction point. Search was praised.",
    "Accessibility Audit Summary": "The audit found 14 WCAG 2.2 AA issues. The most frequent was insufficient colour contrast on "
        "primary buttons, followed by missing form labels on the signup page.",
    "Order to Cash Report Requirements": "The order-to-cash report must show DSO by region weekly, flag invoices overdue by "
        "more than 30 days, and reconcile to the ledger at month end.",
    "Support Assistant Architecture Review": "The support assistant's retrieval step fails on scanned PDFs, which have no text "
        "layer, so answers about older contracts lack citations. Escalation to a person works as designed.",
    "Prompt Guidelines": "Keep system prompts under 1,200 tokens. Use three examples for few-shot prompts, and put retrieved "
        "documents after the instructions, fenced as data.",
    "GenAI Use Case Assessment": "Contract summarization has the highest value for its cost; marketing image generation was "
        "deprioritized because of brand review effort.",
    "Security Risk Register": "Ransomware is the top risk and exceeds our risk appetite; third-party access risk is within "
        "appetite after the vendor review.",
}


def load_all(lyst, actor, workspace_id: str) -> list[str]:
    """Ingest every sample into a workspace; returns the dataset names."""
    from ..demo import csv_bytes  # noqa: PLC0415

    erp, bank = ledgers()
    frames = {"order_events": order_events(), "login_events": logins(), "stores": stores(), "warehouses": warehouses(),
              "ledger_erp": erp, "ledger_bank": bank, "supplier_links": supplier_links(), "customers": customers(),
              "checkout_experiment": experiment(),
              "model_predictions": predictions(), "clinical_trial": clinical_trial(),
              **{name: eval_results(name, *spec) for name, spec in EVALS.items()},
              "email_holdout": experiment(4000, groups=("holdout", "treated"), lift=0.03, base=0.05, seed=9, group_col="group",
                                          outcome="purchased")}
    for name, df in frames.items():
        lyst.data.ingest(actor, workspace_id, name, csv_bytes(df), filename=f"{name}.csv", title=name.replace("_", " ").title())
    if getattr(lyst, "knowledge", None) is not None:
        for title, text in DOCUMENTS.items():
            lyst.knowledge.add_document(actor, workspace_id, text.encode(), filename=title.lower().replace(" ", "_") + ".md",
                                        title=title)
    return list(frames)
