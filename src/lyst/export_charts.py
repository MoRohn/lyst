# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The answer's charts, drawn for files: as on screen, the history muted, the forecast dotted inside its shaded
interval, the latest point marked.

* ``drawing`` makes a ReportLab vector drawing, placed as-is in the PDF and turned into SVG for Markdown.
* ``excel_chart`` makes a native Excel chart over the chart's data sheet, so it stays live and editable.
* ``slide_chart`` makes a native PowerPoint chart, also editable.
"""

from __future__ import annotations

import math
from typing import Any

from .answer_view import ACCENT, BAND, BORDER, MUTED, TEXT, as_date, fmt, mix, period_label

GRID = mix("#ffffff", BORDER, 0.9)
# a line per member, in the light theme's categorical order (ui/tokens.py chartCategoricalColors): the accent first
SERIES = ["#0B6B64", "#5b6577", "#d9822b", "#8a63d2", "#d64550", "#12A79B"]


def nice_range(values: list[float], *, include_zero: bool = False, ticks: int = 5) -> tuple[float, float, float]:
    """Axis bounds and step that read well: (low, high, step) on 1, 2, 2.5 or 5 times a power of ten."""
    vals = [v for v in values if isinstance(v, int | float) and math.isfinite(v)]
    if not vals:
        return 0.0, 1.0, 0.25
    lo, hi = min(vals), max(vals)
    if include_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if hi == lo:
        pad = abs(hi) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(1, ticks - 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def _axis_fmt(v: float, kind: str, step: float) -> str:
    if kind == "percent":
        return f"{v * 100:.{0 if step * 100 >= 1 else 1}f}%"
    if kind == "currency":
        a = abs(v)
        s = "-" if v < 0 else ""
        for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if a >= div:
                return f"{s}${a / div:g}{unit}"
        return f"{s}${a:g}"
    return f"{v:,.4g}" if abs(v) < 1e4 else f"{v / 1e3:,.0f}K" if abs(v) < 1e6 else f"{v / 1e6:,.1f}M"


def _xs(labels: list[Any]) -> tuple[list[float], bool]:
    """Positions along the x axis: real dates when every label is one (so gaps read true), else evenly spaced."""
    ds = [as_date(x) for x in labels]
    if labels and all(ds):
        return [float(d.toordinal()) for d in ds], True  # type: ignore[union-attr]
    return [float(i) for i in range(len(labels))], False


def _ticks_x(labels: list[Any], pos: list[float], dated: bool, most: int = 7) -> list[tuple[float, str]]:
    if dated:
        ds = [as_date(x) for x in labels]
        months = (ds[-1].year - ds[0].year) * 12 + ds[-1].month - ds[0].month  # type: ignore[union-attr]
        every = next((m for m in (1, 3, 6, 12, 24, 60) if months / m <= most - 1), 120)
        picks = [(p, d) for p, d in zip(pos, ds, strict=True) if (d.year * 12 + d.month - 1) % every == 0]  # type: ignore[union-attr]
        if len(picks) >= 2:
            return [(p, period_label(d)) for p, d in picks]
    n = len(labels)
    if n <= most:
        return [(p, period_label(x)) for p, x in zip(pos, labels, strict=True)]
    idx = sorted({round(i * (n - 1) / (most - 1)) for i in range(most)})
    return [(pos[i], period_label(labels[i])) for i in idx]


def drawing(c: dict[str, Any], width: float = 480, height: float = 230, *, title: bool = True):
    """The chart as a ReportLab Drawing (points), in Lyst's light theme."""
    from reportlab.graphics.shapes import Circle, Drawing, Line, Polygon, PolyLine, Rect, String  # noqa: PLC0415
    from reportlab.lib import colors  # noqa: PLC0415

    col = colors.HexColor
    kind = c.get("format", "number")
    d = Drawing(width, height)
    top_pad = 22 if title else 6
    legend_h = 16 if c.get("type") == "forecast" or c.get("series") else 0
    if title:
        d.add(String(0, height - 13, str(c.get("title", "")), fontName="Helvetica-Bold", fontSize=10, fillColor=col(TEXT)))
    left, bottom, right = 46.0, 24.0, 8.0
    pw, ph = width - left - right, height - bottom - top_pad - legend_h
    if c.get("type") == "bar":
        ys = [float(v) for v in c["y"]]
        lo, hi, step = nice_range(ys, include_zero=True)
    else:
        allv = list(c.get("history_y") or []) + list(c.get("y") or []) + list(c.get("lo") or []) + list(c.get("hi") or [])
        if c.get("series"):
            allv = [v for s_ in c["series"] for v in s_["y"]]
        lo, hi, step = nice_range([v for v in allv if v is not None])

    def Y(v: float) -> float:  # noqa: N802
        return bottom + (v - lo) / (hi - lo) * ph

    # grid and value axis
    v = lo
    while v <= hi + step / 2:
        d.add(Line(left, Y(v), left + pw, Y(v), strokeColor=col(GRID), strokeWidth=0.5))
        d.add(String(left - 5, Y(v) - 3, _axis_fmt(v, kind, step), fontName="Helvetica", fontSize=7,
                     fillColor=col(MUTED), textAnchor="end"))
        v += step
    d.add(Line(left, bottom, left + pw, bottom, strokeColor=col(BORDER), strokeWidth=0.8))

    if c.get("type") == "bar":
        n = len(c["x"])
        slot = pw / max(1, n)
        bw = slot * 0.62
        for i, (x, y) in enumerate(zip(c["x"], ys, strict=True)):
            x0 = left + i * slot + (slot - bw) / 2
            d.add(Rect(x0, min(Y(0), Y(y)), bw, abs(Y(y) - Y(0)), fillColor=col(ACCENT), strokeColor=None))
            d.add(String(x0 + bw / 2, max(Y(0), Y(y)) + 3, fmt(y, kind), fontName="Helvetica", fontSize=7,
                         fillColor=col(TEXT), textAnchor="middle"))
            label = str(x) if len(str(x)) <= 14 else str(x)[:13] + "…"
            d.add(String(x0 + bw / 2, bottom - 11, label, fontName="Helvetica", fontSize=7, fillColor=col(MUTED),
                         textAnchor="middle"))
        return d

    hx, hy = list(c.get("history_x") or []), list(c.get("history_y") or [])
    labels = hx + list(c["x"])
    pos, dated = _xs(labels)
    x0, x1 = min(pos), max(pos)

    def X(p: float) -> float:  # noqa: N802
        return left + (p - x0) / ((x1 - x0) or 1) * pw

    for p, text in _ticks_x(labels, pos, dated):
        d.add(Line(X(p), bottom, X(p), bottom - 3, strokeColor=col(BORDER), strokeWidth=0.6))
        d.add(String(X(p), bottom - 12, text, fontName="Helvetica", fontSize=7, fillColor=col(MUTED), textAnchor="middle"))

    def pts(ps: list[float], vs: list[Any]) -> list[float]:
        out: list[float] = []
        for p, val in zip(ps, vs, strict=False):
            if isinstance(val, int | float):
                out += [X(p), Y(val)]
        return out

    if c.get("type") == "forecast":
        fpos = pos[len(hx):]
        if c.get("lo") and c.get("hi"):
            band = pts(fpos, c["hi"]) + pts(fpos[::-1], c["lo"][::-1])
            if len(band) >= 6:
                d.add(Polygon(band, fillColor=col(BAND), strokeColor=None, strokeWidth=0))
        hist = pts(pos[:len(hx)], hy)
        if len(hist) >= 4:
            d.add(PolyLine(hist, strokeColor=col(MUTED), strokeWidth=1.3, strokeLineJoin=1))
        fc = pts(fpos, c["y"])
        if len(fc) >= 4:
            d.add(PolyLine(fc, strokeColor=col(ACCENT), strokeWidth=1.8, strokeDashArray=[2.5, 2.5], strokeLineCap=1))
        if fc:
            d.add(Circle(fc[-2], fc[-1], 2.6, fillColor=col(ACCENT), strokeColor=colors.white, strokeWidth=1))
        # legend, as on screen: Actual, Interval, Forecast
        ly, lx = height - top_pad - 8, left
        for name, draw in (("Actual", "line"), ("Interval", "band"), ("Forecast", "dash")):
            if draw == "band":
                d.add(Rect(lx, ly - 3, 16, 7, fillColor=col(BAND), strokeColor=None))
            else:
                d.add(Line(lx, ly, lx + 16, ly, strokeColor=col(MUTED if draw == "line" else ACCENT),
                           strokeWidth=1.6, strokeDashArray=[2.5, 2.5] if draw == "dash" else None))
            d.add(String(lx + 20, ly - 3, name, fontName="Helvetica", fontSize=7.5, fillColor=col(TEXT)))
            lx += 26 + len(name) * 4.4
    elif c.get("series"):
        ly, lx = height - top_pad - 8, left
        for i, s_ in enumerate(c["series"]):
            color = col(SERIES[i % len(SERIES)])
            line = pts(pos, s_["y"])
            if len(line) >= 4:
                d.add(PolyLine(line, strokeColor=color, strokeWidth=1.6, strokeLineJoin=1))
            d.add(Line(lx, ly, lx + 12, ly, strokeColor=color, strokeWidth=1.8))
            name = str(s_["name"])
            d.add(String(lx + 15, ly - 3, name, fontName="Helvetica", fontSize=7.5, fillColor=col(TEXT)))
            lx += 22 + len(name) * 4.4
    else:
        line = pts(pos, c["y"])
        if len(line) >= 4:
            d.add(PolyLine(line, strokeColor=col(ACCENT), strokeWidth=1.8, strokeLineJoin=1))
        if line:
            d.add(Circle(line[-2], line[-1], 3, fillColor=col(ACCENT), strokeColor=colors.white, strokeWidth=1.2))
    return d


