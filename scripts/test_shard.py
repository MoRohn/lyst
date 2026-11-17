# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Print the test files for one CI shard: ``python scripts/test_shard.py INDEX COUNT``.

Files are dealt largest first to the lightest shard (by file size, a stand-in for run time), so every file lands
in exactly one shard and the shards finish at about the same time. Deterministic: the same tree gives the same
split. ``--check`` confirms the shards together cover every test file once."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent / "tests"


def shards(count: int) -> list[list[str]]:
    files = sorted(TESTS.glob("test_*.py"), key=lambda p: (-p.stat().st_size, p.name))
    load = [0] * count
    out: list[list[str]] = [[] for _ in range(count)]
    for f in files:
        i = min(range(count), key=lambda k: (load[k], k))
        load[i] += f.stat().st_size
        out[i].append(f"tests/{f.name}")
    return [sorted(s) for s in out]


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--check":
        count = int(argv[1])
        every = sorted(f"tests/{p.name}" for p in TESTS.glob("test_*.py"))
        dealt = sorted(f for s in shards(count) for f in s)
        if dealt != every:
            print("shards do not cover every test file exactly once", file=sys.stderr)
            return 1
        print(f"{len(every)} test files in {count} shards")
        return 0
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    index, count = int(argv[0]), int(argv[1])
    print(" ".join(shards(count)[index]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
