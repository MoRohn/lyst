# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Graph runs: plan, parallel Builders, an independent Checker, a Router with a bounded fix loop, learned rules,
the morning brief, the weekly review, and the Brain graph."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.graphs import BRIEF_LINES, DEFAULT_REQUIREMENTS, check, split_goal
from lyst.security import AccessDenied

PASSES = "What was revenue last quarter?"
AMBIGUOUS = "Which region has the lowest gross margin?"  # 'margin' names two metrics: needs input, every time


def settle(lyst, ws, run_id, rounds=12):
    """Drive the graph the way the worker tick does: Builders' tasks run, then the Router moves each branch on."""
    for _ in range(rounds):
        lyst.runtime.drain()
        lyst.graphs.advance(ws, run_id)
        if lyst.graphs._run(ws, run_id)["status"] != "running":
            break


def test_the_planner_splits_only_what_was_written():
    assert split_goal("1. Revenue last quarter\n- Margin by region; churn") == ["Revenue last quarter", "Margin by region",
                                                                            "churn"]
    assert split_goal("What was revenue last quarter?") == ["What was revenue last quarter?"]
    assert split_goal("revenue last quarter\nRevenue, last quarter!") == ["revenue last quarter"]  # same job once


def test_a_plan_waits_for_the_go(seeded):
    lyst, owner = seeded
    run = lyst.graphs.plan(owner, "northwind", f"{PASSES}\n{AMBIGUOUS}")
    assert run["status"] == "planned" and [b["status"] for b in run["branches"]] == ["planned", "planned"]
    assert not any(b["task_id"] for b in run["branches"])  # no Builder starts before the go
    assert lyst.runtime.list(owner, "northwind") == []
    assert {e["edge"] for e in run["edges"]} == {"plan"}
    with pytest.raises(ValueError):
        lyst.graphs.plan(owner, "northwind", "   ")


def test_pass_moves_forward_fail_loops_three_times_then_blocks_and_a_rule_is_learned(seeded):
    lyst, owner = seeded
    run = lyst.graphs.plan(owner, "northwind", f"{PASSES}\n{AMBIGUOUS}", intake={"proof": ["Q3"]})
    lyst.graphs.go(owner, "northwind", run["id"])
    started = lyst.graphs.get(owner, "northwind", run["id"])
    assert all(b["status"] == "building" and b["task_id"] for b in started["branches"])  # both Builders in parallel
    settle(lyst, "northwind", run["id"])
    run = lyst.graphs.get(owner, "northwind", run["id"])
    ok, bad = run["branches"]
    assert ok["status"] == "passed" and ok["attempts"] == 1
    ev = {r["id"]: r for r in ok["evidence"]}
    assert set(ev) == {"finished", "answered", "validated", "sourced", "reproduced", "mentions_1"} and all(r["passed"] for r in ev.values())
    assert "sha256" in ev["sourced"]["evidence"] and ev["finished"]["strength"] == "noise"
    assert "must-pass" in ev["validated"]["evidence"] and "Q3" in ev["answered"]["evidence"]
    # the failing branch went back to its Builder with the error, three attempts, then blocked; the others finished
    assert bad["status"] == "blocked" and bad["attempts"] == 3
    assert [a["outcome"] for a in bad["history"]] == ["fail", "fail", "fail"]
    assert len({a["task_id"] for a in bad["history"]}) == 3
    assert "more than one approved metric" in bad["last_reason"]
    edges = [e["edge"] for e in run["edges"] if e["branch_id"] == "b2"]
    assert edges.count("retry") == 2 and edges.count("blocked") == 1 and edges.count("learn") == 1
    assert run["status"] == "partial" and run["final"]["every_branch_ended"] and not run["final"]["all_passed"]
    assert run["final"]["requirements_without_evidence"] == []
    assert len(run["rules"]) == 1 and "Do not run it unchanged" in run["final"]["learned"][0]
    harness = lyst.graphs.harness(owner, "northwind")
    assert harness["learned"][0]["active"] and "## Learned rules" in harness["markdown"]
    links = {(lk["source"], lk["target"], lk["kind"]) for lk in run["graph"]["links"]}
    assert ("checker:b2", "builder:b2", "fail") in links and ("router", "merge", "pass") in links
    assert ("router", "planner", "learn") in links  # the learning edge goes back to the splitter
    assert run["merged"] == [{"branch": "b1", "title": ok["title"], "answer": run["merged"][0]["answer"],
                              "task_id": ok["task_id"], "proof": 6, "also": []}]
    assert "re-ran 3 recorded queries independently" in ev["reproduced"]["evidence"]
    assert run["final"]["timing"]["graph_seconds"] <= run["final"]["timing"]["as_a_chain_seconds"] + 1
    last = bad["history"][-1]["packet"]
    assert last["unit"] == "b2" and last["verdict"] == "red" and last["policy"] == "repair"
    assert "this branch only" in last["scope"] and last["evidence"]
    md = lyst.graphs.checks_md(owner, "northwind", run["id"])
    assert "| b1 " in md and "| pass |" in md and "| fail |" in md
    assert "flowchart LR" in lyst.graphs.graph_md(owner, "northwind", run["id"])


