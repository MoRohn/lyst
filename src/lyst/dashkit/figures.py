# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Plotly figure factories that share one look and one set of conventions.

Every factory takes a `Palette` (`theme.palette(scheme)`) and returns a `go.Figure` with the scheme's template
applied, so figures match the cards in light and dark mode. Conventions:

- **Clickable marks carry the member in `customdata[0]`** (pie slices in `label`), so `state.picked(clickData)`
  reads any chart's click the same way.
- **A selection dims, it does not filter its own chart**: pass `selected=[...]` and the other members fade.
- **Semantic colours come from the palette** (`p.good`, `p.bad`, `p.accent`, `p.muted`); categorical colours
  from `p.categorical` in a stable member order (`p.colour_map`).
- **Zero-based bars and areas**; rates may zoom, and say so in the card subtitle.

Factories never import Dash, so they are unit-tested and usable in notebooks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .theme import Palette, palette_template

__all__ = ["bars", "base", "distribution", "donut", "empty", "heatmap", "lines", "scatter", "sparkline", "stacked_bars", "trend",
           "waterfall"]

_DIM = 0.25


def base(p: Palette, *, height: int | None = None, **layout) -> go.Figure:
    """An empty figure with the scheme's template and the given layout overrides."""
    fig = go.Figure(layout=go.Layout(template=palette_template(p)))
    if height:
        fig.update_layout(height=height)
    fig.update_layout(**layout)
    return fig


def _fmt_fn(fmt: Callable | None) -> Callable:
    return fmt or (lambda v: f"{v:,.0f}")


def _dimmed(colour: str, member, selected: Sequence | None, p: Palette) -> str:
    if not selected or member in selected:
        return colour
    return p.rgba(colour, _DIM)


def empty(p: Palette, message: str = "No data for these filters", *, height: int = 220) -> go.Figure:
    """A blank figure with a centred note: an explicit empty state, never an empty axis."""
    fig = base(p, height=height)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", font=dict(color=p.ink_muted, size=13))
    return fig


# ------------------------------------------------------------------------------- small multiples

def sparkline(values: Sequence[float], p: Palette, *, colour: str | None = None, kind: str = "area", height: int = 44,
              x: Sequence | None = None) -> go.Figure:
    """A KPI sparkline: no axes, zero-based area (kind="area") or a line centred on its range (kind="line", for rates)."""
    y = list(values)
    colour = colour or p.accent
    xs = list(x) if x is not None else list(range(len(y)))
    fig = base(p, height=height, margin=dict(l=0, r=4, t=4, b=0), showlegend=False, hovermode=False)
    if not y:
        return fig
    fig.add_trace(go.Scatter(x=xs, y=y, mode="lines", line=dict(color=colour, width=2, shape="spline", smoothing=0.6),
                             fill="tozeroy" if kind == "area" else None, fillcolor=p.rgba(colour, 0.12), hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=[xs[-1]], y=[y[-1]], mode="markers", hoverinfo="skip",
                             marker=dict(color=colour, size=6, line=dict(color=p.surface, width=1.5))))
    fig.update_xaxes(visible=False, fixedrange=True)
    lo, hi = (0, max(y) * 1.08 or 1) if kind == "area" else (min(y), max(y))
    if kind != "area":
        pad = (hi - lo) * 0.2 or abs(hi) * 0.05 or 1
        lo, hi = lo - pad, hi + pad
    fig.update_yaxes(visible=False, fixedrange=True, range=[lo, hi])
    return fig


# ------------------------------------------------------------------------------------- bars

