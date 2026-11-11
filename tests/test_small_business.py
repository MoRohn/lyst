# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A freelancer or small business owner brings the exports they already have (an invoicing app, a bank or card) and
gets governed starting metrics proposed from them: income invoiced, expenses, invoices sent, active clients and the
average invoice. Nothing is defined until the owner accepts it, and everyday words ("earn", "spending") reach the
right metric."""

from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest

from lyst import domains

from .conftest import ask

WS = "studio"
CLIENTS = ["Acme Co", "Bluebird Cafe", "Corner Dental", "Delta Realty", "Evergreen Gym"]
CATEGORIES = {"Software": 180, "Rent": 900, "Contractors": 1200, "Travel": 250, "Equipment": 300}


def _csv(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode()


def books(today: str = "2026-10-28") -> tuple[pd.DataFrame, pd.DataFrame]:
    """22 months of a design studio's invoices (issued, due 30 days later, paid or still open) and expenses."""
    rng = np.random.default_rng(3)
    inv, exp, n = [], [], 1
    for m in pd.date_range("2025-01-01", "2026-10-01", freq="MS"):
        for _ in range(int(rng.integers(3, 7))):
            client = CLIENTS[int(rng.integers(0, 5))] if rng.random() > 0.35 else "Acme Co"
            issued = m + pd.Timedelta(days=int(rng.integers(0, 27)))
            paid = issued + pd.Timedelta(days=int(rng.integers(10, 70)))
            paid = None if paid > pd.Timestamp(today) else paid.date()
            inv.append({"invoice_no": f"INV-{n:04d}", "client": client, "issue_date": issued.date(),
                        "due_date": (issued + pd.Timedelta(days=30)).date(), "paid_date": paid,
                        "amount": round(float(rng.normal(1800, 600)), 2), "status": "paid" if paid else "open"})
            n += 1
        for cat, base in CATEGORIES.items():
            exp.append({"date": (m + pd.Timedelta(days=int(rng.integers(0, 27)))).date(), "category": cat,
                        "vendor": f"{cat} vendor", "amount": round(base * float(rng.uniform(0.6, 1.5)), 2)})
    return pd.DataFrame(inv), pd.DataFrame(exp)


def open_studio(lyst, *, accept: bool = True):
    owner = lyst.guard.create_user("sam@studio.test", "Sam")
    lyst.guard.create_workspace(owner, WS, "Sam's design studio")
    inv, exp = books()
    lyst.data.ingest(owner, WS, "invoices", _csv(inv), filename="invoices.csv", title="Invoices",
                     description="Invoices exported from my invoicing app")
    lyst.data.ingest(owner, WS, "expenses", _csv(exp), filename="expenses.csv", title="Expenses",
                     description="Business expenses from my bank")
    found = lyst.onboarding.discover(owner, WS)
    if accept:
        for p in lyst.onboarding.proposals(owner, WS, kind="metric"):
            lyst.onboarding.accept(owner, WS, p["id"])
    return owner, found


def test_invoicing_and_expense_exports_are_recognized_as_a_small_business(lyst):
    owner, found = open_studio(lyst, accept=False)
    top = found["domains"][0]
    assert top["id"] == "small_business" and top["strength"] == "strong"
    assert any(e["where"] == "invoices" and "invoice" in e["signals"] for e in top["evidence"])  # plural file name
    props = {p["id"]: p["payload"] for p in lyst.onboarding.proposals(owner, WS, kind="metric")}
    assert set(props) == {"metric:invoiced", "metric:expenses", "metric:invoice_count", "metric:active_clients",
                          "metric:average_invoice"}
    # both exports have an "amount" column: each metric is built from the export it belongs to
    assert (props["metric:invoiced"]["dataset"], props["metric:invoiced"]["measure"]["column"]) == ("invoices", "amount")
    assert props["metric:invoiced"]["time_column"] == "issue_date"
    assert (props["metric:expenses"]["dataset"], props["metric:expenses"]["measure"]["column"]) == ("expenses", "amount")
    assert props["metric:expenses"]["higher_is_better"] is False
    assert props["metric:active_clients"]["measure"] == {"agg": "count_distinct", "column": "client", "filters": []}
    assert "income" in props["metric:invoiced"]["synonyms"]
    # proposals are not authority
    assert not lyst.semantic.metrics(owner, WS)


def test_everyday_words_reach_the_accepted_metrics(lyst):
    owner, _ = open_studio(lyst)
    inv, exp = books()
    inv["issue_date"] = pd.to_datetime(inv["issue_date"])
    sep = inv[(inv.issue_date >= "2026-09-01") & (inv.issue_date < "2026-10-01")].amount.sum()
    r = ask(lyst, owner, "How much did I earn last month?", ws=WS)["result"]
    assert r["answer"].startswith("Invoiced was ")
    assert f"${sep / 1000:.1f}K in Sep 2026" in r["answer"]
    r = ask(lyst, owner, "What was my spending by category this year?", ws=WS)["result"]
    assert "Contractors" in r["answer"] and "xpenses were" in r["answer"]
    r = ask(lyst, owner, "Brief me on expenses", ws=WS)["result"]
    assert r["mode"] == "brief" and r["answer"].startswith("Expenses in Sep 2026: ")
    # the whole-business brief covers every accepted metric
    r = ask(lyst, owner, "How are we doing?", ws=WS)["result"]
    assert {row[0] for row in r["tables"][0]["rows"]} == {"Invoiced", "Expenses", "Invoices sent", "Active clients",
                                                          "Average invoice"}


def test_an_enterprise_workspace_is_not_mistaken_for_a_small_business(seeded):
    lyst, owner = seeded
    found = lyst.onboarding.discover(owner, "northwind")
    assert found["domains"][0]["id"] != "small_business"


@pytest.mark.parametrize(("text", "has", "lacks"), [
    ("invoices.csv", {"invoices", "invoice", "csv"}, set()),
    ("expense_categories", {"expense", "categories", "category"}, set()),
    ("status", {"status"}, {"statu"}),
    ("business address", {"business", "address"}, {"busines", "addres"}),
])
def test_words_add_the_singular_of_a_plural(text, has, lacks):
    w = domains.words(text)
    assert has <= w and not (lacks & w)