def test_the_next_run_never_takes_a_learned_failing_path_again(seeded):
    lyst, owner = seeded
    first = lyst.graphs.plan(owner, "northwind", AMBIGUOUS)
    lyst.graphs.go(owner, "northwind", first["id"])
    settle(lyst, "northwind", first["id"])
    again = lyst.graphs.rerun(owner, "northwind", first["id"])
    assert again["status"] == "planned" and again["rerun_of"] == first["id"]
    assert again["branches"][0]["warning"].startswith("Learned rule:")  # flagged when the plan is drawn
    lyst.graphs.go(owner, "northwind", again["id"])
    settle(lyst, "northwind", again["id"])
    again = lyst.graphs.get(owner, "northwind", again["id"])
    b = again["branches"][0]
    assert b["status"] == "blocked" and b["attempts"] == 1  # no retries spent on a known failure
    assert b["last_reason"].startswith("blocked by a learned rule")
    assert lyst.graphs.harness(owner, "northwind")["learned"][0]["hits"] == 1
    assert any("stopped early" in x for x in again["final"]["learned"])


def test_a_person_can_fix_a_blocked_branch_without_weakening_its_checks(seeded):
    lyst, owner = seeded
    run = lyst.graphs.plan(owner, "northwind", AMBIGUOUS, max_attempts=1)
    lyst.graphs.go(owner, "northwind", run["id"])
    settle(lyst, "northwind", run["id"])
    before = lyst.graphs.get(owner, "northwind", run["id"])["branches"][0]
    assert before["status"] == "blocked" and before["attempts"] == 1
    lyst.graphs.revise(owner, "northwind", run["id"], "b1", PASSES)
    settle(lyst, "northwind", run["id"])
    after = lyst.graphs.get(owner, "northwind", run["id"])
    assert after["status"] == "done" and after["branches"][0]["status"] == "passed"
    assert after["branches"][0]["requirements"] == before["requirements"]
    with pytest.raises(ValueError):
        lyst.graphs.revise(owner, "northwind", run["id"], "b1", "anything")  # only a blocked branch


