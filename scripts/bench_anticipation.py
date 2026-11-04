# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What one anticipation cycle costs on the demo workspace with every anticipator busy: two approved schedules due
soon, two monitors, ten finished analyses, a connection-free credential set and the attention rules.

    uv run python scripts/bench_anticipation.py [cycles]

Reports the median and p95 wall time of a cycle at each level, after the first (which prepares everything) and on
repeats (which only re-check validity), plus what the first cycle recorded as resources.
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("LYST_MASTER_KEY", "ab" * 32)
os.environ["LYST_KEYRING"] = "0"
os.environ["LYST_CATALOG_AUTO"] = "0"
os.environ.pop("LYST_OPENROUTER_API_KEY", None)

from lyst import demo  # noqa: E402
from lyst.app import Lyst  # noqa: E402
from lyst.config import load_settings  # noqa: E402

WS = "northwind"


def main() -> None:
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    home = Path(tempfile.mkdtemp()) / "home"
    lyst = Lyst.build(load_settings(home), lease_seconds=30)
    owner = lyst.guard.create_user("bench@northwind.test", "Bench")
    demo.seed(lyst, owner)
    for title, q in (("Monthly finance review", "Review revenue and operating margin for the latest month"),
                     ("Support capacity", "Review support tickets for the latest month")):
        s = lyst.runtime.create_schedule(owner, WS, title, q, 2592000)
        lyst.runtime.decide(owner, s["approval_id"], True)
        lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?",
                                    [datetime.now(UTC) + timedelta(minutes=40), s["schedule_id"]])
    for metric in ("operating_margin", "revenue"):
        m = lyst.monitors.create(owner, WS, metric=metric, grain="month", threshold_sigma=6.0)
        lyst.runtime.decide(owner, m["approval_id"], True)
    for q in ["Why did operating margin fall in Q3 2026?", "How did revenue change in Q3 2026?"] * 5:
        lyst.runtime.submit(owner, WS, q)
    lyst.runtime.drain()
    print(f"cycles per level: {cycles}")
    for level in (1, 2, 3):
        lyst.stores.workspace(WS).execute("DELETE FROM anticipation_events")
        lyst.stores.workspace(WS).execute("DELETE FROM anticipations")
        lyst.stores.workspace(WS).execute("DELETE FROM anticipation_usage")
        lyst.anticipation.set_settings(owner, WS, {"level": level, "daily_prepare_limit": 10000, "daily_compute_ms": 86_400_000, "daily_investigation_limit": 500})
        t0 = time.perf_counter()
        first = lyst.anticipation.cycle(WS)
        first_ms = (time.perf_counter() - t0) * 1000
        times = []
        for _ in range(cycles):
            t0 = time.perf_counter()
            lyst.anticipation.cycle(WS)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        items = lyst.stores.workspace(WS).query("SELECT kind, count(*) AS n FROM anticipations GROUP BY kind ORDER BY kind")
        print(f"level {level}: first cycle {first_ms:.0f} ms (anticipated {first['anticipated']}, prepared {first['prepared']}); "
              f"repeat median {statistics.median(times):.0f} ms, p95 {times[int(len(times) * 0.95) - 1]:.0f} ms; "
              f"items {', '.join(f'{r['kind']}={r['n']}' for r in items)}")
    lyst.close()


if __name__ == "__main__":
    main()
