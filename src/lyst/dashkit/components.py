# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Page pieces with one anatomy: cards, KPI cards, findings, definitions, grids, the shell.

They return Dash components (Dash Mantine Components underneath), styled by the CSS variables
`lyst.dashkit.theme.Theme.css()` defines, so light and dark mode need no callback. Rules:

- **Every card header has an icon** (`card(..., icon=...)` raises without one); repeated decoration is identical.
- **KPI cards in a row have the same parts**: all with a delta and a sparkline, or none (`kpi_row` raises
  otherwise); a large tabular value with a muted unit; a delta pill that is green when *better*, not when up;
  a caption naming the comparison.
- **Charts and grids carry an accessible name** (`graph(..., alt=...)`, `grid(..., alt=...)`).
- **Optional parts are tested with `is not None`**: a Dash component with no children is falsy, so `if subtitle:`
  would drop an empty `html.Span(id=...)` a callback fills.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import dcc, html

from ..analytics import fmt as F
from .icons import icon

__all__ = ["Kpi", "card", "chip_row", "credit", "decomp_tree", "definitions", "delta_pill", "empty_state", "findings", "graph",
           "grid", "grid_theme", "info", "kpi_card", "kpi_row", "legend", "money_value", "nav_link", "page_header",
           "section_title", "shell", "stat"]

GRAPH_CONFIG = {"displayModeBar": False, "responsive": True, "scrollZoom": False}
_FINDING_ICONS = {
    "insights": "sparkles", "trending_up": "trending-up", "trending_down": "trending-down", "warning": "triangle-alert",
    "error": "circle-alert", "flag": "target", "bolt": "zap", "science": "flask-conical", "query_stats": "scan-search",
    "groups": "users", "savings": "piggy-bank", "account_tree": "git-branch", "timeline": "chart-line", "pie_chart": "chart-pie",
    "money_off": "banknote", "crisis_alert": "triangle-alert",
}


def _finding_icon(name: str) -> str:
    key = str(name or "").replace(":material/", "").strip(":")
    from .icons import _icons  # noqa: PLC0415

    cand = _FINDING_ICONS.get(key, key.replace("_", "-"))
    return cand if cand in _icons() else "sparkles"


# ------------------------------------------------------------------------------- primitives

def graph(id: Any, figure: Any = None, *, height: int | str = 260, alt: str, clickable: bool = False, className: str = "",
          static: bool = False, **kwargs) -> html.Div:
    """A `dcc.Graph` with the kit's config, inside a `role="figure"` wrapper whose aria-label is `alt`."""
    config = dict(GRAPH_CONFIG, staticPlot=static)
    style = {"height": height} if height is not None else {}
    style.update(kwargs.pop("style", {}))
    fig = figure if figure is not None else {"data": [], "layout": {"paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
                                                                   "xaxis": {"visible": False}, "yaxis": {"visible": False}}}
    g = dcc.Graph(id=id, figure=fig, config=config, style=style, className="lk-graph-inner", **kwargs)
    cls = f"lk-graph{' lk-clickable' if clickable else ''} {className}".strip()
    return html.Div(g, className=cls, role="figure", **{"aria-label": alt})


def info(text: str, *, size: int = 14) -> dmc.Tooltip:
    """An ⓘ with a tooltip: definitions next to the number they define."""
    return dmc.Tooltip(label=text, multiline=True, w=280, position="top",
                       children=html.Span(icon("info", size), className="lk-info", tabIndex=0, **{"aria-label": text}))


def section_title(text: str, *, sub: str | None = None) -> html.Div:
    return html.Div([html.Div(text, className="lk-section-title"), html.Div(sub, className="lk-sub") if sub is not None else None],
                    className="lk-section")