def svg(c: dict[str, Any], width: float = 720, height: float = 320) -> str:
    """The chart as an SVG document (for Markdown and anything else that shows images)."""
    from reportlab.graphics import renderSVG  # noqa: PLC0415

    out = renderSVG.drawToString(drawing(c, width, height))
    # PDF font names to web font stacks, so a browser or Markdown viewer draws the same sans-serif type
    return (out.replace("font-family: Helvetica-Bold;", "font-family: Helvetica, Arial, sans-serif; font-weight: 600;")
               .replace("font-family: Helvetica;", "font-family: Helvetica, Arial, sans-serif;"))


# ---------------------------------------------------------------------------- native charts

def native_format(kind: str, values: list[Any], *, labels: bool = False) -> str:
    """An Excel/PowerPoint number format for a chart axis (or its data labels): 32%, $9.4M, 1,234."""
    if kind == "percent":
        return "0.0%" if labels else "0%"
    big = max((abs(v) for v in values if isinstance(v, int | float)), default=0)
    if kind == "currency":
        if big >= 1e6:
            return '"$"#,##0.0,,"M"' if labels else '"$"#,##0,,"M"'
        if big >= 1e4:
            return '"$"#,##0.0,"K"' if labels else '"$"#,##0,"K"'
        return '"$"#,##0'
    if big >= 1e6:
        return '#,##0.0,,"M"'
    return "#,##0.##" if big < 1e4 else "#,##0"

