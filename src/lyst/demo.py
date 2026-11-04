# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A seeded demo workspace with a planted story, used by the UI's first run and the tests.

Northwind Industries' monthly P&L, Jan 2024 to Sep 2026, by region and product line.
Planted: in Q3 2026 EMEA's cost of goods jumps from ~55% to ~67% of revenue (a supplier
price rise), which pulls the company's operating margin down. Everything else follows
steady growth with seasonality and noise. A second extract, ``pnl_ledger``, is the same P&L
as the finance ledger reports it, with three planted discrepancies for the data-quality
scenario.
"""

from __future__ import annotations

import io

import numpy as np
import pandas as pd

from .semantic import Filter, Measure, MetricSpec

REGIONS = {"North America": 1.0, "EMEA": 0.8, "APAC": 0.6, "LATAM": 0.3}
LINES = {"Hardware": (0.58, 1.0), "Software": (0.18, 0.7), "Services": (0.45, 0.5)}


def pnl(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    months = pd.date_range("2024-01-01", "2026-09-01", freq="MS")
    rows = []
    for i, m in enumerate(months):
        season = 1 + 0.08 * np.sin(2 * np.pi * (m.month - 3) / 12)
        for region, rscale in REGIONS.items():
            for line, (cogs_ratio, lscale) in LINES.items():
                revenue = 1_000_000 * rscale * lscale * (1 + 0.012 * i) * season * rng.normal(1, 0.03)
                ratio = cogs_ratio + rng.normal(0, 0.01)
                if region == "EMEA" and m >= pd.Timestamp("2026-07-01"):
                    ratio += 0.12  # planted
                cogs = revenue * ratio
                opex = revenue * 0.22 * rng.normal(1, 0.02)
                rows.append({"month": m.date(), "region": region, "product_line": line,
                             "revenue": round(revenue, 2), "cogs": round(cogs, 2), "opex": round(opex, 2),
                             "operating_income": round(revenue - cogs - opex, 2)})
    return pd.DataFrame(rows)


def ledger(df: pd.DataFrame) -> pd.DataFrame:
    """The same P&L from the ledger, with planted discrepancies."""
    out = df.copy()
    # 1. APAC Services, Mar 2026, booked twice in the ledger
    dup = out[(out.region == "APAC") & (out.product_line == "Services") & (out.month == pd.Timestamp("2026-03-01").date())]
    out = pd.concat([out, dup], ignore_index=True)
    # 2. LATAM Hardware revenue for Aug 2026 missing (null)
    mask = (out.region == "LATAM") & (out.product_line == "Hardware") & (out.month == pd.Timestamp("2026-08-01").date())
    out.loc[mask, "revenue"] = None
    # 3. a negative cogs credit
    out.loc[5, "cogs"] = -abs(out.loc[5, "cogs"])
    return out


def demand(seed: int = 11) -> pd.DataFrame:
    """Monthly support demand by channel, Jan 2024 to Sep 2026: growth, seasonality (a Q4 peak), noise."""
    rng = np.random.default_rng(seed)
    months = pd.date_range("2024-01-01", "2026-09-01", freq="MS")
    rows = []
    for i, m in enumerate(months):
        season = 1 + 0.15 * (1 if m.month in (10, 11, 12) else 0) + 0.05 * np.sin(2 * np.pi * m.month / 12)
        for channel, base in (("email", 6000), ("chat", 4000), ("phone", 2500)):
            tickets = base * (1 + 0.015 * i) * season * rng.normal(1, 0.04)
            rows.append({"month": m.date(), "channel": channel, "tickets": int(round(tickets))})
    return pd.DataFrame(rows)


def staffing() -> pd.DataFrame:
    months = pd.date_range("2024-01-01", "2026-09-01", freq="MS")
    return pd.DataFrame({"month": [m.date() for m in months], "agents": [38 + i // 6 for i in range(len(months))],
                         "capacity_per_agent": [420] * len(months), "cost_per_agent": [5200.0] * len(months)})


def csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode()


METRICS = [
    MetricSpec(name="revenue", title="Revenue", dataset="pnl", description="Sum of booked revenue.",
               measure=Measure(agg="sum", column="revenue"), time_column="month",
               dimensions=["region", "product_line"], format="currency", unit="USD",
               synonyms=["sales", "top line"]),
    MetricSpec(name="operating_income", title="Operating income", dataset="pnl",
               description="Revenue minus cost of goods and operating expenses.",
               measure=Measure(agg="sum", column="operating_income"), time_column="month",
               dimensions=["region", "product_line"], format="currency", unit="USD", synonyms=["ebit"]),
    MetricSpec(name="operating_margin", title="Operating margin", dataset="pnl", kind="ratio",
               description="Operating income divided by revenue.",
               numerator=Measure(agg="sum", column="operating_income"),
               denominator=Measure(agg="sum", column="revenue"), time_column="month",
               dimensions=["region", "product_line"], format="percent", synonyms=["op margin", "margin"]),
    MetricSpec(name="cogs_ratio", title="COGS ratio", dataset="pnl", kind="ratio",
               description="Cost of goods divided by revenue.",
               numerator=Measure(agg="sum", column="cogs"), denominator=Measure(agg="sum", column="revenue"),
               time_column="month", dimensions=["region", "product_line"], format="percent",
               higher_is_better=False, synonyms=["cost of goods ratio", "margin"]),
    MetricSpec(name="tickets", title="Support tickets", dataset="demand", description="Tickets opened, all channels.",
               measure=Measure(agg="sum", column="tickets"), time_column="month", dimensions=["channel"],
               format="number", unit="tickets", higher_is_better=False, synonyms=["support demand", "demand", "ticket volume"]),
    MetricSpec(name="hardware_revenue", title="Hardware revenue", dataset="pnl",
               measure=Measure(agg="sum", column="revenue", filters=[Filter(column="product_line", value="Hardware")]),
               time_column="month", dimensions=["region"], format="currency"),
]


def seed(lyst, owner, workspace_id: str = "northwind") -> None:
    lyst.guard.create_workspace(owner, workspace_id, "Northwind Industries")
    frame = pnl()
    lyst.data.ingest(owner, workspace_id, "pnl", csv_bytes(frame), filename="pnl.csv", title="Monthly P&L",
                     description="Management P&L by region and product line", classification="confidential")
    lyst.data.ingest(owner, workspace_id, "pnl_ledger", csv_bytes(ledger(frame)), filename="ledger.csv",
                     title="P&L from the ledger", description="The finance ledger's view of the same P&L")
    lyst.data.ingest(owner, workspace_id, "demand", csv_bytes(demand()), filename="demand.csv", title="Support demand",
                     description="Monthly tickets opened by channel")
    lyst.data.ingest(owner, workspace_id, "staffing", csv_bytes(staffing()), filename="staffing.csv", title="Support staffing",
                     description="Agents on staff, capacity and cost per agent, by month")
    for m in METRICS:
        lyst.semantic.define(owner, workspace_id, m, note="seeded")
    lyst.semantic.set_term(owner, workspace_id, "ebitda", "Not defined in this workspace yet.", source="demo")
