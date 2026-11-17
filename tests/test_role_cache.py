# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workspace roles are read once and remembered; every membership change forgets them, so access follows at once."""

import re
from pathlib import Path

import pytest

from lyst.security import AccessDenied

WS = "northwind"
SRC = Path(__file__).resolve().parent.parent / "src" / "lyst"


def _counting(lyst, monkeypatch):
    seen = []
    real = lyst.stores.control.scalar

    def scalar(sql, params=None):
        if "FROM memberships" in sql:
            seen.append(params)
        return real(sql, params)
    monkeypatch.setattr(lyst.stores.control, "scalar", scalar)
    return seen


def test_a_role_is_read_once_then_remembered(seeded, monkeypatch):
    lyst, owner = seeded
    lyst.guard.forget_roles()
    seen = _counting(lyst, monkeypatch)
    for _ in range(50):
        assert lyst.guard.role(owner, WS) == "owner"
    assert len(seen) == 1


def test_removal_and_role_changes_apply_to_the_next_check(seeded):
    lyst, owner = seeded
    pat = lyst.guard.create_user("pat@northwind.test", "Pat")
    assert lyst.guard.role(pat, WS) is None  # remembered as "not a member" until a membership changes
    lyst.guard.add_member(owner, WS, pat.user_id, "editor")
    assert lyst.guard.role(pat, WS) == "editor"
    lyst.guard.require(pat, WS, "data.read")
    lyst.guard.add_member(owner, WS, pat.user_id, "viewer")
    assert lyst.guard.role(pat, WS) == "viewer"
    lyst.guard.remove_member(owner, WS, pat.user_id)
    assert lyst.guard.role(pat, WS) is None
    with pytest.raises(AccessDenied):
        lyst.guard.require(pat, WS, "data.read")


def test_a_failed_removal_still_leaves_the_cache_consistent(seeded):
    lyst, owner = seeded
    with pytest.raises(ValueError):
        lyst.guard.remove_member(owner, WS, owner.user_id)  # the last owner stays
    assert lyst.guard.role(owner, WS) == "owner"


def test_memberships_are_written_only_where_the_cache_is_forgotten():
    """Any new code that writes memberships must clear the role cache; this lists every place that does today."""
    writes = re.compile(r"(INSERT[^\"']*INTO|DELETE FROM|UPDATE)\s+memberships", re.I)
    found = sorted(str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if writes.search(p.read_text()))
    assert found == ["portability.py", "security/__init__.py"]
    for rel in found:
        assert "forget_roles()" in (SRC / rel).read_text()