def series_for_native(c: dict[str, Any]) -> dict[str, list[Any]]:
    """Columns a native chart plots for a forecast: actual, forecast (joined to the last actual so the fan starts
    there), and the interval as a transparent base plus a filled width, stacked. Blank points are None."""
    hx, hy = list(c.get("history_x") or []), list(c.get("history_y") or [])
    n = len(c["x"])
    lo = list(c.get("lo") or [None] * n)
    hi = list(c.get("hi") or [None] * n)
    last = hy[-1] if hy else None
    actual = hy + [None] * n
    forecast = [None] * len(hy) + list(c["y"])
    base: list[Any] = [None] * len(hy) + lo
    width: list[Any] = [None] * len(hy) + [(b - a) if a is not None and b is not None else None for a, b in zip(lo, hi, strict=False)]
    if hy and last is not None:
        forecast[len(hy) - 1] = last
        base[len(hy) - 1], width[len(hy) - 1] = last, 0.0
    return {"labels": hx + list(c["x"]), "actual": actual, "forecast": forecast, "base": base, "width": width}


def excel_chart(ws, c: dict[str, Any], *, first_row: int, last_row: int, cols: dict[str, int]):
    """A native Excel chart over a chart sheet's rows. ``cols`` maps actual/forecast/base/width (or value) to columns."""
    from openpyxl.chart import AreaChart, BarChart, LineChart, Reference  # noqa: PLC0415
    from openpyxl.chart.label import DataLabelList  # noqa: PLC0415
    from openpyxl.chart.legend import Legend, LegendEntry  # noqa: PLC0415
    from openpyxl.chart.shapes import GraphicalProperties  # noqa: PLC0415
    from openpyxl.drawing.line import LineProperties  # noqa: PLC0415

    kind = c.get("format", "number")
    vals = list(c.get("y") or []) + list(c.get("history_y") or [])
    axis_fmt = native_format(kind, vals)
    cats = Reference(ws, min_col=1, min_row=first_row, max_row=last_row)

    def ref(name: str) -> Reference:
        return Reference(ws, min_col=cols[name], min_row=first_row - 1, max_row=last_row)

    def finish(ch, lo: float | None = None, hi: float | None = None) -> None:
        ch.title = str(c.get("title", ""))
        ch.height, ch.width = 9.5, 22
        ch.x_axis.delete = False
        ch.y_axis.delete = False
        ch.y_axis.number_format = axis_fmt
        ch.y_axis.majorGridlines.spPr = GraphicalProperties(ln=LineProperties(solidFill=GRID.lstrip("#")))
        if lo is not None:
            ch.y_axis.scaling.min, ch.y_axis.scaling.max = lo, hi
        ch.x_axis.number_format = "mmm yyyy"

    if c.get("type") == "bar":
        ch = BarChart()
        ch.type, ch.gapWidth = "col", 60
        ch.add_data(ref("value"), titles_from_data=True)
        ch.set_categories(cats)
        s = ch.series[0]
        s.graphicalProperties.solidFill = ACCENT.lstrip("#")
        s.graphicalProperties.line.noFill = True
        s.dLbls = DataLabelList()
        s.dLbls.showVal = True
        s.dLbls.numFmt = native_format(kind, vals, labels=True)
        ch.legend = None
        finish(ch)
        return ch

    if c.get("series") and cols.get("series"):
        ch = LineChart()
        for i, col_ in enumerate(cols["series"]):
            ch.add_data(Reference(ws, min_col=col_, min_row=first_row - 1, max_row=last_row), titles_from_data=True)
            s = ch.series[i]
            s.graphicalProperties.line.solidFill = SERIES[i % len(SERIES)].lstrip("#")
            s.graphicalProperties.line.width = 19000
            s.smooth = False
            s.marker.symbol = "none"
        ch.set_categories(cats)
        ch.legend = Legend()
        ch.legend.position = "t"
        lo, hi, _ = nice_range([v for s_ in c["series"] for v in s_["y"] if v is not None])
        finish(ch, lo, hi)
        return ch

    if c.get("type") != "forecast":
        ch = LineChart()
        ch.add_data(ref("value"), titles_from_data=True)
        ch.set_categories(cats)
        s = ch.series[0]
        s.graphicalProperties.line.solidFill = ACCENT.lstrip("#")
        s.graphicalProperties.line.width = 22000
        s.smooth = False
        ch.legend = None
        lo, hi, _ = nice_range([v for v in c["y"] if v is not None])
        finish(ch, lo, hi)
        return ch

    area = AreaChart()
    area.grouping = "stacked"
    area.add_data(ref("base"), titles_from_data=True)
    area.add_data(ref("width"), titles_from_data=True)
    area.set_categories(cats)
    base, band = area.series
    base.graphicalProperties.noFill = True
    base.graphicalProperties.line.noFill = True
    band.graphicalProperties.solidFill = BAND.lstrip("#")
    band.graphicalProperties.line.noFill = True
    line = LineChart()
    line.add_data(ref("actual"), titles_from_data=True)
    line.add_data(ref("forecast"), titles_from_data=True)
    line.set_categories(cats)
    actual, fc = line.series
    actual.graphicalProperties.line.solidFill = MUTED.lstrip("#")
    actual.graphicalProperties.line.width = 19000
    fc.graphicalProperties.line.solidFill = ACCENT.lstrip("#")
    fc.graphicalProperties.line.width = 25000
    fc.graphicalProperties.line.dashStyle = "sysDot"
    for s in (actual, fc):
        s.smooth = False
        s.marker.symbol = "none"
    allv = [v for v in (c.get("history_y") or []) + list(c["y"]) + list(c.get("lo") or []) + list(c.get("hi") or []) if v is not None]
    lo, hi, _ = nice_range(allv)
    finish(area, lo, hi)
    area.legend = Legend()
    area.legend.position = "t"
    area.legend.legendEntry = [LegendEntry(idx=0, delete=True)]  # the transparent base is not a series to name
    area += line
    return area


