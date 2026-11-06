# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Skill discovery and reuse (scenario D of the eighth iteration) and conversational drill-down with reproducible
analytical state."""

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.followups import resolve
from lyst.skills.discovery import bm25, tokens

from .conftest import ask


def _approved_hhi(lyst, owner):
    analyst = lyst.guard.create_user("ana@northwind.test", "Ana")
    lyst.guard.add_member(owner, "northwind", analyst.user_id, "editor")
    ask(lyst, analyst, "What is the Herfindahl concentration of revenue by region last quarter?")
    ap = next(a for a in lyst.runtime.approvals(owner, "northwind") if a["action"] == "skill.register")
    lyst.runtime.decide(owner, ap["id"], True)
    return analyst


def test_tokens_and_bm25_rank_by_meaning():
    assert "concentration" in tokens("How concentrated is revenue?") and "concentration" in tokens("Herfindahl index")
    docs = [tokens("Herfindahl concentration index of a measure across members"), tokens("Forecast a metric with ETS")]
    s = bm25(tokens("how concentrated is revenue across regions"), docs)
    assert s[0] > s[1] == 0


def test_scenario_d_a_paraphrased_request_reuses_the_validated_skill(seeded):
    lyst, owner = seeded
    analyst = _approved_hhi(lyst, owner)
    before = {(s["name"], s["version"]) for s in lyst.skills.list(owner, "northwind")}
    # different words from the skill's triggers: without discovery this drafts a new "market concentration" skill
    q = "Calculate the market concentration of revenue across regions last quarter"
    d = lyst.skills.discover(analyst, "northwind", q)
    assert d["reuse"] == "hhi_concentration"
    top = d["candidates"][0]
    assert top["name"] == "hhi_concentration" and top["usable"] and "concentration" in top["matched_terms"]
    t = ask(lyst, analyst, q)
    assert t["status"] == "done" and t["result"]["status"] == "complete"
    assert "run_skill" in [s["capability"] for s in t["steps"]]
    assert {(s["name"], s["version"]) for s in lyst.skills.list(owner, "northwind")} == before  # nothing new drafted
    df = demo.pnl()
    m = pd.to_datetime(df["month"])
    q3 = df[(m >= "2026-07-01") & (m < "2026-10-01")].groupby("region").revenue.sum()
    assert f"{float(((q3 / q3.sum()) ** 2).sum()):.3f}" in t["result"]["answer"]
    # use is counted
    assert lyst.skills.discover(analyst, "northwind", q)["candidates"][0]["runs"] >= 1


def test_an_incompatible_or_failing_skill_is_listed_with_its_reason_not_reused(seeded):
    lyst, owner = seeded
    _approved_hhi(lyst, owner)
    db = lyst.stores.workspace("northwind")
    perms = json.loads(db.scalar("SELECT permissions FROM skills WHERE name = 'hhi_concentration' AND status = 'approved'"))
    perms["input_sql"] = perms["input_sql"].replace('"revenue"', '"net_revenue"')
    db.execute("UPDATE skills SET permissions = ? WHERE name = 'hhi_concentration' AND status = 'approved'", [json.dumps(perms)])
    d = lyst.skills.discover(owner, "northwind", "market concentration of revenue across regions")
    hhi = next(c for c in d["candidates"] if c["name"] == "hhi_concentration")
    assert not hhi["usable"] and "net_revenue" in hhi["reasons"][0] and d["reuse"] is None


def test_discovery_endpoint(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.get("/v1/workspaces/northwind/skills/discover", params={"q": "forecast revenue next quarter"}, headers=h).json()
    assert r["reuse"] is None and r["candidates"][0]["kind"] == "capability" and "forecast" in r["candidates"][0]["name"]


# ----------------------------------------------------------------------------- drill-down


def test_follow_up_phrases_resolve_against_the_earlier_answer():
    parent = {"understand": {"question": "What was revenue last quarter?", "filters": [], "by": None}, "period_label": "Q3 2026"}
    dims = ["region", "product_line"]
    r = lambda t: resolve(parent, t, dims, "revenue")  # noqa: E731
    assert r("Break that down by region")["question"] == "What was revenue Q3 2026 by region?"
    assert r("now by product line")["changed"] == {"by": "product_line"}
    assert "same period a year earlier" in r("Compare it to last year")["question"]
    assert r("Show the top contributing regions")["question"] == "Why did revenue change Q3 2026?"
    assert r("Make this a dashboard by region")["question"] == "Build a dashboard of revenue by region"
    s = r("Schedule this every Monday at 9am")
    assert s["action"] == "schedule" and s["schedule"] == {"at": "09:00", "weekdays": ["mon"]}
    assert r("What about Q1 2026?")["question"] == "What was revenue Q1 2026?"
    assert r("What about last year?")["question"] == "What was revenue last year?"  # a new period, not a comparison
    assert r("Exclude unusual transactions")["action"] == "unsupported"  # never silently drops rows
    assert r("Test whether the difference is significant")["action"] == "unsupported"
    assert r("hmm")["action"] == "unsupported"


def test_drill_down_carries_context_and_records_how_the_state_was_reached(seeded):
    lyst, owner = seeded
    first = ask(lyst, owner, "What was revenue in EMEA last quarter?")
    fu = lyst.services.runtime  # noqa: F841 - the API path is tested below; the service directly here
    from lyst.followups import FollowUps

    f = FollowUps(lyst.runtime, lyst.semantic)
    out = f.apply(owner, first["id"], "Break that down by product line")
    assert out["question"] == "What was revenue Q3 2026 in EMEA by product line?"
    assert out["carried"]["filters"] == [{"column": "region", "op": "=", "value": "EMEA"}]
    lyst.runtime.drain()
    child = lyst.runtime.get(owner, out["task_id"])
    assert child["status"] == "done" and child["request"]["derived_from"]["task_id"] == first["id"]
    assert child["request"]["derived_from"]["changed"] == {"by": "product_line"}
    df = demo.pnl()
    m = pd.to_datetime(df["month"])
    q3 = df[(m >= "2026-07-01") & (m < "2026-10-01") & (df.region == "EMEA")]
    hw = float(q3[q3.product_line == "Hardware"].revenue.sum())
    vals = [v for t in child["result"]["tables"] for row in t["rows"] for v in row if isinstance(v, int | float)]
    assert any(abs(v - hw) < 1e-6 * hw for v in vals)
    # reproducible: asking the recorded question again gives the same numbers
    again = ask(lyst, owner, child["request"]["question"])
    assert again["result"]["tables"] == child["result"]["tables"]


def test_follow_up_endpoint_schedules_and_refuses_unknown(seeded):
    lyst, owner = seeded
    first = ask(lyst, owner, "What was revenue last quarter?")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post(f"/v1/tasks/{first['id']}/follow-up", headers=h,
                   json={"text": "Schedule this every Monday", "timezone": "Europe/London"}).json()
        assert r["action"] == "schedule" and r["enabled"] is False and r["cadence"] == "every Monday at 08:00 (Europe/London)"
        r = c.post(f"/v1/tasks/{first['id']}/follow-up", headers=h, json={"text": "Exclude unusual transactions"}).json()
        assert r["action"] == "unsupported" and "silently" in r["message"]
        other = lyst.guard.create_user("x@other.test", "X")
        hx = {"Authorization": f"Bearer {lyst.guard.issue_token(other)}"}
        assert c.post(f"/v1/tasks/{first['id']}/follow-up", headers=hx, json={"text": "by region"}).status_code in (403, 404)


@pytest.mark.parametrize("text", ["break that down by region", "Compare it to last year"])
def test_follow_ups_need_a_metric_answer(seeded, text):
    lyst, owner = seeded
    first = ask(lyst, owner, "What is our margin?")  # ambiguous: no metric resolved
    from lyst.followups import FollowUps

    with pytest.raises(ValueError):
        FollowUps(lyst.runtime, lyst.semantic).apply(owner, first["id"], text)


SUGGESTED = ["break that down by region", "compare it to last year", "why?", "make this a dashboard", "schedule this every Monday"]


@pytest.mark.parametrize("text", [
    *SUGGESTED,
    *(f"“{t}”" for t in SUGGESTED),  # typed as the placeholder shows them, quotes and all
    *(f'"{t}"' for t in SUGGESTED),
    *(f"Change it: “{t}”" for t in SUGGESTED),
    *(t.capitalize() + "." for t in SUGGESTED),
])
def test_every_suggested_refinement_resolves_however_it_is_typed(text):
    parent = {"understand": {"question": "Forecast cogs ratio for the next 2 quarters", "filters": [], "by": None},
              "period_label": None}
    out = resolve(parent, text, ["region", "product_line"], "cogs ratio")
    assert out["action"] in ("task", "schedule"), out


@pytest.mark.parametrize(("text", "want"), [
    ("compare to last year", "same period a year earlier"),
    ("how does it compare with last year?", "same period a year earlier"),
    ("versus last year", "same period a year earlier"),
    ("compare with the previous quarter", "previous period"),
    ("region", "by region"),
    ("regions please", "by region"),
    ("for each product line", "by product line"),
    ("what is driving this", "Why did"),
    ("explain", "Why did"),
    ("turn this into a dashboard", "Build a dashboard of cogs ratio"),
    ("dashboard by region", "Build a dashboard of cogs ratio by region"),
])
def test_common_wordings_of_the_suggestions(text, want):
    parent = {"understand": {"question": "What was cogs ratio last quarter?", "filters": [], "by": None}, "period_label": "Q3 2026"}
    out = resolve(parent, text, ["region", "product_line"], "cogs ratio")
    assert out["action"] == "task" and want in out["question"], out


@pytest.mark.parametrize("text", ["schedule every Monday", "every Monday", "send me this every Monday", "schedule it weekly"])
def test_schedule_wordings(text):
    parent = {"understand": {"question": "What was cogs ratio last quarter?", "filters": [], "by": None}, "period_label": None}
    out = resolve(parent, text, ["region"], "cogs ratio")
    assert out["action"] == "schedule" and out["schedule"]["weekdays"] == ["mon"], out


def test_a_schedule_with_no_time_asks_when_and_monthly_alone_is_a_trend():
    parent = {"understand": {"question": "What was cogs ratio last quarter?", "filters": [], "by": None}, "period_label": None}
    assert "Say when" in resolve(parent, "schedule this", ["region"], "cogs ratio")["message"]
    assert resolve(parent, "show it monthly", ["region"], "cogs ratio")["changed"] == {"intent": "trend"}


@pytest.mark.parametrize("text", SUGGESTED)
def test_refine_a_forecast_chart_through_the_api(seeded, text):
    """The case from the artifact viewer: a forecast chart refined with each suggested phrase, quotes and all."""
    lyst, owner = seeded
    first = ask(lyst, owner, "Forecast cogs ratio for the next 2 quarters")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post(f"/v1/tasks/{first['id']}/follow-up", headers=h, json={"text": f"“{text}”", "timezone": "UTC"})
        assert r.status_code == 202, r.text
        out = r.json()
    assert out["action"] in ("task", "schedule"), out
    if out["action"] == "task":
        lyst.runtime.drain()
        child = lyst.runtime.get(owner, out["task_id"])
        assert child["status"] == "done" and child["request"]["derived_from"]["task_id"] == first["id"]
