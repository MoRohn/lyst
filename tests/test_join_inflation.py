# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario 5, an incorrectly joined dataset: a sum over a one-to-many join counts the "one" side once per match."""

from .conftest import ask

ORDERS = b"order_id,region,amount\n1,EMEA,100\n2,EMEA,200\n3,APAC,300\n"
LINES = b"line_id,order_id,sku\n1,1,a\n2,1,b\n3,1,c\n4,2,a\n5,3,b\n"  # order 1 has three lines


def _seed(lyst, owner):
    lyst.data.ingest(owner, "northwind", "orders", ORDERS, filename="orders.csv")
    lyst.data.ingest(owner, "northwind", "order_lines", LINES, filename="lines.csv")


def test_an_aggregate_over_a_fanned_out_join_is_flagged_not_verified(seeded):
    """J1: fan-out was checked only for queries without an aggregate, the case that cannot mislead a sum."""
    lyst, owner = seeded
    _seed(lyst, owner)
    t = ask(lyst, owner, "select sum(o.amount) as revenue from orders o join order_lines l on o.order_id = l.order_id")
    r = t["result"]
    assert r["tables"] or r["answer"]
    checks = {c["name"]: c for c in r["confidence"]["checks"]}
    assert "join_fanout" in checks and checks["join_fanout"]["passed"] is False, r["confidence"]
    assert "orders" in checks["join_fanout"]["detail"] and r["confidence"]["level"] != "verified"


def test_a_sum_over_the_many_side_of_the_same_join_is_not_flagged(seeded):
    lyst, owner = seeded
    _seed(lyst, owner)
    t = ask(lyst, owner, "select count(l.line_id) as lines, o.region from orders o join order_lines l "
                         "on o.order_id = l.order_id group by o.region")
    checks = {c["name"]: c for c in t["result"]["confidence"]["checks"]}
    assert "join_fanout" not in checks, checks.get("join_fanout")


def test_a_one_to_one_join_with_a_sum_is_not_flagged(seeded):
    lyst, owner = seeded
    _seed(lyst, owner)
    t = ask(lyst, owner, "select sum(o.amount) from orders o join (select distinct order_id from order_lines) l "
                         "on o.order_id = l.order_id")
    checks = {c["name"]: c for c in t["result"]["confidence"]["checks"]}
    assert "join_fanout" not in checks, checks.get("join_fanout")
