# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario C, imperfect enterprise data (master prompt 4.1). Each case was found by probing: before these checks
Lyst reported the overstated, mixed or partial figure as *verified*. Now the problem is detected inside the period
analysed, the answer leads with it, and the result is not verified."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.semantic import Measure, MetricSpec

from .conftest import ask


def _metric(lyst, owner, name, frame, **kw):
    lyst.data.ingest(owner, "northwind", name, demo.csv_bytes(frame), filename=f"{name}.csv")
    lyst.semantic.define(owner, "northwind", MetricSpec(
        name=f"{name}_revenue", title=f"{name.capitalize()} revenue", dataset=name, measure=Measure(agg="sum", column="revenue"),
        time_column="month", dimensions=kw.pop("dimensions", ["region"]), format="currency"))


def _failed(r):
    return {c["name"]: c for c in r["confidence"]["checks"] if not c["passed"]}


def test_duplicate_transactions_are_not_verified(seeded):
    lyst, owner = seeded
    base = demo.pnl()
    dup = pd.concat([base, base[pd.to_datetime(base["month"]) >= "2026-07-01"].head(6)])
    _metric(lyst, owner, "dup", dup)
    r = ask(lyst, owner, "What was dup revenue in Q3 2026?")["result"]
    assert r["confidence"]["level"] == "not verified" and r["status"] == "unverified"
    assert "6 of 42 rows in Q3 2026 are exact duplicates" in _failed(r)["duplicate_rows"]["detail"]
    assert r["answer"].startswith("Not verified: 6 of 42 rows")
    assert r["implications"][0].startswith("Do not use these figures")
    # a period without duplicates is fine
    clean = ask(lyst, owner, "What was dup revenue in Q2 2026?")["result"]
    assert clean["confidence"]["level"] == "verified"


def test_mixed_currencies_are_not_summed_silently(seeded):
    lyst, owner = seeded
    cur = demo.pnl()
    cur["currency"] = ["EUR" if r == "EMEA" else "USD" for r in cur["region"]]
    _metric(lyst, owner, "ccy", cur, dimensions=["region", "currency"])
    r = ask(lyst, owner, "What was ccy revenue in Q3 2026?")["result"]
    assert r["confidence"]["level"] == "not verified"
    assert "2 currencies (EUR, USD)" in _failed(r)["single_currency"]["detail"]
    # filtered to one currency it is a clean question
    usd = ask(lyst, owner, "What was ccy revenue in Q3 2026 in USD?")["result"]
    assert usd["confidence"]["level"] == "verified", usd["confidence"]


def test_an_incomplete_period_is_not_compared_with_a_complete_one(seeded):
    lyst, owner = seeded
    base = demo.pnl()
    _metric(lyst, owner, "part", base[pd.to_datetime(base["month"]) < "2026-09-01"])  # Q3 2026 has two of three months
    r = ask(lyst, owner, "Why did part revenue decline in Q3 2026?")["result"]
    assert r["confidence"]["level"] == "not verified"
    assert "Q3 2026 ends 2026-10-01 (exclusive), so it is incomplete" in _failed(r)["freshness"]["detail"]
    assert r["answer"].startswith("Not verified: data runs to 2026-08-31")
    # asking for period-to-date is a different question: a warning, not an error
    td = ask(lyst, owner, "What was part revenue this quarter so far?")["result"]
    assert td["confidence"]["level"] != "not verified"


def test_data_that_starts_inside_the_prior_period_is_flagged(seeded):
    lyst, owner = seeded
    base = demo.pnl()
    _metric(lyst, owner, "late", base[pd.to_datetime(base["month"]) >= "2026-05-01"])  # Q2 2026 has two of three months
    r = ask(lyst, owner, "What was late revenue in Q3 2026?")["result"]
    assert "after Q2 2026 begins" in _failed(r)["period_start:prior"]["detail"]
    assert r["confidence"]["level"] == "not verified"


def test_schema_drift_blocks_the_metric_with_a_plain_reason(seeded):
    lyst, owner = seeded
    base = demo.pnl()
    _metric(lyst, owner, "drift", base)
    lyst.data.ingest(owner, "northwind", "drift", demo.csv_bytes(base.drop(columns=["revenue"])), filename="v2.csv")
    t = ask(lyst, owner, "What was drift revenue in Q3 2026?")
    assert t["status"] == "done" and t["outcome"] == "needs_input"  # not a crash with a database error
    assert "needs column(s) revenue, which drift v2 no longer has" in t["result"]["answer"]


def test_a_corrupt_upload_is_refused_with_a_reason(seeded):
    lyst, owner = seeded
    with pytest.raises(ValueError, match="could not be read"):
        lyst.data.ingest(owner, "northwind", "bad", b"\x00\x01garbage,,\n\xff\xfe", filename="bad.csv")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post("/v1/workspaces/northwind/datasets", headers=h, data={"name": "bad"},
                   files={"file": ("bad.csv", b"\x00\x01garbage,,\n\xff\xfe", "text/csv")})
        assert r.status_code == 400 and "could not be read" in r.json()["detail"]
    assert "bad" not in {d["name"] for d in lyst.data.catalog(owner, "northwind")}
