# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Receivables: "Who owes me money?" reads the invoices still open today, how late each is, who to chase first and
which clients pay slower than they used to. Lyst never contacts a client: reminders are actions for the owner."""

from __future__ import annotations

import pandas as pd
import pytest

from lyst.analyst import receivables
from lyst.followups import FollowUps

from .conftest import ask
from .test_small_business import WS, books, open_studio

AS_OF = "2026-10-28"


@pytest.fixture
def studio(lyst, monkeypatch):
    monkeypatch.setattr(receivables, "today", lambda: pd.Timestamp(AS_OF).date())
    owner, _ = open_studio(lyst)
    return lyst, owner


def _expected():
    inv, _ = books()
    for c in ("issue_date", "due_date", "paid_date"):
        inv[c] = pd.to_datetime(inv[c])
    open_ = inv[inv.paid_date.isna()].copy()
    open_["late"] = (pd.Timestamp(AS_OF) - open_.due_date).dt.days
    return inv, open_


def test_who_owes_me_money_totals_the_open_invoices_and_names_who_to_chase(studio):
    lyst, owner = studio
    t = ask(lyst, owner, "Who owes me money?", ws=WS)
    r = t["result"]
    assert t["status"] == "done" and r["status"] == "complete" and r["mode"] == "receivables"
    inv, open_ = _expected()
    overdue = open_[open_.late > 0]
    assert r["answer"].startswith(f"Clients owe you ${open_.amount.sum():,.0f} across {len(open_)} open invoice")
    assert "as of 28 Oct 2026" in r["answer"]
    if len(overdue):
        assert f"${overdue.amount.sum():,.0f} of it is overdue" in r["answer"]
        worst = overdue.groupby("client").amount.sum().idxmax()
        assert f"{worst} owes the most overdue" in r["answer"]
        oldest = overdue.sort_values(["late", "amount"], ascending=False).iloc[0]
        assert r["actions"][0].startswith(f"Send {oldest.client} a reminder for invoice {oldest.invoice_no}")
    # the by-client table adds up to the total, and every open invoice falls in one aging bucket
    owed = next(tb for tb in r["tables"] if tb["title"].startswith("Owed by client"))
    assert sum(row[1] for row in owed["rows"]) == pytest.approx(open_.amount.sum(), abs=0.05)
    aging = next(tb for tb in r["tables"] if tb["title"] == "Aging")
    assert sum(row[2] for row in aging["rows"]) == len(open_)
    # how fast clients pay comes from the paid invoices
    paid = inv[inv.paid_date.notna()]
    assert f"Clients usually pay {(paid.paid_date - paid.issue_date).dt.days.median():.0f} days" in r["answer"]
    assert r["supporting"]["queries"] and all(q["sql"] for q in r["supporting"]["queries"])
    assert any("does not contact clients" in a for a in r["supporting"]["assumptions"])
    assert r["confidence"]["level"] == "verified"


def test_a_named_client_and_payment_speed_lead_the_answer(studio):
    lyst, owner = studio
    _, open_ = _expected()
    acme = open_[open_.client == "Acme Co"]
    r = ask(lyst, owner, "How much does Acme Co owe me?", ws=WS)["result"]
    assert r["answer"].startswith(f"Acme Co owes you ${acme.amount.sum():,.0f} on {len(acme)} open invoice")
    assert "In all, clients owe you" in r["answer"]
    r = ask(lyst, owner, "How long do clients take to pay?", ws=WS)["result"]
    assert r["answer"].startswith("Clients usually pay ") and "takes longest" in r["answer"]


def test_a_stale_export_is_called_out(lyst, monkeypatch):
    monkeypatch.setattr(receivables, "today", lambda: pd.Timestamp("2026-12-31").date())
    owner, _ = open_studio(lyst)
    r = ask(lyst, owner, "Which invoices are overdue?", ws=WS)["result"]
    stale = next(c for c in r["confidence"]["checks"] if c["name"] == "current:invoices")
    assert not stale["passed"] and "not in this export yet" in stale["detail"]
    assert any(a.startswith("Upload a fresh export of Invoices") for a in r["actions"])


def test_no_invoices_dataset_says_what_it_needs(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Who owes us money?")["result"]
    assert r["status"] == "needs_input" and "could not find an invoices dataset" in r["answer"]


def test_a_reader_who_may_not_see_amounts_gets_no_figures(studio):
    lyst, owner = studio
    viewer = lyst.guard.create_user("v@studio.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    lyst.data.set_column_policy(owner, WS, "invoices", "amount", "confidential", "admin")
    r = ask(lyst, viewer, "Who owes me money?", ws=WS)["result"]
    # the amount column is hidden from the viewer, so there is no invoices dataset they can read
    assert r["status"] == "needs_input" and "$" not in r["answer"]


def test_receivables_can_be_scheduled_but_not_moved_to_a_period(studio):
    lyst, owner = studio
    t = ask(lyst, owner, "Who owes me money?", ws=WS)
    fu = FollowUps(lyst.runtime, lyst.semantic)
    res = fu.apply(owner, t["id"], "every Monday at 8")
    assert res["action"] == "schedule" and res["question"] == "Who owes me money?" and res["enabled"] is False
    res = fu.apply(owner, t["id"], "what about last quarter?")
    assert res["action"] == "unsupported" and "invoices open today" in res["message"]


@pytest.mark.parametrize("text", ["Who owes me money?", "Which invoices are overdue?", "How long do clients take to pay?",
                                  "AR aging report", "Which clients pay late?", "unpaid invoices", "How much does Acme owe?"])
def test_receivables_phrases(text):
    assert receivables.asks_for_receivables(text)


@pytest.mark.parametrize("text", ["Which tickets are overdue?", "Show overdue maintenance work orders",
                                  "What do our documents say about overdue invoices?", "What was revenue last quarter?"])
def test_not_receivables_phrases(text):
    assert not receivables.asks_for_receivables(text)


def test_find_invoices_needs_an_amount_and_a_way_to_tell_paid_from_open():
    cols = [{"name": "invoice_no", "semantic_type": "identifier"}, {"name": "client", "semantic_type": "category"},
            {"name": "issue_date", "semantic_type": "date"}, {"name": "due_date", "semantic_type": "date"},
            {"name": "amount", "semantic_type": "amount"}]
    assert receivables.find_invoices([{"name": "invoices", "columns": cols}]) is None  # no paid date or status
    got = receivables.find_invoices([{"name": "invoices", "columns": [*cols, {"name": "status", "semantic_type": "category"}]}])
    assert got and (got["due"], got["issued"], got["status"], got["paid"], got["client"]) == \
        ("due_date", "issue_date", "status", None, "client")