def test_the_checker_never_takes_the_builders_word_for_it():
    done = {"id": "t_1", "status": "done", "error": None}
    claims = {**done, "result": {"status": "complete", "answer": "Revenue was $1M.",
                                 "confidence": {"checks": [{"name": "freshness", "passed": False, "detail": "stale",
                                                            "severity": "error"}]}}}
    by = {r["id"]: r for r in check(claims, DEFAULT_REQUIREMENTS)}
    assert by["answered"]["passed"] and not by["validated"]["passed"]  # a complete status is not proof
    unproven = {**done, "result": {"status": "complete", "answer": "Revenue was $1M.", "confidence": {"checks": []}}}
    by = {r["id"]: r for r in check(unproven, DEFAULT_REQUIREMENTS)}
    assert not by["validated"]["passed"] and "no validation check ran" in by["validated"]["evidence"]
    assert all(not r["passed"] for r in check(None, DEFAULT_REQUIREMENTS))
    odd = check(claims, [{"id": "x", "kind": "vibes", "text": "Feels right"}])
    assert not odd[0]["passed"]  # a requirement nothing can prove fails


def test_brief_is_at_most_fifteen_lines_and_review_names_what_failed(seeded):
    lyst, owner = seeded
    run = lyst.graphs.plan(owner, "northwind", f"{PASSES}\n{AMBIGUOUS}")
    lyst.graphs.go(owner, "northwind", run["id"])
    settle(lyst, "northwind", run["id"])
    brief = lyst.graphs.brief(owner, "northwind")
    assert 0 < len(brief["lines"]) <= BRIEF_LINES
    assert brief["done"] == 1 and brief["blocked"] == 1
    assert any(x.startswith("Done ·") and "proof:" in x for x in brief["lines"])
    assert any(x.startswith("Blocked ·") for x in brief["lines"])
    assert any(x.startswith("Graph ·") for x in brief["lines"])
    review = lyst.graphs.review(owner, "northwind")
    assert review["runs"] == 1 and review["failed_most"][0]["failures"] == 3
    assert "Fix or rephrase" in review["one_change"]


def test_who_may_do_what(seeded):
    lyst, owner = seeded
    stranger = lyst.guard.create_user("eve@else.test", "Eve")
    with pytest.raises(AccessDenied):
        lyst.graphs.plan(stranger, "northwind", PASSES)
    run = lyst.graphs.plan(owner, "northwind", AMBIGUOUS, max_attempts=2)
    with pytest.raises(AccessDenied):
        lyst.graphs.get(stranger, "northwind", run["id"])
    lyst.graphs.go(owner, "northwind", run["id"])
    with pytest.raises(ValueError):
        lyst.graphs.go(owner, "northwind", run["id"])  # a go is given once
    settle(lyst, "northwind", run["id"])
    rule = lyst.graphs.harness(owner, "northwind")["learned"][0]
    viewer = lyst.guard.create_user("val@northwind.test", "Val")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.graphs.retire_rule(viewer, "northwind", rule["id"])
    lyst.graphs.retire_rule(owner, "northwind", rule["id"])
    assert lyst.graphs.harness(owner, "northwind")["learned"][0]["active"] is False


def test_cancel_stops_open_branches(seeded):
    lyst, owner = seeded
    run = lyst.graphs.plan(owner, "northwind", PASSES)
    lyst.graphs.go(owner, "northwind", run["id"])
    out = lyst.graphs.cancel(owner, "northwind", run["id"])
    assert out["status"] == "cancelled" and out["branches"][0]["status"] == "cancelled"
    assert lyst.runtime.get(owner, out["branches"][0]["task_id"])["status"] == "cancelled"


def test_the_brain_graph_links_only_what_exists(seeded):
    lyst, owner = seeded
    run = lyst.graphs.plan(owner, "northwind", f"{PASSES}\n{AMBIGUOUS}")
    lyst.graphs.go(owner, "northwind", run["id"])
    settle(lyst, "northwind", run["id"])
    g = lyst.graphs.brain(owner, "northwind")
    ids = {n["id"] for n in g["nodes"]}
    assert all(lk["source"] in ids and lk["target"] in ids for lk in g["links"])
    kinds = g["counts"]
    for k in ("dataset", "metric", "run", "branch", "rule", "analysis"):
        assert kinds.get(k), k
    assert any(lk["rel"] == "reads" and lk["source"].startswith("metric:") for lk in g["links"])
    assert any(lk["rel"] == "built" for lk in g["links"])  # a branch links to the analysis its Builder made
    assert any(lk["rel"] == "learned from" for lk in g["links"])
    assert not any(n["kind"] == "column" for n in g["nodes"])
    quiet = lyst.graphs.brain(owner, "northwind", runs=False)
    assert not any(n["kind"] in ("run", "branch", "rule") for n in quiet["nodes"])


