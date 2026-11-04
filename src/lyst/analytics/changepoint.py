# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Change-point detection for short business series (quarters, months).

Binary segmentation on a mean-shift model: split where the sum of squared deviations falls the most,
keep the split when the fall beats a BIC-style penalty, recurse on both sides. Deterministic, no
dependencies beyond numpy, and honest on short series: with fewer than four points nothing is reported.

    breaks = change_points([1, 1, 1, 0.7, 0.7, 0.7])   -> [Break(index=3, before=1.0, after=0.7, ...)]
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Break:
    index: int          # first point of the new regime
    before: float       # mean of the segment ending at index-1
    after: float        # mean of the segment starting at index
    shift: float        # after - before
    gain: float         # reduction in squared error the split buys
    penalty: float      # what it had to beat


def _sse(x: np.ndarray) -> float:
    return float(((x - x.mean()) ** 2).sum()) if len(x) else 0.0


def change_points(values, *, min_segment: int = 2, max_breaks: int = 3, penalty_scale: float = 1.0) -> list[Break]:
    x = np.asarray([v for v in values], dtype=float)
    n = len(x)
    if n < 2 * min_segment or np.isnan(x).any():
        return []
    # noise scale from first differences (robust to the shifts we are looking for)
    diffs = np.diff(x)
    mad = float(np.median(np.abs(diffs - np.median(diffs)))) if len(diffs) > 2 else 0.0
    sigma = mad / 0.6745 / math.sqrt(2)  # a difference of two noisy points has variance 2·sigma²
    if sigma <= 0:
        # flat segments with a few jumps: estimate noise from the smaller 80% of the differences
        small = np.sort(np.abs(diffs))[: max(1, int(0.8 * len(diffs)))]
        sigma = float(small.std()) if len(small) > 1 else 0.0
    if sigma <= 0:
        sigma = 1e-6 * (float(np.ptp(x)) or 1.0)
    sigma2 = sigma * sigma
    penalty = penalty_scale * 2 * sigma2 * math.log(n)
    breaks: list[Break] = []

    def split(lo: int, hi: int) -> None:
        if len(breaks) >= max_breaks or hi - lo < 2 * min_segment:
            return
        seg = x[lo:hi]
        base = _sse(seg)
        best, best_gain = None, 0.0
        for k in range(min_segment, len(seg) - min_segment + 1):
            gain = base - _sse(seg[:k]) - _sse(seg[k:])
            if gain > best_gain:
                best, best_gain = k, gain
        if best is None or best_gain <= penalty:
            return
        idx = lo + best
        breaks.append(Break(idx, float(x[lo:idx].mean()), float(x[idx:hi].mean()),
                            float(x[idx:hi].mean() - x[lo:idx].mean()), best_gain, penalty))
        split(lo, idx)
        split(idx, hi)

    split(0, n)
    return sorted(breaks, key=lambda b: b.index)
