# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Transformations: derived datasets verified by running them, refreshed with their inputs, versioned, with lineage."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.data.sqlguard import QueryRejected
from lyst.transforms import explain, lint

WS = "northwind"
CLEAN = ("SELECT month, region, product_line, sum(revenue) AS revenue, sum(cogs) AS cogs FROM pnl_ledger "
         "GROUP BY month, region, product_line")


def test_preview_checks_the_actual_result(seeded):
    lyst, owner = seeded
    p = lyst.transforms.preview(owner, WS, CLEAN, key=["month", "region", "product_line"])
    assert p["ok"] and p["rows"] == 396 and p["inputs"] == ["pnl_ledger"]
    checks = {c["name"]: c for c in p["checks"]}
    assert checks["key_unique"]["passed"] and checks["not_empty"]["passed"]
    assert checks["reconciles:cogs"]["passed"]  # summed measures keep their total
    # without the GROUP BY, the planted double booking repeats the key
    dup = lyst.transforms.preview(owner, WS, "SELECT month, region, product_line, revenue FROM pnl_ledger", key=["month", "region", "product_line"])
    assert not dup["ok"] and "1 rows repeat" in next(c["detail"] for c in dup["checks"] if c["name"] == "key_unique")
    # a join on part of the grain multiplies rows
    fan = lyst.transforms.preview(owner, WS, "SELECT p.month, p.region, p.revenue, l.cogs FROM pnl p JOIN pnl_ledger l ON p.month = l.month")
    assert not fan["ok"] and any(c["name"] == "no_fan_out" and not c["passed"] for c in fan["checks"])
    with pytest.raises(QueryRejected):
        lyst.transforms.preview(owner, WS, "SELECT * FROM read_csv('/etc/passwd')")


def test_lint_and_explain():
    kinds = {a["kind"] for a in lint("SELECT DISTINCT * FROM pnl, demand ORDER BY month")}
    assert {"select_star", "order_without_limit", "distinct", "join_without_condition"} <= kinds
    ex = explain("SELECT p.month, sum(p.revenue) / sum(d.tickets) AS revenue_per_ticket FROM pnl p JOIN demand d ON p.month = d.month "
                 "WHERE p.region = 'EMEA' GROUP BY p.month")
    assert ex["inputs"] == ["demand", "pnl"] and ex["joins"][0]["on"] == "p.month = d.month"
    assert ex["filter"] == "p.region = 'EMEA'" and ex["computed"][0]["column"] == "revenue_per_ticket"


def test_define_materializes_registers_and_follows_its_input(seeded):
    lyst, owner = seeded
    out = lyst.transforms.define(owner, WS, "ledger_clean", CLEAN, key=["month", "region", "product_line"],
                                 description="The ledger at one row per month, region and product line")
    assert out["version"] == 1 and out["run"]["outcome"] == "ok" and out["run"]["dataset_version"] == 1
    q = lyst.data.query(owner, WS, "SELECT count(*) FROM ledger_clean")
    assert q.rows == [[396]]
    # a governed dataset: insights, catalog, lineage
    assert lyst.catalog.insights(owner, WS, "ledger_clean")["grain"] == ["month", "product_line", "region"]
    graph = lyst.knowledge.lineage(owner, WS, "dataset", "ledger_clean")
    assert any(u["name"] == "pnl_ledger" and u["rel"] == "derived_from" for u in graph["upstream"])
    asset = lyst.assets.list(owner, WS, kind="data_model")[0]
    assert asset["ref_id"] == "ledger_clean" and asset["depends"]["datasets"] == {"pnl_ledger": 1}
    # the input changes: the transformation runs again on its own, and the asset stays current
    lyst.data.ingest(owner, WS, "pnl_ledger", demo.csv_bytes(demo.ledger(demo.pnl(seed=8))), filename="ledger.csv")
    t = lyst.transforms.get(owner, WS, "ledger_clean")
    assert t["runs"][0]["trigger"] == "input:pnl_ledger" and t["runs"][0]["dataset_version"] == 2
    a = lyst.assets.get(owner, WS, asset["id"])
    assert a["health"] == "current" and a["depends"]["datasets"] == {"pnl_ledger": 2} and a["version"] == 1
    # an input loses a column: the run fails, the last good version stays, the asset is broken and needs a person
    lyst.data.ingest(owner, WS, "pnl_ledger", demo.csv_bytes(demo.ledger(demo.pnl()).drop(columns=["cogs"])), filename="ledger.csv")
    t = lyst.transforms.get(owner, WS, "ledger_clean")
    assert t["status"] == "failed" and "cogs" in t["last_error"]
    assert lyst.stores.workspace(WS).scalar("SELECT current_version FROM datasets WHERE name = 'ledger_clean'") == 2
    a = lyst.assets.get(owner, WS, asset["id"])
    assert a["health"] == "broken" and not a["proposal"]["automatic"]
    # fixing the definition is a new version and recovers
    fixed = CLEAN.replace(", sum(cogs) AS cogs", "")
    out = lyst.transforms.define(owner, WS, "ledger_clean", fixed, key=["month", "region", "product_line"], note="cogs dropped upstream")
    assert out["version"] == 2
    t = lyst.transforms.get(owner, WS, "ledger_clean")
    assert t["status"] == "active" and [v["version"] for v in t["versions"]] == [2, 1]
    assert lyst.assets.get(owner, WS, asset["id"])["health"] == "current"


