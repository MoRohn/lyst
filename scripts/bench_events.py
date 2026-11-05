# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Event engine benchmark: concurrent emission through the outbox, duplicate suppression under contention, and rule
evaluation throughput. Runs on a throwaway home.

    LYST_ENCRYPT=0 uv run python scripts/bench_events.py [--events 2000] [--threads 8] [--rules 6]
"""

from __future__ import annotations

import argparse
import os
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=2000)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--rules", type=int, default=6)
    a = ap.parse_args()
    os.environ.setdefault("LYST_MASTER_KEY", "ab" * 32)
    os.environ.setdefault("LYST_KEYRING", "0")
    from lyst import demo
    from lyst.app import Lyst
    from lyst.config import load_settings

    home = Path(tempfile.mkdtemp(prefix="lyst-bench-events-"))
    lyst = Lyst.build(load_settings(home))
    owner = lyst.guard.create_user("bench@northwind.test", "Bench")
    demo.seed(lyst, owner)
    ev = lyst.events
    for i in range(a.rules):  # cheap rules that match some events and notify (suppressed per subject)
        spec = {"name": f"bench rule {i}", "on": ["external.bench.*"], "autonomy": "prepare", "suppress_seconds": 3600,
                "max_per_day": 500, "when": {"field": "data.n", "op": "gte", "value": i * 100},
                "action": {"kind": "notify", "summary": "bench"}}
        lyst.autonomy.activate(owner, "northwind", lyst.autonomy.save(owner, "northwind", spec)["id"])
    ev.dispatch()

    lat: list[float] = []

    def one(n: int) -> None:
        t = time.perf_counter()
        ev.emit("northwind", f"external.bench.e{n % 7}", subject=("thing", str(n % 50)), data={"n": n},
                source="api", dedupe_key=f"bench:{n % (a.events // 2)}")  # every key is sent twice
        lat.append(time.perf_counter() - t)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(a.threads) as pool:
        list(pool.map(one, range(a.events)))
    emit_s = time.perf_counter() - t0
    stored = lyst.stores.control.scalar("SELECT count(*) FROM platform_events WHERE type LIKE 'external.bench.%'")
    t1 = time.perf_counter()
    handled = 0
    while True:
        n = ev.dispatch(limit=500)["handled"]
        handled += n
        if not n:
            break
    disp_s = time.perf_counter() - t1
    acts = lyst.stores.workspace("northwind").scalar("SELECT count(*) FROM activities")
    evals = lyst.stores.workspace("northwind").scalar("SELECT count(*) FROM rule_evaluations")
    lat.sort()
    print(f"emitted {a.events} ({a.threads} threads, each dedupe key twice) in {emit_s:.2f}s "
          f"= {a.events / emit_s:.0f}/s; p50 {statistics.median(lat) * 1000:.1f} ms, p95 {lat[int(len(lat) * .95)] * 1000:.1f} ms")
    print(f"stored {stored} events (expected {a.events // 2}: duplicates suppressed)")
    print(f"dispatched {handled} deliveries to {len(ev.consumers)} consumers in {disp_s:.2f}s = {handled / max(disp_s, 1e-9):.0f}/s; "
          f"{evals} rule evaluations, {acts} activities (suppression per subject keeps this small)")
    lyst.close()


if __name__ == "__main__":
    main()
