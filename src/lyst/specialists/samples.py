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


def load_all(lyst, actor, workspace_id: str) -> list[str]:
    """Ingest every sample into a workspace; returns the dataset names."""
    from ..demo import csv_bytes  # noqa: PLC0415

    erp, bank = ledgers()
    frames = {"order_events": order_events(), "login_events": logins(), "stores": stores(), "warehouses": warehouses(),
              "ledger_erp": erp, "ledger_bank": bank, "supplier_links": supplier_links(), "customers": customers()}
    for name, df in frames.items():
        lyst.data.ingest(actor, workspace_id, name, csv_bytes(df), filename=f"{name}.csv", title=name.replace("_", " ").title())
    return list(frames)
