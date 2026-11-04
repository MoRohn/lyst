# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
# Lyst's own module, maintained here; it grew out of the author's earlier analytics toolkit.
"""A decomposition tree: where a change came from, one best split at a time.

Like Power BI's decomposition tree with "AI splits", but deterministic and tested. At each
node the change (current - prior) is split by the dimension whose members deviate most
from proportional change: the score is sum over members of |change_m - change_node x
prior share_m|, the part of the change a uniform shift doesn't explain. The biggest
member contributions are expanded, to `depth` levels.

    tree = decompose(cur, prev, value="won_revenue", dims=["office", "product", "sector"], depth=3)
    flat = flatten(tree)            # rows: path, dim, member, prior, current, change, share_of_parent

Sums are exact at every level: children's changes add up to their parent's change.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

__all__ = ["Node", "decompose", "flatten", "split_scores"]


@dataclass
class Node:
    dim: str | None
    member: str | None
    prior: float
    current: float
    path: tuple = ()
    split_dim: str | None = None
    children: list = field(default_factory=list)

    @property
    def change(self) -> float:
        return self.current - self.prior


def _totals(df: pd.DataFrame, dim: str, value: str) -> pd.Series:
    return df.groupby(dim, observed=True)[value].sum() if len(df) else pd.Series(dtype=float)


def split_scores(cur: pd.DataFrame, prev: pd.DataFrame, value: str, dims: list[str]) -> dict[str, float]:
    """How much of the change each dimension's member mix explains beyond a uniform shift."""
    total_prev, total_cur = prev[value].sum(), cur[value].sum()
    change = total_cur - total_prev
    out = {}
    for d in dims:
        c, p = _totals(cur, d, value), _totals(prev, d, value)
        idx = c.index.union(p.index)
        c, p = c.reindex(idx, fill_value=0), p.reindex(idx, fill_value=0)
        share = p / total_prev if total_prev else pd.Series(1 / max(1, len(idx)), index=idx)
        out[d] = float((c - p - change * share).abs().sum())
    return out


def decompose(cur: pd.DataFrame, prev: pd.DataFrame, *, value: str, dims: list[str], depth: int = 3,
              top: int = 4, min_share: float = 0.05) -> Node:
    """The tree. `top` members per split (by |change|), and only those whose |change| is at
    least `min_share` of their parent's absolute member changes; the rest stay unexpanded."""
    root = Node(None, None, float(prev[value].sum()), float(cur[value].sum()))

    def grow(node: Node, c: pd.DataFrame, p: pd.DataFrame, left: list[str], level: int):
        if level >= depth or not left or (len(c) == 0 and len(p) == 0):
            return
        scores = split_scores(c, p, value, left)
        best = max(scores, key=scores.get)
        node.split_dim = best
        ct, pt = _totals(c, best, value), _totals(p, best, value)
        idx = ct.index.union(pt.index)
        ct, pt = ct.reindex(idx, fill_value=0), pt.reindex(idx, fill_value=0)
        ch = (ct - pt)
        for m in idx:
            node.children.append(Node(best, m, float(pt[m]), float(ct[m]), node.path + ((best, m),)))
        node.children.sort(key=lambda n: -abs(n.change))
        denom = float(ch.abs().sum()) or 1.0
        rest = [d for d in left if d != best]
        for child in node.children[:top]:
            if abs(child.change) / denom >= min_share:
                grow(child, c[c[best] == child.member], p[p[best] == child.member], rest, level + 1)

    grow(root, cur, prev, list(dims), 0)
    return root


def flatten(root: Node) -> pd.DataFrame:
    rows = []

    def walk(n: Node, parent: Node | None):
        if parent is not None:
            rows.append({"level": len(n.path), "path": " > ".join(f"{m}" for _, m in n.path), "dim": n.dim,
                         "member": n.member, "prior": n.prior, "current": n.current, "change": n.change,
                         "share_of_parent": n.change / parent.change if parent.change else float("nan"),
                         "split_dim": n.split_dim})
        for ch in n.children:
            walk(ch, n)

    walk(root, None)
    return pd.DataFrame(rows)
