# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Calibrate early warning on synthetic monthly series (30 months, Gaussian noise sigma 8 around 1000).

* False positives: pure noise, and noise with a single 2.5-sigma blip in the last month.
* Detection: a decline that starts four months before the end, `step` sigma deeper each month.
* Lead: the series is replayed month by month through a six-month decline; the month early warning first raises is
  compared with the month the monitor's own rule (robust z of the latest month >= 3) first alerts.

    uv run python scripts/bench_earlywarning.py [runs]
"""

from __future__ import annotations

import sys
import warnings

import numpy as np
import pandas as pd

from lyst.anticipate.earlywarning import assess

warnings.filterwarnings("ignore")
IDX = pd.date_range("2024-04-01", periods=30, freq="MS")


def rate(make, runs: int) -> tuple[float, float]:
    raised = early = 0
    for seed in range(runs):
        a = assess(pd.Series(make(np.random.default_rng(seed)), index=IDX), "month", 3.0, True)
        if a and a["raise"]:
            raised += 1
            early += abs(a["latest_z"]) < 3.0
    return raised / runs, (early / raised if raised else 0.0)


def lead(step: float, runs: int) -> dict[str, float]:
    earlier = same = later = missed = 0
    leads = []
    for seed in range(runs):
        v = 1000 + np.random.default_rng(seed).normal(0, 8, 32)
        v[-6:] -= 8 * step * np.arange(1, 7)
        warn = mon = None
        for k in range(1, 7):
            n = 26 + k
            a = assess(pd.Series(v[:n], index=pd.date_range("2024-04-01", periods=n, freq="MS")), "month", 3.0, True)
            if warn is None and a and a["raise"]:
                warn = k
            if mon is None and a and abs(a["latest_z"]) >= 3.0:
                mon = k
        if warn is None:
            missed += 1
            continue
        mon = mon or 7
        leads.append(mon - warn)
        earlier += warn < mon
        same += warn == mon
        later += warn > mon
    n = max(runs - missed, 1)
    return {"earlier": earlier / n, "same": same / n, "later": later / n, "missed": missed / runs,
            "mean_lead": float(np.mean(leads)) if leads else 0.0}


def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    noise = lambda r: 1000 + r.normal(0, 8, 30)  # noqa: E731

    def blip(r):
        v = noise(r)
        v[-1] -= 20
        return v

    print(f"runs per case: {runs}")
    print(f"false positives, noise:            {rate(noise, runs)[0]:.1%}")
    print(f"false positives, noise + blip:     {rate(blip, runs)[0]:.1%}")
    for step in (0.5, 1.0, 1.5, 2.5):
        def decline(r, step=step):
            v = noise(r)
            v[-4:] -= 8 * step * np.arange(1, 5)
            return v
        det, _ = rate(decline, runs)
        ld = lead(step, runs)
        print(f"decline {step:.1f} sigma/month: detected after 4 months {det:.1%}; replayed: earlier than the monitor "
              f"{ld['earlier']:.0%}, same month {ld['same']:.0%}, later {ld['later']:.0%}, never {ld['missed']:.0%}, "
              f"mean lead {ld['mean_lead']:+.1f} months")


if __name__ == "__main__":
    main()