def test_graph_runs_over_http_with_the_worker_tick(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst)) as c:
        r = c.post("/v1/workspaces/northwind/graph-runs", headers=h,
                   json={"goal": PASSES, "intake": {"done": "an answer with proof", "proof": ["Q3"]}})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        assert c.post(f"/v1/workspaces/northwind/graph-runs/{rid}/go", headers=h).status_code == 200
        lyst.runtime.drain()
        lyst.graphs.tick()
        lyst.runtime.drain()
        lyst.graphs.tick()
        got = c.get(f"/v1/workspaces/northwind/graph-runs/{rid}", headers=h).json()
        assert got["status"] == "done", got["branches"]
        files = c.get(f"/v1/workspaces/northwind/graph-runs/{rid}/files", headers=h).json()
        assert files["checks.md"].startswith("# Checks") and "flowchart LR" in files["graph.md"]
        assert c.get("/v1/workspaces/northwind/graph/brief", headers=h).json()["done"] == 1
        assert c.get("/v1/workspaces/northwind/graph/review", headers=h).status_code == 200
        assert c.get("/v1/workspaces/northwind/brain/graph", headers=h).json()["counts"]["run"] == 1
        assert c.get("/v1/workspaces/northwind/graph-runs/nope", headers=h).status_code == 404
        assert c.post("/v1/workspaces/northwind/graph-runs", headers=h, json={"goal": ""}).status_code == 400


def test_lanes_open_on_blast_radius_not_confidence(seeded):
    lyst, owner = seeded
    approver = lyst.guard.create_user("ann@northwind.test", "Ann")
    lyst.guard.add_member(owner, "northwind", approver.user_id, "admin")
    run = lyst.graphs.plan(owner, "northwind", f"{PASSES}\nEmail the revenue summary to the board",
                           intake={"approval": "board"})
    lanes = {b["id"]: b["lane"] for b in run["branches"]}
    assert lanes == {"b1": "contained", "b2": "closed"}
    lyst.graphs.go(owner, "northwind", run["id"])
    settle(lyst, "northwind", run["id"])
    got = lyst.graphs.get(owner, "northwind", run["id"])
    held = got["branches"][1]
    assert held["status"] == "awaiting_approval" and held["hold"] == "start" and not held["task_id"]  # never started
    assert got["status"] == "running"  # a closed lane does not open by itself, whatever passes around it
    with pytest.raises(AccessDenied):
        lyst.graphs.approve(owner, "northwind", run["id"], "b2")  # the planner does not open their own closed lane
    lyst.graphs.approve(approver, "northwind", run["id"], "b2", approve=False)
    settle(lyst, "northwind", run["id"])
    got = lyst.graphs.get(owner, "northwind", run["id"])
    assert got["branches"][1]["status"] == "blocked" and got["status"] == "partial"
    assert "rejected by ann@northwind.test" in got["branches"][1]["last_reason"]


