# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Pins persist per person and workspace, outside any browser session."""

import pytest

from lyst.security import AccessDenied


def test_pins_are_per_person_and_survive_a_new_service(seeded):
    lyst, owner = seeded
    lyst.pins.pin(owner, "northwind", "analysis", "t_1", "Margin")
    lyst.pins.pin(owner, "northwind", "dataset", "pnl", "Monthly P&L")
    assert [p["id"] for p in lyst.pins.list(owner, "northwind")] == ["pnl", "t_1"]
    from lyst.pins import PinService  # noqa: PLC0415

    again = PinService(lyst.stores, lyst.guard)  # a reload: nothing comes from memory
    assert {p["id"] for p in again.list(owner, "northwind")} == {"pnl", "t_1"}
    other = lyst.guard.create_user("sam2@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", other.user_id, "viewer")
    assert lyst.pins.list(other, "northwind") == []  # another person's pins are their own
    lyst.pins.unpin(owner, "northwind", "analysis", "t_1")
    assert [p["id"] for p in lyst.pins.list(owner, "northwind")] == ["pnl"]
    with pytest.raises(ValueError):
        lyst.pins.pin(owner, "northwind", "nonsense", "x")


def test_a_stranger_cannot_pin_in_a_workspace(seeded):
    lyst, _ = seeded
    stranger = lyst.guard.create_user("eve@else.test", "Eve")
    with pytest.raises(AccessDenied):
        lyst.pins.pin(stranger, "northwind", "dataset", "pnl")
