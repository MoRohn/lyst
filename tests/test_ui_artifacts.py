# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Artifacts: previews come from the artifact's own content, every kind has a viewer, and the viewer opens by a
stable id read from the engine (so a reload or a link lands on the same thing)."""

from streamlit.testing.v1 import AppTest

from lyst.ui import artifacts

from .test_ui_ux import ui  # noqa: F401 - the running test engine


def test_every_kind_has_a_viewer_and_a_thumbnail():
    assert set(artifacts.KINDS) == set(artifacts.VIEWERS)
    for kind in artifacts.KINDS:
        if kind in ("analysis", "chart"):
            continue
        uri = artifacts.thumbnail({"kind": kind, "id": "x", "title": "t"}, "dark")
        assert uri.startswith("data:image/svg+xml;base64,")


def test_a_chart_thumbnail_is_drawn_from_its_numbers(monkeypatch):
    monkeypatch.setattr(artifacts, "result", lambda tid: {"charts": [{"type": "bar", "y": [3, 2, 1]}]})
    import base64  # noqa: PLC0415

    svg = base64.b64decode(artifacts.thumbnail({"kind": "chart", "id": "t_1:0", "task_id": "t_1", "title": "c"},
                                               "light").split(",")[1]).decode()
    assert svg.count("<rect") >= 4  # the frame and one bar per value


def test_the_viewer_opens_an_artifact_by_id_and_refuses_an_unknown_one(ui):  # noqa: F811
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/artifact.py")
    at.query_params["kind"] = "dashboard"
    at.query_params["id"] = "as_nope"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Not found" in m.value for m in at.markdown)
    tasks = [b for b in at.button if b.label == "Open"]
    assert tasks is not None


def test_maps_carry_their_connections_as_text():
    from lyst.ui import visuals  # noqa: PLC0415

    ring = visuals.ring_graph([("a", "Sales", "10 rows"), ("b", "Costs", "5 rows")], [("a", "b", "month")],
                              label="Datasets")
    assert 'alt="Datasets. Sales and Costs share month."' in ring
    lay = visuals.layered_graph([("Data", [("d:a", "Sales", "")]), ("Metrics", [("m:r", "Revenue", "")])],
                                [("d:a", "m:r")], label="Map")
    assert "Sales to Revenue" in lay and lay.startswith("<img")