def test_refusals(seeded):
    lyst, owner = seeded
    with pytest.raises(ValueError, match="failed its checks"):
        lyst.transforms.define(owner, WS, "bad", "SELECT month, region, product_line, revenue FROM pnl_ledger", key=["month", "region", "product_line"])
    with pytest.raises(ValueError, match="already exists"):
        lyst.transforms.define(owner, WS, "pnl", CLEAN)
    lyst.transforms.define(owner, WS, "a_model", "SELECT month, sum(revenue) AS revenue FROM pnl GROUP BY month")
    lyst.transforms.define(owner, WS, "b_model", "SELECT month, revenue FROM a_model")
    with pytest.raises(ValueError, match="cycle"):
        lyst.transforms.define(owner, WS, "a_model", "SELECT month, revenue FROM b_model")
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, WS, viewer.user_id, "editor")
    lyst.data.set_row_policy(owner, WS, "pnl", "region", principal_kind="user", principal=viewer.user_id, allowed_values=["EMEA"])
    with pytest.raises(QueryRejected, match="silently partial"):
        lyst.transforms.define(viewer, WS, "emea_only", "SELECT month, sum(revenue) AS revenue FROM pnl GROUP BY month")


def test_incremental_runs_append_only_new_rows(seeded):
    lyst, owner = seeded
    events = pd.DataFrame({"day": pd.date_range("2026-01-01", periods=10).date, "amount": [float(i) for i in range(10)]})
    lyst.data.ingest(owner, WS, "events", demo.csv_bytes(events), filename="events.csv")
    lyst.transforms.define(owner, WS, "events_big", "SELECT day, amount FROM events WHERE amount >= 0", incremental_column="day")
    more = pd.concat([events, pd.DataFrame({"day": pd.date_range("2026-01-11", periods=3).date, "amount": [10.0, 11.0, 12.0]})])
    lyst.data.ingest(owner, WS, "events", demo.csv_bytes(more), filename="events.csv")
    run = lyst.transforms.get(owner, WS, "events_big")["runs"][0]
    assert run["mode"] == "incremental" and run["rows"] == 13
    assert lyst.data.query(owner, WS, "SELECT count(*), sum(amount) FROM events_big").rows == [[13, 78.0]]


def test_design_from_a_sentence_uses_the_catalog_and_verifies(seeded):
    lyst, owner = seeded
    assert lyst.transforms.design(owner, WS, "monthly revenue by region")["sql"] is None  # no model: asks for SQL

    class Model:
        seen: list = []

        def allowed_for(self, ws):
            return True

        def chat_json(self, ws, messages, **kw):
            self.seen.append(messages)
            return {"sql": "SELECT month, region, sum(revenue) AS revenue FROM pnl GROUP BY month, region", "name": "Revenue by Region",
                    "key": ["month", "region"], "explanation": "Sums revenue per month and region."}

    lyst.transforms.llm = Model()
    p = lyst.transforms.design(owner, WS, "monthly revenue by region")
    assert p["ok"] and p["name"] == "revenue_by_region" and p["preview"]["rows"] == 132
    prompt = Model.seen[0][1]["content"]
    assert '"dataset": "pnl"' in prompt and "EMEA" not in prompt  # schema and grain only, never rows


def test_transform_routes(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.post(f"/v1/workspaces/{WS}/transforms/preview", json={"sql": CLEAN}, headers=h).json()["ok"]
        r = c.post(f"/v1/workspaces/{WS}/transforms", json={"name": "ledger_clean", "sql": CLEAN, "key": ["month", "region", "product_line"]}, headers=h)
        assert r.status_code == 200 and r.json()["run"]["outcome"] == "ok"
        assert [t["name"] for t in c.get(f"/v1/workspaces/{WS}/transforms", headers=h).json()] == ["ledger_clean"]
        assert c.get(f"/v1/workspaces/{WS}/transforms/ledger_clean", headers=h).json()["explain"]["group_by"]
        assert c.post(f"/v1/workspaces/{WS}/transforms/ledger_clean/run", params={"full": True}, headers=h).json()["outcome"] == "ok"
        assert c.post(f"/v1/workspaces/{WS}/transforms/design", json={"request": "x"}, headers=h).json()["sql"] is None
        assert c.post(f"/v1/workspaces/{WS}/transforms", json={"name": "bad", "sql": "SELECT month, revenue FROM pnl_ledger",
                                                               "key": ["month"]}, headers=h).status_code == 400
        assert c.post(f"/v1/workspaces/{WS}/transforms/ledger_clean/retire", headers=h).json()["status"] == "retired"


def test_derived_datasets_inherit_the_strictest_input_classification(seeded):
    lyst, owner = seeded
    db = lyst.stores.workspace(WS)
    db.execute("UPDATE datasets SET classification = 'confidential' WHERE name = 'pnl'")
    db.execute("UPDATE datasets SET classification = 'public' WHERE name = 'demand'")
    lyst.transforms.define(owner, WS, "revenue_per_ticket", "SELECT p.month, sum(p.revenue) / max(d.tickets) AS rpt FROM pnl p "
                           "JOIN (SELECT month, sum(tickets) AS tickets FROM demand GROUP BY month) d ON p.month = d.month GROUP BY p.month")
    assert db.scalar("SELECT classification FROM datasets WHERE name = 'revenue_per_ticket'") == "confidential"
    db.execute("UPDATE datasets SET classification = 'restricted' WHERE name = 'revenue_per_ticket'")  # raised by hand: stays
    lyst.transforms.run(owner, WS, "revenue_per_ticket", full=True)
    assert db.scalar("SELECT classification FROM datasets WHERE name = 'revenue_per_ticket'") == "restricted"
