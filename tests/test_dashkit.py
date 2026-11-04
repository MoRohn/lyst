# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's dashboard kit: theme, icons, components' rules, figure conventions, filter state, formats, findings,
and the vendored copy a generated app ships with (no dependency on any other repository)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lyst.analytics import fmt as F
from lyst.analytics import insights as I
from lyst.dashkit import components as C
from lyst.dashkit import figures as Fg
from lyst.dashkit import state as St
from lyst.dashkit import vendor
from lyst.dashkit.theme import Theme, contrast


def test_formats_round_half_up_and_handle_missing():
    assert F.money(1_234_567) == "$1.23M" and F.money(812.4) == "$812" and F.money(-1200) == "-$1.2k"
    assert F.compact(48_213) == "48.2k" and F.compact(2_000_000) == "2M" and F.compact(999_950) == "1M"
    assert F.kcount(489_520) == "490k" and F.kcount(30) == "<0.1k" and F.kcount(0) == "0"
    assert F.pct(0.1234) == "12.3%" and F.signed_pct(-0.042) == "-4.2%" and F.pts(0.0123) == "+1.2 pts"
    assert F.num(12345.6) == "12,346" and F.round_half_up(0.125, 2) == 0.13 and F.round_half_up(-0.0004, 1) == 0.0
    assert F.money(None) == "—" and F.change(5, 0) is None and F.change(110, 100) == pytest.approx(0.1)


def test_findings_are_computed_and_ranked():
    f = I.change("Revenue", 120.0, 100.0, F.money, period="in Q3 vs Q2")
    assert f.tone == "good" and "up $20" in f.text and f.evidence["pct_change"] == pytest.approx(0.2)
    bad = I.change("Churn", 0.12, 0.10, F.pct, higher_is_better=False)
    assert bad.tone == "bad"
    none = I.change("Revenue", 5.0, 0.0)
    assert none.tone == "neutral" and "No prior period" in none.text
    ranked = I.top([f, None, bad, I.Finding("x", "small", "s", magnitude=0.01)], 2)
    assert [x.kind for x in ranked] == ["change", "change"]
    tie = I.top([I.Finding("a", "a", "a", tone="good", magnitude=0.5), I.Finding("b", "b", "b", tone="bad", magnitude=0.5)])
    assert tie[0].kind == "b"  # bad news first among equals
    neg = I.negative_members(pd.DataFrame({"region": ["A", "B", "C"], "profit": [10.0, -5.0, -2.0]}), "region", "profit", "Profit")
    assert neg.tone == "bad" and "B, C" in neg.text and neg.evidence["total"] == -7.0


def test_theme_tokens_css_and_checks():
    t = Theme.load(None)
    light, dark = t.palette("light"), t.palette("dark")
    assert light.accent == "#1f5eff" and dark.scheme == "dark" and light.cat(7) == light.categorical[2]
    css = t.css()
    assert '[data-mantine-color-scheme="light"]' in css and "--lk-accent:#1f5eff" in css and "--lk-cat-0" in css
    m = t.mantine()
    assert m["primaryColor"] == "brand" and len(m["colors"]["brand"]) == 10 and "#1f5eff" in m["colors"]["brand"]
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0, rel=1e-3)
    checks = t.check()
    assert set(checks) == {"light", "dark"}
    assert not [c for s in checks.values() for c in s if c["level"] == "FAIL"], checks
    purple = Theme(name="p", schemes={s: {**t.schemes[s], "accent": "#7c3aed"} for s in ("light", "dark")})
    assert any(c["check"] == "no purples" and c["level"] == "FAIL" for c in purple.check()["light"])
    assert t.template("dark").layout.colorway[0] == dark.categorical[0]


def test_components_enforce_the_anatomy():
    with pytest.raises(ValueError, match="needs an icon"):
        C.card("Title", "body", icon="")
    card = C.card("Revenue by region", C.graph("g1", alt="Revenue by region"), icon="chart-bar", subtitle="Click a bar")
    assert card.className.startswith("lk-card")
    kpis = [C.Kpi("Revenue", 120.0, 100.0, fmt=F.money, caption="Q3 vs Q2"), C.Kpi("Margin", 0.32, 0.36, fmt=F.pct, delta="pts",
                                                                                 higher_is_better=True, caption="Q3 vs Q2")]
    row = C.kpi_row(kpis, icons=["circle-dollar-sign", "percent"])
    assert len(row.children) == 2
    with pytest.raises(ValueError, match="one icon per KPI"):
        C.kpi_row(kpis, icons=["percent"])
    with pytest.raises(ValueError, match="comparison"):
        C.kpi_row([kpis[0], C.Kpi("Orders", 5.0)], icons=["a", "b"])
    pill = C.delta_pill(-0.04, higher_is_better=False)
    assert "lk-good" in pill.className  # down is good when lower is better
    assert "lk-flat" in C.delta_pill(0.0001).className and "no comparison" in str(C.delta_pill(None).children)
    assert C.money_value("$1.2M")[0].className == "lk-unit"
    fnd = C.findings([I.change("Revenue", 120.0, 100.0)])
    assert "lk-tone-good" in fnd.children[0].children[0].className
    assert "Nothing stands out" in str(C.findings([]).children[0].children)
    with pytest.raises(KeyError, match="unknown icon"):
        C.card("x", icon="not-an-icon")


