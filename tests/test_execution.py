# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""DAG execution: independent steps run in parallel, optional steps skip instead of failing, failures are
classified, verification intensity follows consequence, and resume still works with out-of-order steps."""

import threading
import time

import pytest

from lyst.runtime import TaskRuntime, classify_failure
from lyst.security import AccessDenied

from .conftest import ask


def test_independent_steps_run_in_parallel(seeded, monkeypatch):
    lyst, owner = seeded
    real = lyst.services.registry.run
    concurrent, peak, lock = {"n": 0}, {"n": 0}, threading.Lock()

    def slow(name, ctx, args):
        if name == "metric_value":
            with lock:
                concurrent["n"] += 1
                peak["n"] = max(peak["n"], concurrent["n"])
            time.sleep(0.4)
            try:
                return real(name, ctx, args)
            finally:
                with lock:
                    concurrent["n"] -= 1
        return real(name, ctx, args)

    monkeypatch.setattr(lyst.services.registry, "run", slow)
    start = time.perf_counter()
    t = ask(lyst, owner, "Why did operating margin decline last quarter?")
    elapsed = time.perf_counter() - start
    assert t["status"] == "done"
    assert peak["n"] == 3, "current, prior and year_ago should overlap"
    assert elapsed < 2.5, f"took {elapsed:.2f}s"  # peak == 3 is the real proof; this guards against a 3 × 0.4 s serial run plus the rest
    plan = t["plan"]["steps"]
    drivers = next(s for s in plan if s["key"] == "drivers")
    assert set(drivers["after"]) == {"current", "prior"}
    assert next(s for s in plan if s["key"] == "validate")["after"]


def test_optional_step_failure_is_skipped_not_fatal(seeded, monkeypatch):
    lyst, owner = seeded
    real = lyst.services.registry.run

    def broken(name, ctx, args):
        if name == "anomaly_scan":
            raise RuntimeError("stl exploded")
        return real(name, ctx, args)

    monkeypatch.setattr(lyst.services.registry, "run", broken)
    t = ask(lyst, owner, "Show the operating margin trend over time")
    assert t["status"] == "done"
    step = next(s for s in t["steps"] if s["capability"] == "anomaly_scan")
    assert step["status"] == "skipped" and step["error"].startswith("transient: RuntimeError")
    assert any(e["kind"] == "step_skipped" for e in t["events"])
    assert t["result"]["charts"][0]["type"] == "line"


def test_failure_classes_decide_retries(seeded, monkeypatch):
    lyst, owner = seeded
    assert classify_failure(AccessDenied("x")) == "permission"
    assert classify_failure(ValueError("x")) == "input"
    assert classify_failure(RuntimeError("x")) == "transient"
    calls = {"n": 0}
    real = lyst.services.registry.run

    def once(name, ctx, args):
        if name == "driver_analysis":
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("blip")
        return real(name, ctx, args)

    monkeypatch.setattr(lyst.services.registry, "run", once)
    t = ask(lyst, owner, "Why did revenue change last quarter?")
    assert t["status"] == "done" and calls["n"] == 2  # retried once, then fine

    def denied(name, ctx, args):
        if name == "driver_analysis":
            calls["n"] += 1
            raise AccessDenied("no")
        return real(name, ctx, args)

    calls["n"] = 0
    monkeypatch.setattr(lyst.services.registry, "run", denied)
    t = ask(lyst, owner, "Why did revenue change last quarter?")
    assert t["status"] == "failed" and calls["n"] == 1 and "(permission)" in t["error"]


def test_verification_intensity_follows_consequence(seeded):
    lyst, owner = seeded
    fast = ask(lyst, owner, "What was revenue last quarter?")
    names = {c["name"] for c in fast["result"]["confidence"]["checks"]}
    assert "independent_recompute" not in names and fast["plan"]["verification"] == "low"
    board = ask(lyst, owner, "What was operating margin last quarter? This goes to the board.")
    checks = {c["name"]: c for c in board["result"]["confidence"]["checks"]}
    assert board["plan"]["verification"] == "high"
    assert checks["independent_recompute"]["passed"], checks["independent_recompute"]["detail"]
    assert "pandas over" in checks["independent_recompute"]["detail"]
    deep = ask(lyst, owner, "Investigate why operating margin fell last quarter and test alternative explanations")
    assert deep["plan"]["verification"] == "high"
    assert {c["name"] for c in deep["result"]["confidence"]["checks"]} >= {"independent_recompute", "hypotheses_decided"}


class Crash(BaseException):
    pass


def test_resume_mid_wave_with_parallel_steps(seeded, monkeypatch):
    lyst, owner = seeded
    real = lyst.services.registry.run
    hits = {"n": 0}

    def crash_once(name, ctx, args):
        if name == "metric_value" and args["period"]["label"].startswith("Q2") and hits["n"] == 0:
            hits["n"] += 1
            raise Crash()
        return real(name, ctx, args)

    monkeypatch.setattr(lyst.services.registry, "run", crash_once)
    tid = lyst.runtime.submit(owner, "northwind", "Why did operating margin decline last quarter?")
    with pytest.raises(Crash):
        lyst.runtime.run_once()
    lyst.stores.control.execute("UPDATE tasks SET lease_until = now() - INTERVAL 1 SECOND WHERE id = ?", [tid])
    other = TaskRuntime(lyst.stores, lyst.services, worker_id="w2")
    assert other.run_once() == tid
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "done" and all(s["status"] == "done" for s in t["steps"])
    clean = ask(lyst, owner, "Why did operating margin decline last quarter?")
    assert clean["result"]["answer"] == t["result"]["answer"]
