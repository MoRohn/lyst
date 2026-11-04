# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Adversarial questions (master prompt 4.2): a request built on a wrong premise is corrected, not answered as if true."""

from lyst.analyst.engine import _premise

from .conftest import ask


def test_premise_direction():
    assert _premise("Why did operating margin decline in Q4 2025?") == "down"
    assert _premise("Why did revenue grow so much?") == "up"
    assert _premise("Did margin rise or fall?") is None
    assert _premise("What was revenue last quarter?") is None


def test_a_wrong_premise_is_said_first(seeded):
    lyst, owner = seeded
    # operating margin rose from Q3 2025 to Q4 2025 in the demo data
    r = ask(lyst, owner, "Why did operating margin decline in Q4 2025?")["result"]
    assert r["answer"].startswith("Operating margin did not decline in Q4 2025; it rose versus Q3 2025.")
    assert any("assumed a decline" in a for a in r["supporting"]["assumptions"])
    assert r["confidence"]["level"] == "verified"  # the numbers are still verified; only the framing is corrected
    # a true premise is answered normally
    r2 = ask(lyst, owner, "Why did operating margin decline in Q3 2026?")["result"]
    assert r2["answer"].startswith("Operating margin was 32.5% in Q3 2026")