def card(title: str, *children: Any, icon: str, subtitle: Any = None, info_text: str | None = None, actions: Any = None,
         id: Any = None, footer: Any = None, className: str = "", body_class: str = "", tone: str | None = None,
         stretch: bool = True, **kwargs) -> dmc.Card:
    """A bordered card: icon tile + title (+ ⓘ, actions) in the header, an optional muted subtitle, the body, an
    optional footer. `stretch` fills the grid cell's height, so cards in a row line up."""
    if not icon:
        raise ValueError("card() needs an icon: every card header carries one")
    from .icons import icon as _icon  # noqa: PLC0415

    head = html.Div([
        html.Div([
            html.Span(_icon(icon, 16), className=f"lk-icon-tile{(' lk-tone-' + tone) if tone else ''}"),
            html.Div([html.Div([html.Span(title, className="lk-card-title"), info(info_text) if info_text else None],
                               className="lk-card-title-row"),
                      html.Div(subtitle, className="lk-sub") if subtitle is not None else None], className="lk-card-titles"),
        ], className="lk-card-head-left"),
        html.Div(actions, className="lk-card-actions") if actions is not None else None,
    ], className="lk-card-head")
    parts = [head, html.Div(list(children), className=f"lk-card-body {body_class}".strip())]
    if footer is not None:
        parts.append(html.Div(footer, className="lk-card-foot"))
    extra = {"id": id} if id is not None else {}
    if stretch:
        kwargs.setdefault("h", "100%")
    return dmc.Card(parts, className=f"lk-card {className}".strip(), withBorder=True, padding="md", **extra, **kwargs)


def stat(label: str, value: str, *, sub: str | None = None, tone: str | None = None) -> html.Div:
    """A small labelled number inside a card (not a KPI card)."""
    return html.Div([html.Div(label, className="lk-stat-label"),
                     html.Div(value, className=f"lk-stat-value{(' lk-' + tone) if tone else ''}"),
                     html.Div(sub, className="lk-sub") if sub is not None else None], className="lk-stat")


def legend(items: Sequence[tuple[str, str]], *, dashed: Sequence[str] = ()) -> html.Div:
    """A compact legend row: (label, colour) pairs; labels in `dashed` get a dashed swatch."""
    return html.Div([html.Span([html.Span(className="lk-swatch" + (" lk-swatch-dash" if lab in dashed else ""), style={"--sw": col}), lab],
                              className="lk-legend-item") for lab, col in items], className="lk-legend")


def chip_row(chips: Sequence[tuple[str, Any]], *, clear_id: Any = None, clear_label: str = "Clear filters") -> html.Div:
    """The active-filter line: removable chips, and Clear filters while any is set. Each chip is (text, id) where id
    is the remove button's id (a pattern-matching dict id)."""
    if not chips:
        return html.Div(className="lk-chip-row lk-empty")
    items = [html.Span([html.Span(t), html.Button(icon("x", 12), id=cid, className="lk-chip-x", n_clicks=0,
                                                  **{"aria-label": f"Remove filter {t}"})], className="lk-chip") for t, cid in chips]
    if clear_id is not None:
        items.append(html.Button([icon("filter-x", 14), clear_label], id=clear_id, n_clicks=0, className="lk-clear"))
    return html.Div(items, className="lk-chip-row")


# ---------------------------------------------------------------------------------- numbers

def money_value(text: str) -> list:
    """Split a formatted amount so the currency sign renders muted: "$1.2M" -> [$ muted, 1.2M]."""
    if text and text[0] in "$€£¥" and len(text) > 1:
        return [html.Span(text[0], className="lk-unit"), text[1:]]
    if text and text.startswith("-") and len(text) > 2 and text[1] in "$€£¥":
        return ["-", html.Span(text[1], className="lk-unit"), text[2:]]
    return [text]


