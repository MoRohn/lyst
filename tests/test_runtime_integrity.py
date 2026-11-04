# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Task-runtime integrity: leases, fencing and cancellation under concurrency (docs/ENGINEERING_EXCELLENCE.md R1-R3)."""

import threading
import time

from lyst.runtime import TaskRuntime


def _slow(real, name_match, seconds, calls):
    def run(name, ctx, args):
        if name == name_match:
            calls.append(threading.current_thread().name)
            time.sleep(seconds)
        return real(name, ctx, args)
    return run


def test_a_step_longer_than_the_lease_is_not_run_twice(seeded, monkeypatch):
    """R1: the lease was renewed only between waves, so a step longer than the lease let a second worker
    claim the same task and run the in-flight step again."""
    lyst, owner = seeded
    calls: list[str] = []
    monkeypatch.setattr(lyst.services.registry, "run", _slow(lyst.services.registry.run, "deliver", 2.5, calls))
    a = TaskRuntime(lyst.stores, lyst.services, worker_id="wa", lease_seconds=1)
    b = TaskRuntime(lyst.stores, lyst.services, worker_id="wb", lease_seconds=1)
    tid = a.submit(owner, "northwind", "What was revenue last quarter?")
    assert a.claim() == tid
    runner = threading.Thread(target=a.run_task, args=(tid,))
    runner.start()
    stolen = []
    deadline = time.monotonic() + 6
    while runner.is_alive() and time.monotonic() < deadline:
        got = b.claim()
        if got:
            stolen.append(got)
            b.run_task(got)
        time.sleep(0.2)
    runner.join(timeout=10)
    assert stolen == [] and len(calls) == 1
    assert lyst.runtime.get(owner, tid)["status"] == "done"


def test_cancel_during_the_last_step_is_not_overwritten_by_done(seeded, monkeypatch):
    """R2: the final 'done' write (and _fail, and the approval gate) updated the task unconditionally, so a
    cancel that landed while a step ran was silently replaced."""
    lyst, owner = seeded
    calls: list[str] = []
    monkeypatch.setattr(lyst.services.registry, "run", _slow(lyst.services.registry.run, "learn", 1.0, calls))
    rt = lyst.runtime
    tid = rt.submit(owner, "northwind", "What was revenue last quarter?")
    assert rt.claim() == tid
    runner = threading.Thread(target=rt.run_task, args=(tid,))
    runner.start()
    while not calls:
        time.sleep(0.05)
    rt.cancel(owner, tid)
    runner.join(timeout=10)
    t = rt.get(owner, tid)
    assert t["status"] == "cancelled" and t["result"] is None


def test_a_task_that_keeps_killing_its_worker_is_abandoned(seeded):
    """R3: attempts was counted but never checked, so a task that crashed its worker was re-claimed forever."""
    lyst, owner = seeded
    rt = lyst.runtime
    tid = rt.submit(owner, "northwind", "What was revenue last quarter?")
    lyst.stores.control.execute("UPDATE tasks SET status = 'running', takeovers = 5, "
                                "lease_until = now() - INTERVAL 1 SECOND WHERE id = ?", [tid])
    assert rt.claim() is None
    t = rt.get(owner, tid)
    assert t["status"] == "failed" and "abandoned" in t["error"]


def test_a_write_that_times_out_in_the_queue_is_withdrawn_not_applied_later(tmp_path):
    """S-DB1: Database.write raised TimeoutError while the queued write stayed queued and was committed later,
    so a caller that reported failure (or retried) saw the write applied anyway."""
    import pytest

    from lyst.store.db import Database

    db = Database(tmp_path / "t.duckdb", name="t", master_key=b"k" * 32, encrypt=False)
    db.execute("CREATE TABLE t (v INTEGER)")
    gate = threading.Event()
    blocker = threading.Thread(target=lambda: db.write(lambda c: gate.wait(5)))
    blocker.start()
    time.sleep(0.1)
    with pytest.raises(TimeoutError, match="withdrawn"):
        db.write(lambda c: c.execute("INSERT INTO t VALUES (1)"), timeout=0.2)
    gate.set()
    blocker.join()
    db.execute("INSERT INTO t VALUES (2)")
    assert [r["v"] for r in db.query("SELECT v FROM t")] == [2]
    db.close()


def test_model_wording_with_spelled_out_scales_is_verified_not_dropped():
    """N1 (seen live on OpenRouter, 4 of 5 answers): "$25.41 million" parsed as 25.41, so correct model wording was
    rejected as unverified and the template kept. Invented figures must still be caught."""
    from lyst.analyst.engine import _numbers, unbacked_numbers

    answer = "Revenue was $25.41M in Q3 2026, +$316.0K (+1.3%) versus Q2 2026 ($25.10M), and +$2.69M versus Q3 2025."
    allowed = [x for n in _numbers(answer) for x in (n, n / 100, n * 100)]
    good = ("Q3 2026 revenue of $25.41 million rose 1.3% ($316,000) from Q2 2026's $25.10 million and was "
            "$2.69 million above Q3 2025.")
    assert unbacked_numbers(good, allowed) == []
    assert unbacked_numbers("Revenue of $30 million rose 4.2% in Q3 2026.", allowed) == [30e6, 0.042]
    assert _numbers("down 3.5 percentage points") == [0.035] and _numbers("12 m units") == [12.0]
