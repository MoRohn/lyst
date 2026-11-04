# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario 8, a large analytical workload, measured in-process on a fresh installation.

    uv run python scripts/bench_large.py --rows 5000000

Generates a synthetic sales ledger (date, region, product, amount, cost) with a known total, ingests it,
defines a revenue metric, then measures: ingest time, peak resident memory, one guarded SQL aggregation, and
Analyst questions end to end (no model), checking each answer against an independent numpy calculation.
Numbers describe the machine that ran it; nothing here is a capacity guarantee.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path


def peak_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=5_000_000)
    args = ap.parse_args()
    home = Path(tempfile.mkdtemp(prefix="lyst-bench-"))
    os.environ.update(LYST_MASTER_KEY="cd" * 32, LYST_KEYRING="0", LYST_HOME=str(home))
    import numpy as np
    import pyarrow as pa

    from lyst.app import Lyst
    from lyst.config import load_settings
    from lyst.semantic import Measure, MetricSpec

    out: dict = {"rows": args.rows}
    rng = np.random.default_rng(7)
    start = np.datetime64("2024-01-01")
    days = rng.integers(0, 1035, args.rows)  # 2024-01-01 .. 2026-10-31
    regions = np.array(["EMEA", "AMER", "APAC", "LATAM"])[rng.integers(0, 4, args.rows)]
    amount = np.round(rng.gamma(2.0, 50.0, args.rows), 2)
    table = pa.table({"day": (start + days).astype("datetime64[D]"), "region": regions,
                      "product": rng.integers(0, 5000, args.rows).astype(str), "amount": amount,
                      "cost": np.round(amount * rng.uniform(0.4, 0.9, args.rows), 2)})
    out["peak_mb_after_generate"] = round(peak_mb())

    t0 = time.perf_counter()
    lyst = Lyst.build(load_settings(home))
    out["startup_s"] = round(time.perf_counter() - t0, 2)
    owner = lyst.guard.create_user("bench@lyst.test", "Bench")
    lyst.guard.create_workspace(owner, "bench", "Bench")
    t0 = time.perf_counter()
    lyst.data.ingest_arrow(owner, "bench", "sales", table, source="bench", title="Sales ledger")
    out["ingest_s"] = round(time.perf_counter() - t0, 2)
    out["peak_mb_after_ingest"] = round(peak_mb())
    lyst.semantic.define(owner, "bench", MetricSpec(name="revenue", title="Revenue", dataset="sales",
                                                    measure=Measure(agg="sum", column="amount"), time_column="day",
                                                    dimensions=["region"], format="currency"))
    t0 = time.perf_counter()
    q = lyst.data.query(owner, "bench", "select region, sum(amount) as revenue, count(*) as n from sales group by region")
    out["sql_group_by_s"] = round(time.perf_counter() - t0, 3)
    got = {r[0]: r[1] for r in q.rows}
    expected = {r: float(amount[regions == r].sum()) for r in ["EMEA", "AMER", "APAC", "LATAM"]}
    out["sql_matches_numpy"] = all(abs(got[k] - v) < 0.5 for k, v in expected.items())
    q3 = (days >= (np.datetime64("2026-07-01") - start).astype(int)) & (days < (np.datetime64("2026-10-01") - start).astype(int))
    out["independent_q3_2026_revenue"] = round(float(amount[q3].sum()), 2)

    # Analyst end to end: the last full quarter before today (2026-11) is Q3 2026
    for question in ["What was revenue last quarter?", "What was revenue last quarter by region?"]:
        t0 = time.perf_counter()
        tid = lyst.runtime.submit(owner, "bench", question)
        lyst.runtime.drain()
        t = lyst.runtime.get(owner, tid)
        key = "analyst_s:" + question
        out[key] = round(time.perf_counter() - t0, 2)
        out["status:" + question] = f"{t['status']} / {t.get('outcome')}"
        out["answer:" + question] = (t["result"] or {}).get("answer", t.get("error"))[:240]
    out["peak_mb_end"] = round(peak_mb())
    lyst.close()
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