def slide_chart(slide, c: dict[str, Any], x, y, w, h):
    """A native PowerPoint chart on ``slide``: the forecast as actual and dotted forecast lines over a shaded
    interval (a stacked area under the lines), a line, or labelled columns."""
    import copy  # noqa: PLC0415

    from lxml import etree  # noqa: PLC0415
    from pptx.chart.data import CategoryChartData  # noqa: PLC0415
    from pptx.dml.color import RGBColor  # noqa: PLC0415
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION  # noqa: PLC0415
    from pptx.enum.dml import MSO_LINE_DASH_STYLE  # noqa: PLC0415
    from pptx.oxml.ns import qn  # noqa: PLC0415
    from pptx.util import Pt  # noqa: PLC0415

    kind = c.get("format", "number")
    vals = list(c.get("y") or []) + list(c.get("history_y") or [])
    num_fmt = native_format(kind, vals)
    rgb = lambda hx: RGBColor.from_string(hx.lstrip("#").upper())  # noqa: E731

    data = CategoryChartData()
    if c.get("type") == "forecast":
        s = series_for_native(c)
        data.categories = [period_label(x_) for x_ in s["labels"]]
        data.add_series("Actual", s["actual"])
        data.add_series("Forecast", s["forecast"])
        data.add_series("Interval base", s["base"])
        data.add_series("Interval", s["width"])
        ctype = XL_CHART_TYPE.LINE
    elif c.get("type") == "bar":
        data.categories = [str(x_) for x_ in c["x"]]
        data.add_series(str(c.get("title", "Value")), c["y"])
        ctype = XL_CHART_TYPE.COLUMN_CLUSTERED
    elif c.get("series"):
        data.categories = [period_label(x_) for x_ in c["x"]]
        for s_ in c["series"]:
            data.add_series(str(s_["name"]), s_["y"])
        ctype = XL_CHART_TYPE.LINE
    else:
        data.categories = [period_label(x_) for x_ in c["x"]]
        data.add_series(str(c.get("title", "Value")), c["y"])
        ctype = XL_CHART_TYPE.LINE
    gf = slide.shapes.add_chart(ctype, x, y, w, h, data)
    chart = gf.chart
    chart.font.size = Pt(11)
    chart.font.color.rgb = rgb(MUTED)
    va = chart.value_axis
    va.tick_labels.number_format = num_fmt
    va.tick_labels.number_format_is_linked = False
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = rgb(GRID)
    va.format.line.fill.background()
    chart.category_axis.format.line.color.rgb = rgb(BORDER)
    chart.category_axis.tick_labels.font.size = Pt(10)

    if c.get("type") == "bar":
        plot = chart.plots[0]
        plot.gap_width = 60
        plot.has_data_labels = True
        plot.data_labels.number_format = native_format(kind, vals, labels=True)
        plot.data_labels.number_format_is_linked = False
        plot.series[0].format.fill.solid()
        plot.series[0].format.fill.fore_color.rgb = rgb(ACCENT)
        chart.has_legend = False
        lo, hi, _ = nice_range(list(c["y"]), include_zero=True)
        va.minimum_scale, va.maximum_scale = lo, hi
        return gf

    plot = chart.plots[0]
    for ser in plot.series:
        ser.smooth = False
        ser.marker.style = None
        ser.format.line.width = Pt(2)
    from pptx.enum.chart import XL_MARKER_STYLE  # noqa: PLC0415

    for ser in plot.series:
        ser.marker.style = XL_MARKER_STYLE.NONE
    if c.get("series"):
        for i, ser in enumerate(plot.series):
            ser.format.line.color.rgb = rgb(SERIES[i % len(SERIES)])
        chart.has_legend = True
        chart.legend.position = XL_LEGEND_POSITION.TOP
        chart.legend.include_in_layout = False
        lo, hi, _ = nice_range([v for s_ in c["series"] for v in s_["y"] if v is not None])
        va.minimum_scale, va.maximum_scale = lo, hi
        _skip_labels(chart, len(c["x"]))
        return gf
    if c.get("type") != "forecast":
        plot.series[0].format.line.color.rgb = rgb(ACCENT)
        chart.has_legend = False
        lo, hi, _ = nice_range([v for v in c["y"] if v is not None])
        va.minimum_scale, va.maximum_scale = lo, hi
        _skip_labels(chart, len(c["x"]))
        return gf

    actual, fc = plot.series[0], plot.series[1]
    actual.format.line.color.rgb = rgb(MUTED)
    actual.format.line.width = Pt(1.5)
    fc.format.line.color.rgb = rgb(ACCENT)
    fc.format.line.width = Pt(2.25)
    fc.format.line.dash_style = MSO_LINE_DASH_STYLE.ROUND_DOT
    allv = [v for v in (c.get("history_y") or []) + list(c["y"]) + list(c.get("lo") or []) + list(c.get("hi") or []) if v is not None]
    lo, hi, _ = nice_range(allv)
    va.minimum_scale, va.maximum_scale = lo, hi

    # move the two interval series into a stacked area chart that shares the line chart's axes
    line_el = plot._element
    sers = line_el.findall(qn("c:ser"))
    area_el = etree.SubElement(line_el.getparent(), qn("c:areaChart"))
    line_el.addprevious(area_el)
    etree.SubElement(area_el, qn("c:grouping")).set("val", "stacked")
    etree.SubElement(area_el, qn("c:varyColors")).set("val", "0")
    for i, ser in enumerate(sers[2:]):
        for tag in ("c:marker", "c:smooth"):
            for el in ser.findall(qn(tag)):
                ser.remove(el)
        sp = ser.find(qn("c:spPr"))
        if sp is not None:
            ser.remove(sp)
        fill = (f'<a:solidFill><a:srgbClr val="{BAND.lstrip("#").upper()}"/></a:solidFill>' if i == 1 else "<a:noFill/>")
        sp = etree.fromstring(
            '<c:spPr xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f'{fill}<a:ln><a:noFill/></a:ln></c:spPr>')
        ser.find(qn("c:tx")).addnext(sp)
        area_el.append(ser)
    for ax in line_el.findall(qn("c:axId")):
        area_el.append(copy.deepcopy(ax))
    _skip_labels(chart, len(s["labels"]))
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.TOP
    chart.legend.include_in_layout = False
    chart.legend.font.size = Pt(11)
    leg = chart.legend._element
    entry = etree.fromstring(
        '<c:legendEntry xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        '<c:idx val="0"/><c:delete val="1"/></c:legendEntry>')  # legend order: the area (base first), then lines
    leg.find(qn("c:legendPos")).addnext(entry)
    return gf


def _skip_labels(chart, n: int, most: int = 10) -> None:
    """Label every k-th period on a slide chart's category axis so month labels never collide."""
    from lxml import etree  # noqa: PLC0415
    from pptx.oxml.ns import qn  # noqa: PLC0415

    k = max(1, math.ceil(n / most))
    if k == 1:
        return
    ax = chart.category_axis._element
    anchor = ax.find(qn("c:noMultiLvlLbl"))
    for tag in ("c:tickLblSkip", "c:tickMarkSkip"):
        el = etree.Element(qn(tag))
        el.set("val", str(k))
        if anchor is not None:
            anchor.addprevious(el)
        else:
            ax.append(el)