def test_figures_follow_the_click_and_dim_conventions():
    p = Theme.load(None).palette("light")
    df = pd.DataFrame({"region": ["EMEA", "APAC", "NA"], "value": [3.0, 1.0, 2.0]})
    fig = Fg.bars(df, "region", "value", p, selected=["EMEA"], fmt=F.money)
    bar = fig.data[0]
    assert list(bar.y) == ["APAC", "NA", "EMEA"] and bar.customdata[-1][0] == "EMEA"
    cols = list(bar.marker.color)
    assert cols[-1] == p.accent and cols[0].startswith("rgba(")  # the others dim, the chart never filters itself
    assert St.picked({"points": [{"customdata": ["EMEA"]}]}) == "EMEA"
    idx = pd.date_range("2025-01-01", periods=12, freq="MS")
    y = pd.Series(np.linspace(100, 160, 12), index=idx)
    fc = pd.DataFrame({"period": pd.date_range("2026-01-01", periods=3, freq="MS"), "yhat": [165, 170, 175], "lo": [150, 150, 150],
                       "hi": [180, 190, 200]})
    an = pd.DataFrame({"period": [idx[5]], "value": [y.iloc[5]], "is_anomaly": [True], "direction": ["drop"]})
    tr = Fg.trend(y, p, forecast=fc, anomalies=an, name="Revenue")
    names = [t.name for t in tr.data]
    assert names == ["Revenue", "Forecast interval", "Forecast", "Anomaly"] and tr.data[-1].marker.color[0] == p.bad
    spark = Fg.sparkline([1, 2, 3], p, kind="line")
    assert spark.layout.xaxis.visible is False and len(spark.data) == 2
    hm = Fg.heatmap(pd.DataFrame([[1.0, np.nan], [2.0, 3.0]], index=["a", "b"], columns=["x", "y"]), p, fmt=F.num)
    assert hm.data[0].text[0][1] == ""  # NaN cells stay blank
    assert Fg.empty(p, "nothing").layout.annotations[0].text == "nothing"


def test_filter_state_helpers():
    spec = [St.Filter("period", "Period", default="quarter", chip=False), St.Filter("region", "Region")]
    f = St.toggle({"period": "quarter"}, "region", "EMEA")
    assert f["region"] == "EMEA" and St.toggle(f, "region", "EMEA")["region"] is None
    assert St.active(f, spec) == {"region": "EMEA"} and St.describe(f, spec) == "Region: EMEA"
    df = pd.DataFrame({"region": ["EMEA", "NA"], "v": [1, 2]})
    assert St.apply(df, f, {"region": "region"})["v"].tolist() == [1]
    assert St.apply(df, f, {"region": "region"}, exclude="region")["v"].tolist() == [1, 2]
    assert St.picked_many({"points": [{"customdata": ["A"]}, {"customdata": ["B"]}, {"customdata": ["A"]}]}) == ["A", "B"]
    assert St.xf_id("region", "bars") == {"type": "lk-xf", "dim": "region", "name": "bars"}


def test_vendored_kit_is_self_contained(tmp_path):
    import subprocess
    import sys

    files = vendor(tmp_path)
    assert "lystkit/dashkit/app.py" in files and "lystkit/analytics/insights.py" in files and "lystkit/dashkit/static/lystkit.css" in files
    src = "\n".join(p.read_text() for p in (tmp_path / "lystkit").rglob("*.py"))
    assert "from lyst." not in src and "import lyst" not in src and "ai-analytics" not in src and "stkit" not in src.replace("lystkit", "")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from lystkit.dashkit import app, components, figures, state, checks; from lystkit.analytics import fmt, insights, drivers; "
            "print(fmt.money(1500), components.card('t', icon='award').className)")
    run = subprocess.run([sys.executable, "-I", "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=120,
                         env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert run.returncode == 0, run.stderr[-1500:]
    assert run.stdout.strip() == "$1.5k lk-card"


def test_ratio_forecast_outlook_is_a_level_never_a_sum():
    """A rate (operating margin) over six forecast periods is where it ends up, not the six values added together."""
    from lyst.analytics.timeseries import Forecast

    idx = pd.date_range("2024-10-01", periods=24, freq="MS")
    hist = pd.Series([0.33 + 0.01 * ((i % 3) - 1) for i in range(24)], index=idx)
    future = pd.date_range("2026-10-01", periods=6, freq="MS")
    fc = Forecast(history=hist, table=pd.DataFrame({"period": future, "yhat": [0.32] * 6, "lo": [0.30] * 6, "hi": [0.34] * 6}),
                  method="ETS(A,Ad,A)", level=0.8, backtest_mape=0.06, backtest_points=6, season=12)
    rate = I.forecast_note(fc, "Operating margin", F.pct, stock=True)
    assert rate.title == "Operating margin heading to 32.0% by Mar 2027"
    assert "192" not in rate.text and "of operating margin over" not in rate.text and rate.evidence["forecast"] == pytest.approx(0.32)
    flow = I.forecast_note(fc, "Revenue", F.money, stock=False)  # a flow is summed over the horizon, by design
    assert "over the next 6 periods" in flow.text and flow.evidence["forecast"] == pytest.approx(0.32 * 6)
    # the generated overview page passes stock=True for percent metrics
    from lyst import studio_dash

    assert "stock=is_rate" in studio_dash.OVERVIEW_PY and 'is_rate = m["format"] == "percent"' in studio_dash.OVERVIEW_PY