def delta_pill(change: float | None, *, kind: str = "pct", higher_is_better: bool = True, flat: float | None = None) -> html.Span:
    """▲/▼ change pill: "pct" shows a relative change (0.084 -> +8.4%), "pts" a rate change in percentage points.
    Green when the change is good for the business, whatever its sign; grey and "unchanged" inside `flat`."""
    if change is None or (isinstance(change, float) and math.isnan(change)):
        return html.Span("no comparison", className="lk-delta lk-flat")
    tol = flat if flat is not None else 0.0005
    if abs(change) < tol:
        return html.Span([icon("minus", 12), "unchanged"], className="lk-delta lk-flat")
    up = change > 0
    good = up == higher_is_better
    text = F.signed_pct(change) if kind == "pct" else F.pts(change)
    return html.Span([icon("arrow-up-right" if up else "arrow-down-right", 12), text], className=f"lk-delta {'lk-good' if good else 'lk-bad'}")


@dataclass
class Kpi:
    """One KPI card's content. `delta` is "pct" (relative change), "pts" (difference of two rates) or "none"."""
    label: str
    value: float | None
    prior: float | None = None
    fmt: Callable[[float], str] = F.num
    delta: str = "pct"
    higher_is_better: bool = True
    spark: Sequence[float] | None = None
    spark_kind: str = "area"
    caption: str | None = None
    info: str | None = None
    empty: str = "—"

    def change(self) -> float | None:
        if self.value is None or self.prior is None:
            return None
        if self.delta == "pts":
            return self.value - self.prior
        return F.change(self.value, self.prior)


def kpi_card(k: Kpi, *, icon_name: str, spark_figure: Any = None, id: Any = None) -> dmc.Card:
    """A KPI card: icon tile + label (+ ⓘ), the value with a muted unit and the delta pill, a sparkline, and a
    caption naming the comparison."""
    value = k.empty if k.value is None or (isinstance(k.value, float) and math.isnan(k.value)) else k.fmt(k.value)
    body = [
        html.Div([html.Span(icon(icon_name, 16), className="lk-icon-tile"), html.Span(k.label, className="lk-kpi-label"),
                  info(k.info) if k.info else None], className="lk-kpi-head"),
        html.Div([html.Span(money_value(value), className="lk-kpi-value"),
                  None if k.delta == "none" else delta_pill(k.change(), kind=k.delta, higher_is_better=k.higher_is_better)],
                 className="lk-kpi-main"),
    ]
    if spark_figure is not None:
        body.append(graph({"type": "lk-spark", "kpi": k.label} if id is None else {"type": "lk-spark", "kpi": str(id)}, spark_figure,
                          height=46, alt=f"{k.label} by period", static=True, className="lk-spark"))
    body.append(html.Div(k.caption or "", className="lk-sub lk-kpi-caption"))
    extra = {"id": id} if id is not None else {}
    return dmc.Card(body, className="lk-card lk-kpi", withBorder=True, padding="md", **extra)


def kpi_row(kpis: Sequence[Kpi], *, icons: Sequence[str], sparks: Sequence[Any] | None = None, cols: dict | int | None = None,
            ids: Sequence[Any] | None = None) -> dmc.SimpleGrid:
    """KPI cards in a responsive row (1 / 2 / n columns). Every card has the same parts."""
    if len(icons) != len(kpis):
        raise ValueError("kpi_row: one icon per KPI (every card header carries one)")
    has_spark = {s is not None for s in (sparks or [None] * len(kpis))}
    has_prior = {k.prior is not None or k.caption is not None for k in kpis}
    if len(has_spark) > 1:
        raise ValueError("kpi_row: give every KPI a sparkline or none, so the cards align")
    if len(has_prior) > 1:
        raise ValueError("kpi_row: give every KPI a comparison (prior or caption) or none")
    n = len(kpis)
    cards = [kpi_card(k, icon_name=i, spark_figure=(sparks[j] if sparks else None), id=(ids[j] if ids else None))
             for j, (k, i) in enumerate(zip(kpis, icons, strict=True))]
    return dmc.SimpleGrid(cards, cols=cols or {"base": 1, "sm": 2, "lg": n}, spacing="md", verticalSpacing="md")


# ------------------------------------------------------------------------------ text blocks