def test_an_edge_exists_only_when_a_named_value_crosses_it(seeded):
    lyst, owner = seeded
    with pytest.raises(ValueError):
        lyst.graphs.plan(owner, "northwind", "x", branches=["What was revenue in {b2}?", PASSES])  # not earlier
    run = lyst.graphs.plan(owner, "northwind", "x", branches=[PASSES, "What was revenue last year?",
                                                             "Explain {b1} to the team"])
    assert [b["after"] for b in run["branches"]] == [[], [], ["b1"]]
    feeds = [e for e in run["edges"] if e["edge"] == "feeds"]
    assert len(feeds) == 1 and feeds[0]["src"] == "checker:b1" and feeds[0]["dst"] == "builder:b3"
    lyst.graphs.go(owner, "northwind", run["id"])
    started = lyst.graphs.get(owner, "northwind", run["id"])
    assert [b["status"] for b in started["branches"]] == ["building", "building", "waiting"]  # b2 never waits on b1
    settle(lyst, "northwind", run["id"])
    b3 = lyst.graphs.get(owner, "northwind", run["id"])["branches"][2]
    assert b3["task_id"]
    request = lyst.runtime.get(owner, b3["task_id"])
    assert "Revenue was" in request["title"]  # b1's answer crossed the edge into b3's job


def test_optional_branches_skip_and_never_touch_is_refused(seeded):
    lyst, owner = seeded
    with pytest.raises(ValueError, match="never be touched"):
        lyst.graphs.plan(owner, "northwind", "Drop the payroll table", intake={"never": "payroll"})
    run = lyst.graphs.plan(owner, "northwind", f"{PASSES}\noptional: {AMBIGUOUS}", max_attempts=1)
    assert run["branches"][1]["optional"] and run["branches"][1]["job"] == AMBIGUOUS
    lyst.graphs.go(owner, "northwind", run["id"])
    settle(lyst, "northwind", run["id"])
    got = lyst.graphs.get(owner, "northwind", run["id"])
    assert got["branches"][1]["status"] == "skipped" and got["status"] == "done"
    assert "optional skipped" in got["final"]["summary"]


def test_an_accepted_fix_becomes_a_constraint_for_the_planner(seeded):
    lyst, owner = seeded
    run = lyst.graphs.plan(owner, "northwind", AMBIGUOUS, max_attempts=1)
    lyst.graphs.go(owner, "northwind", run["id"])
    settle(lyst, "northwind", run["id"])
    lyst.graphs.revise(owner, "northwind", run["id"], "b1", PASSES)
    settle(lyst, "northwind", run["id"])
    rules = lyst.graphs.harness(owner, "northwind")["learned"]
    rewrite = [r for r in rules if r["kind"] == "rewrite"]
    assert len(rewrite) == 1 and rewrite[0]["rewrite"] == PASSES
    nxt = lyst.graphs.rerun(owner, "northwind", run["id"])
    b = nxt["branches"][0]
    assert b["job"] == PASSES and b["original_job"] == AMBIGUOUS
    assert b["warning"].startswith("Rewritten by a learned constraint")
    lyst.graphs.go(owner, "northwind", nxt["id"])
    settle(lyst, "northwind", nxt["id"])
    done = lyst.graphs.get(owner, "northwind", nxt["id"])
    assert done["status"] == "done" and any("ran the fix" in x for x in done["final"]["learned"])


def test_a_spent_budget_stops_the_run_and_wakes_a_person(seeded):
    lyst, owner = seeded
    run = lyst.graphs.plan(owner, "northwind", PASSES, budget_minutes=1)
    lyst.graphs.go(owner, "northwind", run["id"])
    lyst.stores.workspace("northwind").execute("UPDATE graph_runs SET started_at = now() - INTERVAL 2 MINUTE WHERE id = ?",
                                               [run["id"]])
    lyst.graphs.advance("northwind", run["id"])
    got = lyst.graphs.get(owner, "northwind", run["id"])
    assert got["status"] == "partial" and got["branches"][0]["last_reason"].startswith("stopped:")
    assert lyst.stores.workspace("northwind").scalar("SELECT count(*) FROM findings WHERE kind = 'graph_run'") == 1
    harness = lyst.graphs.harness(owner, "northwind")
    assert {p["action"] for p in harness["policy"]} == {"retry", "skip", "repair", "escalate", "stop"}
    assert set(harness["contracts"]) == {"planner", "builder", "checker", "router", "merge"}
