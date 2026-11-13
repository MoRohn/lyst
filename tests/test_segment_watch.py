# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A monitor can watch one segment of a metric (operating margin in EMEA): it detects on that segment's own history,
investigates the segment, and only accepts a dimension and member that exist."""

from __future__ import annotations

import pytest

WS = "northwind"


def _approve(lyst, owner, approval_id):
    admin = lyst.guard.user_by_email("adm@northwind.test") or lyst.guard.create_user("adm@northwind.test", "Adm")
    lyst.guard.add_member(owner, WS, admin.user_id, "admin")
    lyst.runtime.decide(admin, approval_id, True)


def test_a_segment_monitor_catches_and_investigates_its_own_move(seeded):
    lyst, owner = seeded
    m = lyst.monitors.create(owner, WS, metric="cogs_ratio", grain="month",
                             segment={"dimension": "region", "member": "EMEA"})
    mon = next(x for x in lyst.monitors.list(owner, WS) if x["id"] == m["monitor_id"])
    assert mon["segment"] == {"dimension": "region", "member": "EMEA"} and mon["title"] == "COGS ratio in EMEA by month"
    ap = next(a for a in lyst.runtime.approvals(owner, WS) if a["id"] == m["approval_id"])
    assert "COGS ratio in EMEA" in ap["summary"]
    _approve(lyst, owner, m["approval_id"])
    found = lyst.monitors.tick(WS)
    assert found and all(f["metric"] == "cogs_ratio" for f in found)
    assert any("COGS ratio in EMEA" in f["summary"] for f in found)
    task = lyst.runtime.get(owner, found[0]["task_id"])
    assert "cogs ratio in EMEA" in task["request"]["question"]
    # its figure is EMEA's own, not the company's (the planted jump is in EMEA only)
    emea = [f for f in found if f["kind"] == "metric_shift"]
    assert emea and emea[0]["value"] > 0.5


def test_the_whole_metric_and_a_segment_are_separate_watches(seeded):
    lyst, owner = seeded
    whole = lyst.monitors.create(owner, WS, metric="revenue")
    seg = lyst.monitors.create(owner, WS, metric="revenue", segment={"dimension": "region", "member": "APAC"})
    rows = {m["id"]: m for m in lyst.monitors.list(owner, WS)}
    assert rows[whole["monitor_id"]]["segment"] is None and rows[seg["monitor_id"]]["segment"]["member"] == "APAC"


@pytest.mark.parametrize("segment, message", [({"dimension": "colour", "member": "red"}, "not colour"),
                                              ({"dimension": "region", "member": "Atlantis"}, "not a region"),
                                              ({"dimension": "region", "member": ""}, "name the member")])
def test_only_real_segments_can_be_watched(seeded, segment, message):
    lyst, owner = seeded
    with pytest.raises(ValueError, match=message):
        lyst.monitors.create(owner, WS, metric="revenue", segment=segment)