def page_header(title: str, question: str | None = None, *, right: Any = None, eyebrow: str | None = None) -> html.Div:
    """The page title, the one question the page answers, and controls on the right."""
    return html.Div([
        html.Div([html.Div(eyebrow, className="lk-eyebrow") if eyebrow is not None else None,
                  html.H1(title, className="lk-page-title"),
                  html.P(question, className="lk-page-question") if question is not None else None]),
        html.Div(right, className="lk-page-right") if right is not None else None,
    ], className="lk-page-header")


def findings(items: Iterable, *, empty: str = "Nothing stands out for these filters.") -> html.Div:
    """Computed findings (`lyst.analytics.insights.Finding`): a tone tile, a bold title, the sentence."""
    rows = []
    for f in items:
        if f is None:
            continue
        rows.append(html.Div([
            html.Span(icon(_finding_icon(getattr(f, "icon", "")), 15), className=f"lk-finding-icon lk-tone-{getattr(f, 'tone', 'neutral')}"),
            html.Div([html.Div(f.title, className="lk-finding-title"), html.Div(f.text, className="lk-finding-text")]),
        ], className="lk-finding"))
    return html.Div(rows or [html.Div(empty, className="lk-sub")], className="lk-findings")


def definitions(rows: Sequence[tuple[str, str]]) -> html.Dl:
    """Metric definitions as a description list (term, definition)."""
    items = []
    for term, text in rows:
        items += [html.Dt(term), html.Dd(text)]
    return html.Dl(items, className="lk-defs")


def empty_state(message: str, *, icon_name: str = "search", hint: str | None = None) -> html.Div:
    return html.Div([html.Span(icon(icon_name, 22), className="lk-empty-icon"), html.Div(message, className="lk-empty-text"),
                     html.Div(hint, className="lk-sub") if hint is not None else None], className="lk-empty-state")


def decomp_tree(rows, *, fmt: Callable[[float], str], max_rows: int = 18, labels: dict | None = None) -> html.Div:
    """A decomposition tree as indented rows (`lyst.analytics.decomp.flatten` output): each node's member, the
    dimension it was split by, prior -> current and a change bar scaled to the largest change."""
    import pandas as pd  # noqa: PLC0415

    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if df.empty:
        return empty_state("No change to explain", icon_name="split")
    df = df.head(max_rows)
    biggest = float(df["change"].abs().max()) or 1.0
    names = labels or {}
    items = []
    for r in df.itertuples():
        up = r.change >= 0
        w = 100 * abs(r.change) / biggest
        items.append(html.Div([
            html.Div([html.Span(str(r.member), className="lk-tree-member"), html.Span(names.get(r.dim, r.dim), className="lk-tree-dim")],
                     className="lk-tree-label", style={"paddingLeft": 18 * (r.level - 1)}),
            html.Div(f"{fmt(r.prior)} → {fmt(r.current)}", className="lk-tree-values"),
            html.Div([html.Div(className=f"lk-tree-bar {'lk-up' if up else 'lk-down'}", style={"width": f"{w:.1f}%"})], className="lk-tree-track"),
            html.Div(("+" if up else "") + fmt(r.change), className=f"lk-tree-change {'lk-good-text' if up else 'lk-bad-text'}"),
        ], className=f"lk-tree-row lk-level-{r.level}"))
    return html.Div(items, className="lk-tree", role="list")


def credit(text: str) -> html.Div:
    """The in-app credit line."""
    return html.Div(text, className="lk-credit")


# ------------------------------------------------------------------------------------- grid

def grid_theme(*, compact: bool = True) -> dict:
    """AG Grid's Quartz theme bound to the kit's CSS variables: it follows the colour scheme with no callback."""
    params = ("backgroundColor: 'var(--lk-surface)', foregroundColor: 'var(--lk-ink)', "
              "headerBackgroundColor: 'var(--lk-surface-2)', headerTextColor: 'var(--lk-ink-muted)', "
              "borderColor: 'var(--lk-border)', accentColor: 'var(--lk-accent)', "
              "rowHoverColor: 'var(--lk-accent-soft)', selectedRowBackgroundColor: 'var(--lk-accent-soft)', "
              "oddRowBackgroundColor: 'var(--lk-surface)', fontFamily: 'inherit', headerFontWeight: 600, "
              f"fontSize: 13, headerFontSize: 12, spacing: {5 if compact else 7}, wrapperBorderRadius: 8, "
              "wrapperBorder: false, columnBorder: false, rowBorder: {color: 'var(--lk-grid)'}, "
              "iconSize: 14, browserColorScheme: 'inherit'")
    return {"function": f"themeQuartz.withParams({{{params}}})"}