def bars(df: pd.DataFrame, dim: str, value: str, p: Palette, *, orientation: str = "h", selected: Sequence | None = None,
         colour: str | None = None, colours: dict | None = None, fmt: Callable | None = None, sort: bool = True,
         height: int | None = None, highlight: str | None = None, hover: str | None = None, text: bool = True) -> go.Figure:
    """Ranked bars (horizontal by default), labelled at the end. `selected` dims the rest; `highlight` lights one
    member in the accent and leaves the others muted."""
    d = df[[dim, value]].copy()
    if sort:
        d = d.sort_values(value, ascending=orientation == "h")
    f = _fmt_fn(fmt)
    base_colour = colour or p.accent
    cols = []
    for m in d[dim]:
        c = (colours or {}).get(m, base_colour)
        if highlight is not None:
            c = p.accent if m == highlight else p.rgba(p.accent, 0.28)
        cols.append(_dimmed(c, m, selected, p))
    labels = [f(v) for v in d[value]]
    kw = dict(x=d[value], y=d[dim].astype(str)) if orientation == "h" else dict(x=d[dim].astype(str), y=d[value])
    h = height or max(140, 34 * len(d) + 30)
    gap = min(0.8, max(0.32, 1 - 26 * len(d) / max(1, h - 30))) if orientation == "h" else 0.32
    fig = base(p, height=h, showlegend=False, bargap=gap)
    fig.add_trace(go.Bar(**kw, orientation=orientation, marker=dict(color=cols, cornerradius=4),
                         customdata=np.array(d[[dim]].astype(object)), text=labels if text else None, textposition="outside",
                         cliponaxis=False, textfont=dict(color=p.ink_muted, size=11),
                         hovertemplate=hover or f"%{{{'y' if orientation == 'h' else 'x'}}}: %{{text}}<extra></extra>"))
    if orientation == "h":
        fig.update_xaxes(visible=False, rangemode="tozero")
        fig.update_yaxes(showgrid=False, tickfont=dict(color=p.ink, size=12))
        fig.update_layout(margin=dict(l=8, r=56, t=4, b=4))
    else:
        fig.update_yaxes(visible=False, rangemode="tozero")
        fig.update_xaxes(tickfont=dict(color=p.ink, size=11))
    return fig


