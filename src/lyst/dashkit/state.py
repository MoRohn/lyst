# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""View state: global filters, cross-filtering from chart clicks, and a URL that restores it.

One store (`lk-filters`) holds the view: header controls, chart clicks and chips all write to it through a single
reducer callback, every page reads it, and it mirrors into the query string so a copied link reopens the same
view. Pattern-matching ids make it work across pages without per-page wiring:

- a clickable chart:   `C.graph(state.xf_id("region", "region-bars"), ...)`
- a clickable grid:    `C.grid(state.xf_grid_id("region", "region-table"), ..., row_id="region")`
- a header control:    `dmc.SegmentedControl(id=state.ctl_id("period"), ...)`
- the chips + clear:   `html.Div(id=state.CHIPS)` (filled by the reducer's chips callback)

Rules the reducer follows: clicking a member filters every chart to it; clicking it again clears it; a chart is
never filtered by its own dimension (it dims the other members instead: `apply(..., exclude=dim)` plus
`figures.*(selected=...)`); a box or lasso selection on a scatter sets a multi-member filter.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd
from dash import ALL, ClientsideFunction, Input, Output, State, callback, clientside_callback, ctx, no_update

from .components import chip_row

__all__ = ["CHIPS", "SCHEME", "STORE", "URL", "Filter", "active", "apply", "ctl_id", "describe", "picked", "picked_many",
           "register_filters", "store", "toggle", "url_stores", "xf_grid_id", "xf_id"]

STORE = "lk-filters"
CHIPS = "lk-chips"
SCHEME = "lk-scheme"
URL = "lk-url"


@dataclass(frozen=True)
class Filter:
    """A view-state key. `chip=False` for keys that are not filters to show as chips (a period picker). `multi` keys
    hold lists. `type="number"` parses URL values as numbers."""
    key: str
    label: str
    default: Any = None
    multi: bool = False
    chip: bool = True
    type: str = "str"


def xf_id(dim: str, name: str) -> dict:
    """The id of a chart whose clicks (and box selections) filter `dim`."""
    return {"type": "lk-xf", "dim": dim, "name": name}


def xf_grid_id(dim: str, name: str) -> dict:
    """The id of an AG Grid whose row clicks filter `dim` (give it `row_id=<the dim's column>`)."""
    return {"type": "lk-xfg", "dim": dim, "name": name}


def ctl_id(dim: str) -> dict:
    """The id of a control (segmented control, select) that sets `dim`."""
    return {"type": "lk-ctl", "dim": dim}


def store(filters: Sequence[Filter]):
    """The filter store with every key at its default (put it in the frame, outside pages)."""
    from dash import dcc  # noqa: PLC0415

    return dcc.Store(id=STORE, data={f.key: f.default for f in filters})


# ------------------------------------------------------------------------------- pure helpers

def picked(click_data: dict | None) -> Any:
    """The member a click landed on: customdata[0] (the kit's convention), else a pie slice's label, else the
    point's category (y for horizontal bars, else x)."""
    if not click_data or not click_data.get("points"):
        return None
    pt = click_data["points"][0]
    cd = pt.get("customdata")
    if isinstance(cd, list) and cd:
        return cd[0]
    if cd not in (None, []) and not isinstance(cd, list | dict):
        return cd
    if "label" in pt:
        return pt["label"]
    if pt.get("orientation") == "h" or isinstance(pt.get("y"), str) and not isinstance(pt.get("x"), str):
        return pt.get("y")
    return pt.get("x")


def picked_many(selected_data: dict | None) -> list:
    """The members inside a box or lasso selection (customdata[0] of each point)."""
    if not selected_data or not selected_data.get("points"):
        return []
    out = []
    for pt in selected_data["points"]:
        m = picked({"points": [pt]})
        if m is not None and m not in out:
            out.append(m)
    return out


def toggle(filters: dict, dim: str, value: Any) -> dict:
    """Set `dim` to `value`, or clear it when it already holds exactly that value."""
    nxt = dict(filters or {})
    nxt[dim] = None if nxt.get(dim) == value else value
    return nxt


def active(filters: dict | None, spec: Sequence[Filter]) -> dict:
    """The keys of `filters` that are set to something other than their default."""
    f = filters or {}
    return {s.key: f.get(s.key) for s in spec if f.get(s.key) not in (None, [], "") and f.get(s.key) != s.default}


def apply(df: pd.DataFrame, filters: dict | None, columns: dict[str, str], *, exclude: str | Iterable[str] = ()) -> pd.DataFrame:
    """Rows of `df` matching the active filters. `columns` maps filter key -> column; keys not in it (a period) are
    ignored. `exclude` skips a chart's own dimension."""
    ex = {exclude} if isinstance(exclude, str) else set(exclude)
    mask = pd.Series(True, index=df.index)
    for key, col in columns.items():
        if key in ex:
            continue
        v = (filters or {}).get(key)
        if v in (None, [], ""):
            continue
        mask &= df[col].isin(v) if isinstance(v, list | tuple | set) else (df[col] == v)
    return df[mask]


def describe(filters: dict | None, spec: Sequence[Filter]) -> str:
    """"Region: EMEA · Product: Pro" for a scope line, or "" when nothing is filtered."""
    act = active(filters, [s for s in spec if s.chip])
    labels = {s.key: s.label for s in spec}
    parts = []
    for k, v in act.items():
        val = ", ".join(map(str, v)) if isinstance(v, list | tuple) else str(v)
        parts.append(f"{labels[k]}: {val}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------- registration

_URL_SPECS: list[dict] = []


def register_filters(spec: Sequence[Filter]) -> None:
    """Register the reducer (clicks, selections, controls, chips, clear -> store), the chips callback, and the URL
    sync for this app's filter keys. Call once, at import, before the app serves. UI plumbing: hidden from agents."""
    spec = list(spec)
    keys = {s.key for s in spec}
    url_spec = {s.key: {"multi": s.multi, "default": s.default, "type": s.type} for s in spec}
    chip_labels = {s.key: s.label for s in spec if s.chip}

    @callback(
        Output(STORE, "data", allow_duplicate=True),
        Input({"type": "lk-xf", "dim": ALL, "name": ALL}, "clickData"),
        Input({"type": "lk-xf", "dim": ALL, "name": ALL}, "selectedData"),
        Input({"type": "lk-xfg", "dim": ALL, "name": ALL}, "cellClicked"),
        Input({"type": "lk-ctl", "dim": ALL}, "value"),
        Input({"type": "lk-chip-x", "dim": ALL}, "n_clicks"),
        Input({"type": "lk-clear", "i": ALL}, "n_clicks"),
        State(STORE, "data"),
        prevent_initial_call=True,
        mcp_enabled=False,
        hidden=True,
    )
    def _lk_reduce(_clicks, _selections, _cells, _controls, _chips, _clears, filters):
        trig = ctx.triggered_id
        if not isinstance(trig, dict) or not ctx.triggered:
            return no_update
        value = ctx.triggered[0]["value"]
        prop = ctx.triggered[0]["prop_id"].rsplit(".", 1)[-1]
        f = dict(filters or {})
        kind = trig.get("type")
        if kind == "lk-clear":
            if not value:
                return no_update
            return {s.key: (f.get(s.key) if not s.chip else s.default) for s in spec}
        dim = trig.get("dim")
        if dim not in keys:
            return no_update
        if kind == "lk-chip-x":
            if not value:
                return no_update
            f[dim] = next(s.default for s in spec if s.key == dim)
            return f
        if kind == "lk-ctl":
            if value is None or f.get(dim) == value:
                return no_update
            f[dim] = value
            return f
        if kind == "lk-xf" and prop == "clickData":
            member = picked(value)
            if member is None:
                return no_update  # a chart mounting, not a click
            return toggle(f, dim, member)
        if kind == "lk-xfg":
            member = (value or {}).get("rowId")
            if member is None:
                return no_update
            return toggle(f, dim, member)
        if kind == "lk-xf" and prop == "selectedData":
            members = picked_many(value)
            if value is None and not f.get(dim):
                return no_update
            f[dim] = members or None
            return f
        return no_update

    @callback(Output(CHIPS, "children"), Input(STORE, "data"), mcp_enabled=False, hidden=True)
    def _lk_chips(filters):
        act = active(filters, [s for s in spec if s.chip])
        chips = []
        for k, v in act.items():
            val = ", ".join(map(str, v)) if isinstance(v, list | tuple) else str(v)
            chips.append((f"{chip_labels[k]}: {val}", {"type": "lk-chip-x", "dim": k}))
        return chip_row(chips, clear_id={"type": "lk-clear", "i": 0}).children if chips else []

    clientside_callback(
        ClientsideFunction("lk", "urlToStore"),
        Output(STORE, "data", allow_duplicate=True),
        Output({"type": "lk-ctl", "dim": ALL}, "value"),
        Input(URL, "search"),
        State("lk-url-spec", "data"),
        State(STORE, "data"),
        State({"type": "lk-ctl", "dim": ALL}, "id"),
        prevent_initial_call="initial_duplicate",
    )
    clientside_callback(
        ClientsideFunction("lk", "storeToUrl"),
        Output("lk-url-sync", "data"),
        Input(STORE, "data"),
        Input(URL, "pathname"),
        State("lk-url-spec", "data"),
    )
    _URL_SPECS.append(url_spec)


def url_stores() -> list:
    """The stores the URL sync needs (added by `app.frame`): the key spec, and a sink for the clientside writer."""
    from dash import dcc  # noqa: PLC0415

    return [dcc.Store(id="lk-url-spec", data=_URL_SPECS[-1] if _URL_SPECS else {}), dcc.Store(id="lk-url-sync")]