def grid(id: Any, row_data: list[dict], column_defs: list[dict], *, alt: str, height: int | str = 360, pagination: bool = False,
         page_size: int = 15, row_id: str | None = None, selectable: bool = False, compact: bool = True,
         pinned_bottom: list[dict] | None = None, **kwargs) -> html.Div:
    """An AG Grid with the kit's theme, sortable and filterable columns, inside a labelled `role="figure"` wrapper.
    `row_id` names the column that identifies a row (stable selection and updates)."""
    opts = {"theme": grid_theme(compact=compact), "animateRows": True, "tooltipShowDelay": 200, "enableCellTextSelection": True,
            "ensureDomOrder": True}
    if pagination:
        opts.update(pagination=True, paginationPageSize=page_size, paginationPageSizeSelector=False)
    if selectable:
        opts["rowSelection"] = {"mode": "singleRow", "checkboxes": False, "enableClickSelection": True}
    if pinned_bottom:
        opts["pinnedBottomRowData"] = pinned_bottom
    opts.update(kwargs.pop("dashGridOptions", {}))
    extra = {"getRowId": f"params.data.{row_id}"} if row_id else {}
    g = dag.AgGrid(id=id, rowData=row_data, columnDefs=column_defs,
                   defaultColDef={"sortable": True, "filter": True, "resizable": True, "minWidth": 80, **kwargs.pop("defaultColDef", {})},
                   dashGridOptions=opts, columnSize=kwargs.pop("columnSize", "responsiveSizeToFit"),
                   style={"height": height, "width": "100%"}, className="lk-grid-inner", **extra, **kwargs)
    return html.Div(g, className="lk-grid", role="figure", **{"aria-label": alt})


# ------------------------------------------------------------------------------------ shell

def nav_link(label: str, href: str, icon_name: str, *, id: Any = None) -> dmc.NavLink:
    extra = {"id": id} if id is not None else {}
    return dmc.NavLink(label=label, href=href, leftSection=icon(icon_name, 17), className="lk-nav-link", active="exact", **extra)


def shell(*, title: str, subtitle: str | None, nav: Sequence[Any], header: Any, content: Any, navbar_footer: Any = None,
          aside: Any = None) -> dmc.AppShell:
    """The app frame: a left navbar (title, navigation, footer), a header row for page controls, and the page.
    The navbar collapses behind a burger on small screens."""
    navbar = dmc.AppShellNavbar([
        html.Div([html.Div(title, className="lk-app-title"),
                  html.Div(subtitle, className="lk-app-subtitle") if subtitle is not None else None], className="lk-brand"),
        html.Nav(list(nav), className="lk-nav", **{"aria-label": "Pages"}),
        html.Div(navbar_footer, className="lk-nav-foot") if navbar_footer is not None else None,
    ], className="lk-navbar", p="md")
    head = dmc.AppShellHeader(html.Div([
        dmc.Burger(id="lk-burger", size="sm", hiddenFrom="sm", opened=False, **{"aria-label": "Open navigation"}),
        html.Div(header, className="lk-header-inner"),
    ], className="lk-header"), className="lk-headerbar")
    parts = [head, navbar, dmc.AppShellMain(html.Div(content, className="lk-main"), className="lk-mainwrap")]
    if aside is not None:
        parts.append(aside)
    return dmc.AppShell(parts, header={"height": 64}, navbar={"width": 248, "breakpoint": "sm", "collapsed": {"mobile": True}},
                        layout="alt", padding=0, id="lk-shell", className="lk-shell")