def stacked_bars(df: pd.DataFrame, x: str, series: str, value: str, p: Palette, *, colours: dict | None = None,
                 fmt: Callable | None = None, height: int = 260, order: Sequence | None = None, selected: Sequence | None = None,
                 percent: bool = False) -> go.Figure:
    """Stacked (or 100%) columns over x, one colour per series member."""
    piv = df.pivot_table(index=x, columns=series, values=value, aggfunc="sum", fill_value=0)
    members = list(order) if order is not None else list(piv.columns)
    if percent:
        piv = piv.div(piv.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    cmap = colours or p.colour_map(members)
    f = _fmt_fn(fmt)
    fig = base(p, height=height, barmode="stack", bargap=0.35)
    for m in members:
        if m not in piv.columns:
            continue
        fig.add_trace(go.Bar(x=piv.index, y=piv[m], name=str(m), marker=dict(color=_dimmed(cmap[m], m, selected, p)),
                             customdata=np.array([[m]] * len(piv), dtype=object),
                             hovertemplate=f"{m}<br>%{{x}}: %{{y:{'.0%' if percent else ',.0f'}}}<extra></extra>" if fmt is None else None,
                             text=None if fmt is None else [f(v) for v in piv[m]], texttemplate=None))
    if percent:
        fig.update_yaxes(tickformat=".0%", range=[0, 1])
    return fig


# ------------------------------------------------------------------------------------- donut

def donut(labels: Sequence, values: Sequence[float], p: Palette, *, colours: dict | None = None, selected: Sequence | None = None,
          centre: str | None = None, centre_sub: str | None = None, fmt: Callable | None = None, height: int = 220,
          legend: bool | str = False) -> go.Figure:
    """A donut with the total (or `centre`) in the hole. Slices carry their label for clicks."""
    labels = list(labels)
    cmap = colours or p.colour_map(labels)
    f = _fmt_fn(fmt)
    total = float(np.nansum(values))
    cols = [_dimmed(cmap.get(m, p.muted), m, selected, p) for m in labels]
    fig = base(p, height=height, showlegend=bool(legend), margin=dict(l=4, r=4, t=4, b=4))
    if legend in (True, "right"):
        fig.update_layout(legend=dict(orientation="v", x=1.02, y=0.5, xanchor="left", yanchor="middle"))
    elif legend == "bottom":
        fig.update_layout(legend=dict(orientation="h", x=0.5, xanchor="center", y=-0.02, yanchor="top", entrywidth=78,
                                      entrywidthmode="pixels", font=dict(size=10.5)), margin=dict(l=4, r=4, t=4, b=44))
    if total <= 0:
        labels, values, cols = ["none"], [1], [p.grid]
    fig.add_trace(go.Pie(labels=labels, values=list(values), hole=0.7, sort=False, direction="clockwise",
                         marker=dict(colors=cols, line=dict(color=p.surface, width=2)), textinfo="none",
                         hovertemplate="%{label}: %{customdata[1]} · %{percent}<extra></extra>" if total > 0 else None,
                         hoverinfo="skip" if total <= 0 else None, customdata=[[m, f(v)] for m, v in zip(labels, values, strict=False)]))
    big = 20 if height >= 220 and legend != "bottom" else 15
    cy = 0.56 if legend == "bottom" else 0.54
    fig.add_annotation(text=f"<b>{centre if centre is not None else f(total)}</b>", x=0.5, y=cy, xref="paper", yref="paper",
                       font=dict(size=big, color=p.ink))
    if centre_sub:
        fig.add_annotation(text=centre_sub, x=0.5, y=cy - 0.15, xref="paper", yref="paper", font=dict(size=10 if big < 20 else 11, color=p.ink_muted))
    return fig


# ------------------------------------------------------------------------------------- lines

def lines(df: pd.DataFrame, x: str, series: str | None, y: str, p: Palette, *, colours: dict | None = None,
          average: pd.Series | None = None, average_label: str = "All", fmt_axis: str | None = None, height: int = 260,
          selected: Sequence | None = None, markers: bool = True, hover_fmt: str = ",.0f", order: Sequence | None = None) -> go.Figure:
    """One line per series member (or one line), an optional dashed average in the ink colour."""
    fig = base(p, height=height, hovermode="x unified")
    groups = [(None, df)] if series is None else [(m, df[df[series] == m]) for m in
                                                  (order if order is not None else sorted(df[series].dropna().unique()))]
    cmap = colours or p.colour_map([m for m, _ in groups])
    for m, g in groups:
        c = p.accent if m is None else _dimmed(cmap[m], m, selected, p)
        fig.add_trace(go.Scatter(x=g[x], y=g[y], name=str(m) if m is not None else y, mode="lines+markers" if markers else "lines",
                                 line=dict(color=c, width=2.2), marker=dict(size=5, color=c),
                                 hovertemplate=f"{m if m is not None else y}: %{{y:{hover_fmt}}}<extra></extra>"))
    if average is not None:
        fig.add_trace(go.Scatter(x=average.index, y=average.values, name=average_label, mode="lines",
                                 line=dict(color=p.ink_muted, width=1.6, dash="dash"),
                                 hovertemplate=f"{average_label}: %{{y:{hover_fmt}}}<extra></extra>"))
    if fmt_axis:
        fig.update_yaxes(tickformat=fmt_axis)
    fig.update_layout(showlegend=series is not None or average is not None)
    return fig


def trend(actual: pd.Series, p: Palette, *, prior: pd.Series | None = None, forecast: pd.DataFrame | None = None,
          anomalies: pd.DataFrame | None = None, kind: str = "line", height: int = 280, fmt_axis: str | None = None,
          hover_fmt: str = ",.0f", name: str = "Actual", prior_name: str = "Prior period") -> go.Figure:
    """Actuals (line or columns), the prior period dashed, a forecast with its interval band, and anomaly markers.
    `forecast` is `analytics.timeseries.forecast(...).table` (period, yhat, lo, hi); `anomalies` is
    `timeseries.anomalies(...)` (period, value, is_anomaly, direction spike/drop)."""
    fig = base(p, height=height, hovermode="x unified")
    if forecast is not None and "yhat" in forecast:
        forecast = forecast.set_index("period").rename(columns={"yhat": "mean", "lo": "lower", "hi": "upper"})
    if anomalies is not None and "is_anomaly" in anomalies:
        anomalies = anomalies.set_index("period").rename(columns={"is_anomaly": "anomaly"})
    if kind == "bar":
        fig.add_trace(go.Bar(x=actual.index, y=actual.values, name=name, marker=dict(color=p.accent, cornerradius=3),
                             hovertemplate=f"{name}: %{{y:{hover_fmt}}}<extra></extra>"))
    else:
        fig.add_trace(go.Scatter(x=actual.index, y=actual.values, name=name, mode="lines", line=dict(color=p.accent, width=2.4),
                                 fill="tozeroy", fillcolor=p.rgba(p.accent, 0.07), hovertemplate=f"{name}: %{{y:{hover_fmt}}}<extra></extra>"))
    if prior is not None and len(prior):
        fig.add_trace(go.Scatter(x=actual.index[:len(prior)], y=prior.values[:len(actual)], name=prior_name, mode="lines",
                                 line=dict(color=p.muted, width=1.6, dash="dot"), hovertemplate=f"{prior_name}: %{{y:{hover_fmt}}}<extra></extra>"))
    if forecast is not None and len(forecast):
        fc = forecast
        joint_x = [actual.index[-1]] + list(fc.index)
        fig.add_trace(go.Scatter(x=list(fc.index) + list(fc.index[::-1]), y=list(fc["upper"]) + list(fc["lower"][::-1]), fill="toself",
                                 fillcolor=p.rgba(p.accent, 0.13), line=dict(width=0), hoverinfo="skip", mode="lines",
                                 name="Forecast interval", showlegend=True))
        fig.add_trace(go.Scatter(x=joint_x, y=[actual.values[-1]] + list(fc["mean"]), name="Forecast", mode="lines",
                                 line=dict(color=p.accent, width=2, dash="dash"), hovertemplate=f"Forecast: %{{y:{hover_fmt}}}<extra></extra>"))
    if anomalies is not None and len(anomalies) and "anomaly" in anomalies:
        a = anomalies[anomalies["anomaly"]]
        if len(a):
            col = [p.good if d in ("high", "spike") else p.bad for d in a.get("direction", ["high"] * len(a))]
            fig.add_trace(go.Scatter(x=a.index, y=a["value"] if "value" in a else actual.reindex(a.index), mode="markers", name="Anomaly",
                                     marker=dict(size=10, color=col, line=dict(color=p.surface, width=2), symbol="circle"),
                                     hovertemplate="Anomaly: %{y:" + hover_fmt + "}<extra></extra>"))
    if fmt_axis:
        fig.update_yaxes(tickformat=fmt_axis)
    fig.update_yaxes(rangemode="tozero")
    return fig


# --------------------------------------------------------------------------------- waterfall

def waterfall(steps: pd.DataFrame, p: Palette, *, label: str = "label", value: str = "value", measure: str = "measure",
              fmt: Callable | None = None, height: int = 300, orientation: str = "v") -> go.Figure:
    """A bridge: rows with measure "absolute" (start/end) or "relative" (a step). Increases in good, decreases in
    bad, totals in the accent. Zooms to the steps when the totals dwarf them, and says so in a corner note."""
    f = _fmt_fn(fmt)
    vals = steps[value].astype(float).tolist()
    meas = steps[measure].tolist()
    fig = base(p, height=height, showlegend=False)
    kw = dict(x=steps[label], y=vals) if orientation == "v" else dict(y=steps[label], x=vals)
    fig.add_trace(go.Waterfall(**kw, orientation=orientation, measure=meas, text=[f(v) for v in vals], textposition="outside",
                               cliponaxis=False, textfont=dict(color=p.ink_muted, size=11),
                               increasing=dict(marker=dict(color=p.good)), decreasing=dict(marker=dict(color=p.bad)),
                               totals=dict(marker=dict(color=p.accent)), connector=dict(line=dict(color=p.border, width=1)),
                               customdata=np.array(steps[[label]].astype(object)),
                               hovertemplate="%{x}: %{text}<extra></extra>" if orientation == "v" else "%{y}: %{text}<extra></extra>"))
    level, levels = 0.0, []
    for v, m in zip(vals, meas, strict=True):
        level = v if m == "absolute" else level + v
        levels.append(level)
    lo, hi = min(levels), max(levels)
    span = max(abs(v) for v, m in zip(vals, meas, strict=True) if m == "relative") if "relative" in meas else hi
    if lo > 0 and span < 0.25 * lo:
        floor = max(0.0, lo - 1.5 * span)
        axis = dict(range=[floor, hi + 0.6 * span])
        (fig.update_yaxes if orientation == "v" else fig.update_xaxes)(**axis)
        fig.add_annotation(text=f"axis starts at {f(floor)}", x=1, y=1.0, xref="paper", yref="paper", xanchor="right", yanchor="bottom",
                           font=dict(size=10, color=p.ink_muted))
        fig.update_layout(margin=dict(t=22))
    return fig


# ---------------------------------------------------------------------------------- heatmaps

def heatmap(z: pd.DataFrame, p: Palette, *, fmt: Callable | None = None, height: int | None = None, scale: str = "sequential",
            zmid: float | None = None, show_text: bool = True, hover: str = "%{y} · %{x}: %{text}", xgap: int = 3, ygap: int = 3,
            zmin: float | None = None, zmax: float | None = None) -> go.Figure:
    """A matrix (index on y, columns on x) with readable cell labels. NaN cells stay blank."""
    f = _fmt_fn(fmt)
    vals = z.values.astype(float)
    text = [["" if np.isnan(v) else f(v) for v in row] for row in vals]
    colours = p.sequential if scale == "sequential" else p.diverging
    cs = [[i / (len(colours) - 1), c] for i, c in enumerate(colours)]
    lo = np.nanmin(vals) if zmin is None and np.isfinite(vals).any() else (zmin or 0)
    hi = np.nanmax(vals) if zmax is None and np.isfinite(vals).any() else (zmax or 1)
    fig = base(p, height=height or max(180, 30 * len(z) + 60))
    fig.add_trace(go.Heatmap(z=vals, x=[str(c) for c in z.columns], y=[str(i) for i in z.index], text=text,
                             texttemplate="%{text}" if show_text else None, textfont=dict(size=11), colorscale=cs, zmid=zmid, zmin=lo, zmax=hi,
                             showscale=False, xgap=xgap, ygap=ygap, hovertemplate=hover + "<extra></extra>", hoverongaps=False))
    fig.update_yaxes(autorange="reversed", showgrid=False, tickfont=dict(color=p.ink, size=11))
    fig.update_xaxes(showgrid=False, side="top", tickfont=dict(color=p.ink_muted, size=11))
    return fig


# ----------------------------------------------------------------------------- distributions

def distribution(samples: Sequence[float], p: Palette, *, target: float | None = None, fmt: Callable | None = None, bins: int = 60,
                 height: int = 300, quantiles: Sequence[float] = (0.1, 0.5, 0.9), actual: float | None = None,
                 actual_label: str = "Actual") -> go.Figure:
    """A simulated outcome: the histogram, the share at or above `target` shaded in the accent and labelled, and
    P10 / P50 / P90 rules."""
    x = np.asarray(samples, dtype=float)
    f = _fmt_fn(fmt)
    fig = base(p, height=height, showlegend=False, bargap=0.04)
    counts, edges = np.histogram(x, bins=bins)
    mids = (edges[:-1] + edges[1:]) / 2
    share = counts / max(1, counts.sum())
    above = mids >= target if target is not None else np.zeros_like(mids, dtype=bool)
    fig.add_trace(go.Bar(x=mids, y=share, width=np.diff(edges) * 0.96, marker=dict(color=[p.accent if a else p.rgba(p.accent, 0.3) for a in above]),
                         customdata=[f"{f(a)} – {f(b)}" for a, b in zip(edges[:-1], edges[1:], strict=True)],
                         hovertemplate="%{customdata}: %{y:.1%} of runs<extra></extra>"))
    ymax = share.max() if len(share) else 1
    for q in quantiles:
        v = float(np.quantile(x, q))
        fig.add_vline(x=v, line=dict(color=p.ink_muted, width=1, dash="dot"))
        fig.add_annotation(x=v, y=ymax * 1.12, text=f"P{int(q * 100)} {f(v)}", font=dict(size=10, color=p.ink_muted), yanchor="bottom")
    if target is not None:
        prob = float((x >= target).mean())
        fig.add_vline(x=target, line=dict(color=p.ink, width=2))
        fig.add_annotation(x=target, y=ymax * 1.32, text=f"<b>{prob:.0%}</b> of runs ≥ target {f(target)}", font=dict(size=12, color=p.ink),
                           xanchor="left", xshift=6, yanchor="bottom")
    if actual is not None:
        fig.add_vline(x=actual, line=dict(color=p.good, width=2, dash="dash"))
        fig.add_annotation(x=actual, y=ymax * 0.6, text=f"{actual_label} {f(actual)}", font=dict(size=11, color=p.good), xanchor="left", xshift=6)
    fig.update_yaxes(tickformat=".0%", range=[0, ymax * 1.55], showgrid=True)
    fig.update_xaxes(tickformat="~s")
    return fig


def scatter(df: pd.DataFrame, x: str, y: str, p: Palette, *, label: str | None = None, size: str | None = None,
            colour_by: str | None = None, colours: dict | None = None, selected: Sequence | None = None, x_title: str | None = None,
            y_title: str | None = None, height: int = 320, x_fmt: str | None = None, y_fmt: str | None = None,
            quadrants: tuple | None = None, hover: str | None = None) -> go.Figure:
    """A scatter that supports box/lasso selection (dragmode "select"); points carry `label` in customdata."""
    fig = base(p, height=height, dragmode="select", showlegend=colour_by is not None)
    groups = [(None, df)] if colour_by is None else list(df.groupby(colour_by, sort=False))
    cmap = colours or p.colour_map([g for g, _ in groups])
    for g, d in groups:
        c = p.accent if g is None else cmap[g]
        col = [c if m in selected else p.rgba(c, _DIM) for m in d[label]] if (selected is not None and label) else c
        sz = None
        if size:
            s = d[size].astype(float)
            sz = 6 + 22 * np.sqrt(s / max(df[size].max(), 1e-9))
        fig.add_trace(go.Scatter(x=d[x], y=d[y], mode="markers", name=str(g) if g is not None else y,
                                 marker=dict(color=col, size=sz if sz is not None else 9, line=dict(color=p.surface, width=1), opacity=0.9),
                                 customdata=np.array(d[[label]].astype(object)) if label else None, text=d[label] if label else None,
                                 hovertemplate=hover or ("%{text}<br>" if label else "") + f"{x}: %{{x}}<br>{y}: %{{y}}<extra></extra>"))
    if quadrants:
        fig.add_vline(x=quadrants[0], line=dict(color=p.border, width=1, dash="dot"))
        fig.add_hline(y=quadrants[1], line=dict(color=p.border, width=1, dash="dot"))
    fig.update_xaxes(title=x_title or x, showgrid=True, tickformat=x_fmt)
    fig.update_yaxes(title=y_title or y, tickformat=y_fmt)
    return fig
