# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Column profiling edge cases and capability registration rules."""

import pandas as pd
import pytest

from lyst.analyst.registry import Capability, Registry
from lyst.analytics.profile import profile


def test_profile_survives_repeated_column_labels():
    df = pd.DataFrame([[1, "a", 2], [3, "b", 4], [5, "c", 6]], columns=["amount", "region", "amount"])
    p = profile(df)
    assert [c.name for c in p.columns] == ["amount", "region", "amount_2"]


def test_sparse_personal_data_in_a_text_column_is_reported():
    notes = [f"call back about order {i} and the delivery window, which moved twice this month" for i in range(200)]
    notes[5], notes[50], notes[150] = "dana@northwind.test", "eve@elsewhere.test", "sam@acme.test"
    p = profile(pd.DataFrame({"notes": notes}))
    col = next(c for c in p.columns if c.name == "notes")
    assert any("email" in i for i in col.issues), (col.role, col.issues)


def test_a_newer_version_cannot_take_over_or_loosen_a_core_capability():
    r = Registry()
    r.register(Capability("value", "d", "a", "p", "data.read", lambda c, a: {}))
    with pytest.raises(ValueError, match="core capability"):
        r.register(Capability("value", "d", "a", "p", "data.read", lambda c, a: {}, version=2, origin="skill"))
    with pytest.raises(ValueError, match="core capability"):
        r.register(Capability("value", "d", "a", "p", "analyst.ask", lambda c, a: {}, version=2))
    r.register(Capability("value", "d", "a", "p", "data.read", lambda c, a: {}, version=2))  # a real core upgrade
    assert r.get("value").version == 2


def test_generated_dashboard_source_escapes_free_text():
    import tomllib

    from lyst.studio_dash import _quoted

    hostile = 'Sales "Q1"\\" ) ; import os\n#'
    ns: dict = {}
    exec(f'x = "{_quoted(hostile)}"', ns)  # noqa: S102 - proves the literal cannot be closed
    assert "import os" in ns["x"] and "\n" not in ns["x"]
    assert tomllib.loads(f'name = "{_quoted(hostile)}"')["name"] == ns["x"]


@pytest.mark.parametrize("bad", ["nonsense", "Server2"])
def test_an_unknown_profile_is_the_same_error_everywhere(monkeypatch, bad):
    from lyst import profiles

    monkeypatch.setenv("LYST_PROFILE", bad)
    for fn in (profiles.name, profiles.rules, profiles.offline, lambda: profiles.apply({"LYST_PROFILE": bad})):
        with pytest.raises(ValueError, match="LYST_PROFILE"):
            fn()
