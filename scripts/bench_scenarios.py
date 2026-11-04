# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario engine timings on the demo driver model: first what-if (queries data), repeated what-if (cached base),
incremental re-evaluation after one assumption, sensitivity + cases + 10,000-draw simulation.
    uv run python scripts/bench_scenarios.py
"""

from __future__ import annotations

import os
import statistics
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LYST_MASTER_KEY", "ab" * 32)
os.environ.setdefault("LYST_KEYRING", "0")
os.environ.setdefault("LYST_CATALOG_AUTO", "0")

from lyst import demo  # noqa: E402
from lyst.app import Lyst  # noqa: E402
from lyst.config import load_settings  # noqa: E402

Q = "What happens to operating margin if labor costs increase 6% while revenue grows 3%?"


def ms(fn, n=20):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = fn()
        out.append((time.perf_counter() - t0) * 1000)
    return statistics.median(out), r


def main() -> None:
    with tempfile.TemporaryDirectory() as home:
        app = Lyst.build(load_settings(Path(home)))
        o = app.guard.create_user("bench@lyst.test", "Bench")
        demo.seed(app, o)
        sc = app.services.scenarios
        t0 = time.perf_counter()
        first = sc.ask(o, "northwind", Q)
        cold = (time.perf_counter() - t0) * 1000
        warm, _ = ms(lambda: sc.ask(o, "northwind", Q))
        inc, _ = ms(lambda: sc.evaluate(o, "northwind", "pnl_drivers", {**first["overrides"], "labor_share": {"value": 0.6}},
                                        previous=first))
        full, _ = ms(lambda: sc.analyze(o, "northwind", "pnl_drivers", first["overrides"]), n=5)
        print(f"what-if first {cold:.0f} ms; repeated {warm:.1f} ms; one assumption changed {inc:.1f} ms; "
              f"sensitivity+cases+10k simulation {full:.0f} ms")
        app.close()


if __name__ == "__main__":
    main()
