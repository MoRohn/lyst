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


def predictions_cv(n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(43)
    classes = ["car", "pedestrian", "cyclist"]
    rows = []
    for i in range(n):
        c = classes[i % 3]
        wrong = rng.random() < (0.35 if c == "pedestrian" else 0.06)
        p = classes[(classes.index(c) + 1) % 3] if wrong else c
        rows.append({"image_id": f"I{i:04d}", "class_group": c, "actual": c, "predicted": p})
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


def raw_auth_logs() -> pd.DataFrame:
    """Mixed-format raw lines: syslog sshd, JSON app events and web access logs. Planted: admin is guessed from
    203.0.113.9 (eight failures, then a success)."""
    lines = []
    t0 = pd.Timestamp("2026-10-12 08:00", tz="UTC")
    for i in range(40):
        u = f"user{i % 6}"
        ts = t0 + pd.Timedelta(minutes=17 * i)
        lines.append(f"{ts:%b %d %H:%M:%S} bastion sshd[{1000 + i}]: Accepted password for {u} from 10.0.0.{10 + i % 6} port 22")
        lines.append(_json_line({"ts": (ts + pd.Timedelta(minutes=3)).isoformat(), "event": "login_success", "user": u}))
        lines.append(f'10.0.0.{10 + i % 6} - {u} [{ts:%d/%b/%Y:%H:%M:%S +0000}] "GET /app HTTP/1.1" 200 512')
    t1 = pd.Timestamp("2026-10-12 23:10", tz="UTC")
    for k in range(8):
        lines.append(f"{t1 + pd.Timedelta(minutes=k):%b %d %H:%M:%S} bastion sshd[9{k}]: Failed password for admin from "
                     "203.0.113.9 port 22")
    lines.append(f"{t1 + pd.Timedelta(minutes=9):%b %d %H:%M:%S} bastion sshd[99]: Accepted password for admin from 203.0.113.9 port 22")
    lines.append("garbage line that matches no format")
    return pd.DataFrame({"line": lines})


def _json_line(obj: dict) -> str:
    import json  # noqa: PLC0415

    return json.dumps(obj)


def firewall_logs() -> pd.DataFrame:
    """key=value firewall lines; planted: 10.0.0.66 sends a burst of denied connections in one hour."""
    rng = np.random.default_rng(13)
    lines = []
    t0 = pd.Timestamp("2026-10-01", tz="UTC")
    for h in range(72):
        for src in ("10.0.0.21", "10.0.0.22", "10.0.0.23", "10.0.0.66"):
            for _ in range(int(rng.poisson(3))):
                ts = t0 + pd.Timedelta(hours=h, minutes=int(rng.integers(0, 60)))
                lines.append(f"ts={ts.isoformat()} src={src} dst=10.1.0.5 action=allow bytes={int(rng.integers(100, 9000))}")
    for m in range(90):
        ts = t0 + pd.Timedelta(hours=40, minutes=m % 60)
        lines.append(f"ts={ts.isoformat()} src=10.0.0.66 dst=10.1.0.{m % 50} action=deny bytes=60")
    return pd.DataFrame({"line": lines})


def service_logs() -> pd.DataFrame:
    rng = np.random.default_rng(17)
    rows = []
    t0 = pd.Timestamp("2026-10-05", tz="UTC")
    for h in range(96):
        for svc in ("gateway", "orders", "payments", "inventory"):
            for _ in range(int(rng.poisson(2))):
                rows.append({"occurred_at": t0 + pd.Timedelta(hours=h, minutes=int(rng.integers(0, 60))), "service": svc,
                             "event": "error" if rng.random() < 0.05 else "ok"})
    for m in range(70):  # planted: payments fails in a burst, then the callers fail after it
        rows.append({"occurred_at": t0 + pd.Timedelta(hours=60, minutes=m % 60), "service": "payments", "event": "error"})
    return pd.DataFrame(rows)


def wallet_transfers() -> pd.DataFrame:
    hub = "0xHUB"
    edges = [(f"0xA{i}", hub) for i in range(6)] + [(hub, f"0xB{i}") for i in range(5)] + [("0xA1", "0xA2"), ("0xB1", "0xB2"),
                                                                                         ("0xC1", "0xC2")]
    return pd.DataFrame(edges, columns=["from_wallet", "to_wallet"])


def service_dependencies() -> pd.DataFrame:
    edges = [("web", "auth-service"), ("mobile", "auth-service"), ("auth-service", "users-db"), ("auth-service", "sessions"),
             ("checkout", "auth-service"), ("checkout", "payments"), ("payments", "ledger"), ("search", "catalog")]
    return pd.DataFrame(edges, columns=["source", "target"])


def ticket_comments(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(23)
    templates = {"refund": ["I want a refund, I was charged twice", "Still waiting for my refund, terrible",
                            "refund request ignored, very frustrated"],
                 "delivery": ["Delivery was late again", "package missing, delivery problem"],
                 "praise": ["Great help, thanks, resolved fast", "Support was excellent and helpful"]}
    rows = []
    for i in range(n):
        kind = "refund" if i % 2 == 0 else ("delivery" if i % 3 == 0 else "praise")
        rows.append({"ticket_id": f"T{i:04d}", "channel": ["email", "chat", "phone"][i % 3],
                     "comment": templates[kind][int(rng.integers(0, len(templates[kind])))] + f" (order {1000 + i})"})
    return pd.DataFrame(rows)


def channel_options() -> pd.DataFrame:
    return pd.DataFrame([
        {"channel": "Search", "cost_per_unit": 50.0, "return_per_unit": 180.0, "max_units": 600},
        {"channel": "Social", "cost_per_unit": 40.0, "return_per_unit": 90.0, "max_units": 1000},
        {"channel": "Email", "cost_per_unit": 5.0, "return_per_unit": 30.0, "max_units": 2000},
        {"channel": "Display", "cost_per_unit": 30.0, "return_per_unit": 35.0, "max_units": 1000},
    ])


def maintenance_history(n: int = 360) -> pd.DataFrame:
    rng = np.random.default_rng(29)
    rows = []
    for i in range(n):
        mode = ["bearing", "overheating", "electrical"][i % 3]
        vib = rng.normal(6 if mode == "bearing" else 4, 1.5)
        temp = rng.normal(82 if mode == "overheating" else 74, 6)
        amps = rng.normal(23 if mode == "electrical" else 19, 3)
        rows.append({"asset_id": f"P{i:04d}", "vibration": round(vib, 2), "temperature": round(temp, 1), "current_amps": round(amps, 1),
                     "failure_mode": mode, "recorded_at": pd.Timestamp("2025-01-01") + pd.Timedelta(days=i)})
    return pd.DataFrame(rows)


def gene_expression(n: int = 160) -> pd.DataFrame:
    rng = np.random.default_rng(37)
    rows = []
    for i in range(n):
        cond = ["healthy", "disease"][i % 2]
        rows.append({"sample_id": f"S{i:03d}", "gene_brca1": round(rng.normal(5 if cond == "disease" else 3, 0.7), 3),
                     "gene_tp53": round(rng.normal(4, 0.8), 3), "gene_egfr": round(rng.normal(6, 0.8), 3), "condition": cond})
    return pd.DataFrame(rows)


def regression_results(name: str, segment: str, segments: list[str], worst: str, n: int = 240) -> pd.DataFrame:
    """Predicted vs measured values where one segment is planted to err most."""
    rng = np.random.default_rng(len(name) * 7)
    rows = []
    for i in range(n):
        seg = segments[i % len(segments)]
        actual = float(rng.uniform(50, 150))
        noise = rng.normal(0, 18 if seg == worst else 3)
        rows.append({"run_id": f"R{i:04d}", segment: seg, "actual": round(actual, 2), "predicted": round(actual + noise, 2)})
    return pd.DataFrame(rows)


APP_REPO = {
    "app/billing.py": """import subprocess
import sqlite3


def reconcile_invoices(rows, ledger, mode):
    total = 0
    for r in rows:
        if r["status"] == "open":
            if r["amount"] > 0:
                if mode == "strict":
                    for l in ledger:
                        if l["id"] == r["id"] and l["amount"] != r["amount"]:
                            total += 1
                        elif l["id"] == r["id"]:
                            continue
                elif mode == "loose" and r.get("flag"):
                    total += 2
                else:
                    total += 0
            elif r["amount"] < 0 or r.get("refund"):
                total -= 1
        elif r["status"] == "void" and r.get("note"):
            total += 0
    return total


def export(path):
    subprocess.run("tar czf " + path + " /data", shell=True)


def find_customer(conn, name):
    return conn.execute(f"SELECT * FROM customers WHERE name = '{name}'").fetchall()
""",
    "app/pricing.py": """def net_price(price, discount):
    return price - price * discount


def gross_price(price, tax):
    return price + price * tax
""",
    "app/test_pricing.py": """from pricing import net_price, gross_price


def test_net_price():
    assert net_price(100, 0.1) == 90


def test_gross_price():
    assert gross_price(100, 0.2) == 125
""",
}

VAULT_SOL = """pragma solidity ^0.8.0;

contract Vault {
    mapping(address => uint256) public balances;
    address public owner;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok);
        balances[msg.sender] -= amount;
    }

    function sweep(address to) external {
        require(tx.origin == owner);
        payable(to).transfer(address(this).balance);
    }
}
"""


def code_frames() -> dict[str, pd.DataFrame]:
    return {"app_repo": pd.DataFrame([{"path": k, "source": v} for k, v in APP_REPO.items()]),
            "app_repo_history": pd.DataFrame([{"path": "app/billing.py", "changes": 48, "defects": 9},
                                              {"path": "app/pricing.py", "changes": 6, "defects": 1}]),
            "contracts_repo": pd.DataFrame([{"path": "contracts/Vault.sol", "source": VAULT_SOL}])}


def sales_regions() -> pd.DataFrame:
    def box(w, s, e, n):
        return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}

    import json  # noqa: PLC0415

    return pd.DataFrame([{"region": "North", "geometry": json.dumps(box(8.0, 52.0, 15.0, 55.0))},
                         {"region": "Central", "geometry": json.dumps(box(9.0, 50.5, 15.0, 52.0))},
                         {"region": "South", "geometry": json.dumps(box(6.0, 47.0, 14.0, 50.5))}])


def road_network() -> pd.DataFrame:
    edges = [("Depot", "Stop A", 8), ("Stop A", "Stop B", 7), ("Stop B", "Stop C", 9), ("Depot", "Stop D", 12),
             ("Stop D", "Stop E", 15), ("Stop E", "Stop F", 14), ("Stop C", "Stop F", 30), ("Stop A", "Stop D", 6)]
    return pd.DataFrame(edges, columns=["from_node", "to_node", "minutes"])


def supplier_master() -> pd.DataFrame:
    rows = [{"supplier_id": "S1", "supplier_name": "Globex Corporation", "annual_spend": 120000.0},
            {"supplier_id": "S2", "supplier_name": "  Globex Corp. ", "annual_spend": 30000.0},
            {"supplier_id": "S3", "supplier_name": "Initech", "annual_spend": None},
            {"supplier_id": "S4", "supplier_name": "Umbrella Ltd", "annual_spend": 54000.0},
            {"supplier_id": "S4", "supplier_name": "Umbrella Ltd", "annual_spend": 54000.0},
            {"supplier_id": "S5", "supplier_name": "Stark Industries", "annual_spend": 81000.0}]
    return pd.DataFrame(rows)


def returns_series(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(53)
    dates = pd.bdate_range("2025-09-01", periods=n)
    vol = np.where(np.arange(n) < 200, 0.006, 0.02)  # planted: volatility more than triples on the 201st day
    return pd.DataFrame({"date": dates.date, "daily_return": np.round(rng.normal(0.0004, vol), 6)})


RESEARCH_PAGES = {
    "https://registry.example.org/acme-holdings": "<h1>Company registry: Acme Holdings</h1><p>Acme Holdings Ltd is "
        "owned by Northpoint Capital since 2024. The registered office moved to Dublin in March 2025.</p>",
    "https://news.example.com/acme-ownership": "<h1>Acme changes hands</h1><p>Investment firm Northpoint Capital "
        "completed its purchase of Acme Holdings, which is now owned by Northpoint Capital. Staff were told on Monday.</p>",
    "https://blog.example.net/rumours": "<h1>Rumours</h1><p>Some say Acme Holdings is owned by a sovereign fund.</p>",
}


class FixtureFetcher:
    """Serves RESEARCH_PAGES instead of the internet, for tests and evidence (controlled conditions)."""
    allowed_hosts = ["example.org", "example.com"]  # blog.example.net is deliberately not approved

    def __call__(self, url: str) -> bytes:
        if url not in RESEARCH_PAGES:
            raise ValueError(f"{url} returned 404")
        return RESEARCH_PAGES[url].encode()


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
              "raw_auth_logs": raw_auth_logs(), "firewall_logs": firewall_logs(), "service_logs": service_logs(),
              "wallet_transfers": wallet_transfers(), "service_dependencies": service_dependencies(),
              "ticket_comments": ticket_comments(), "channel_options": channel_options(),
              "maintenance_history": maintenance_history(), "gene_expression": gene_expression(),
              "detection_results": predictions_cv(),
              **code_frames(), "territory_polygons": sales_regions(), "road_network": road_network(),
              "supplier_master": supplier_master(), "returns_series": returns_series(),
              "rig_tests": regression_results("rig_tests", "load_case", ["bending", "torsion", "thermal"], "torsion"),
              "twin_vs_sensor": regression_results("twin_vs_sensor", "asset", ["Pump 7", "Chiller 2", "Boiler 1"], "Chiller 2"),
              **{name: eval_results(name, *spec) for name, spec in EVALS.items()},
              "email_holdout": experiment(4000, groups=("holdout", "treated"), lift=0.03, base=0.05, seed=9, group_col="group",
                                          outcome="purchased")}
    for name, df in frames.items():
        lyst.data.ingest(actor, workspace_id, name, csv_bytes(df), filename=f"{name}.csv", title=name.replace("_", " ").title())
    lyst.services.research_fetcher = FixtureFetcher()  # this instance only: research reads the fixture pages
    if getattr(lyst, "knowledge", None) is not None:
        for title, text in DOCUMENTS.items():
            lyst.knowledge.add_document(actor, workspace_id, text.encode(), filename=title.lower().replace(" ", "_") + ".md",
                                        title=title)
    return list(frames)
