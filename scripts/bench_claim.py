# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Claim latency with a deep queue across several workspaces (priority + fairness pick). Usage:
    uv run python scripts/bench_claim.py [queued] [workspaces]
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LYST_MASTER_KEY", "ab" * 32)
os.environ.setdefault("LYST_KEYRING", "0")
os.environ.setdefault("LYST_CATALOG_AUTO", "0")

from lyst.app import Lyst  # noqa: E402
from lyst.config import load_settings  # noqa: E402


def main() -> None:
    queued = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    n_ws = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    with tempfile.TemporaryDirectory() as home:
        app = Lyst.build(load_settings(Path(home)))
        owner = app.guard.create_user("bench@lyst.test", "Bench")
        for i in range(n_ws):
            app.guard.create_workspace(owner, f"ws{i}", f"WS {i}")
        kinds = ("analysis", "scheduled", "investigation", "evaluation")
        for i in range(queued):
            app.runtime.submit(owner, f"ws{i % n_ws}", f"question {i}", kind=kinds[i % 4])
        times = []
        for _ in range(50):
            t0 = time.perf_counter()
            assert app.runtime.claim()
            times.append((time.perf_counter() - t0) * 1000)
        print(f"queued={queued} workspaces={n_ws} claim ms: p50={statistics.median(times):.1f} "
              f"p95={sorted(times)[int(0.95 * len(times)) - 1]:.1f} max={max(times):.1f}")
        app.close()


if __name__ == "__main__":
    main()
