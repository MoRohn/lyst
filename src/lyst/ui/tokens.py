# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's design tokens: the one place type, spacing, shape, motion and colour are decided.

Colours are read from ``.streamlit/config.toml`` (the Streamlit theme Lyst ships), so Streamlit's own widgets,
Lyst's stylesheet and every chart use the same values; nothing else in the UI hard-codes a colour. Everything
else (type scale, spacing, radius, motion, density) lives here and reaches the page as CSS custom properties
(``--lyst-*``) written by :func:`css`, and reaches charts through :func:`plotly_layout`.

Semantic names, not raw values: a page asks for ``--lyst-text-muted`` or ``--lyst-space-3``, never ``#5b6577``
or ``12px``, so a theme change is one edit.
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path
from typing import Any

CONFIG = Path(__file__).resolve().parents[3] / ".streamlit" / "config.toml"

# Fallback when the package runs without the repository's .streamlit folder (an installed wheel).
_FALLBACK = {
    "light": {"primaryColor": "#0B6B64", "linkColor": "#0B6B64", "backgroundColor": "#ffffff",
              "secondaryBackgroundColor": "#f5f6f8", "textColor": "#16181d", "borderColor": "#e3e5ea",
              "chartCategoricalColors": ["#0B6B64", "#5b6577", "#d9822b", "#8a63d2", "#d64550", "#12A79B"]},
    "dark": {"primaryColor": "#0E7C73", "linkColor": "#5CC9B6", "backgroundColor": "#0f1115",
             "secondaryBackgroundColor": "#171a20", "textColor": "#e6e8ec", "borderColor": "#262a33",
             "chartCategoricalColors": ["#2BB5A6", "#8b95a7", "#f0a35a", "#a98ae6", "#ec6b74", "#7FD4C2"]},
}

# Status colours carry meaning (good, caution, bad, in progress); both themes meet 4.5:1 on their background.
_STATUS = {
    "light": {"good": "#1a7f37", "warn": "#9a6700", "bad": "#c62828", "info": "#0b5cad", "neutral": "#5b6577"},
    "dark": {"good": "#4ac26b", "warn": "#d4a72c", "bad": "#f47067", "info": "#6cb6ff", "neutral": "#9aa4b5"},
}

# Type: one family (the theme's), a short modular scale (rem, 15px base), and weights that rank without shouting.
TYPE = {
    "size-xs": ".72rem",   # section labels, chart axes
    "size-sm": ".84rem",   # metadata, captions, helper text
    "size-md": "1rem",     # body
    "size-lg": "1.2rem",   # answer headlines, dialog titles
    "size-xl": "1.65rem",  # the page title (one per page)
    "weight-regular": "400", "weight-medium": "560", "weight-strong": "650",
    "leading-tight": "1.25", "leading-body": "1.55",
    "tracking-label": ".06em", "tracking-title": "-0.01em",
}

# Spacing: a 4px grid. Pages use steps, never raw pixels.
SPACE = {"0": "0", "1": ".25rem", "2": ".5rem", "3": ".75rem", "4": "1rem", "5": "1.5rem", "6": "2rem", "7": "3rem"}

# Layout: the reading column, the wide analytical canvas, the sidebar.
LAYOUT = {"content-max": "1120px", "canvas-max": "1480px", "dialog-max": "760px", "gutter-mobile": "1rem",
          "touch-target": "2.25rem"}

RADIUS = {"sm": "4px", "md": "6px", "lg": "10px", "pill": "999px"}

# Motion: short, and none when the person asks for reduced motion (see css()).
MOTION = {"fast": "120ms", "base": "180ms", "ease": "cubic-bezier(.2,.0,.0,1)"}

ICON = {"sm": "1rem", "md": "1.25rem", "lg": "2rem"}


@cache
def theme(mode: str = "light") -> dict[str, Any]:
    """The Streamlit theme colours for ``mode`` ('light' or 'dark'), plus Lyst's semantic status colours."""
    mode = "dark" if mode == "dark" else "light"
    base = dict(_FALLBACK[mode])
    try:
        cfg = tomllib.loads(CONFIG.read_text())
        base.update(cfg.get("theme", {}).get(mode, {}))
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return base | {"status": _STATUS[mode]}


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    """hex_a blended toward hex_b by t (0..1): muted text and hairlines derived from the theme, not invented."""
    a = [int(hex_a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(hex_b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b, strict=True))


def colors(mode: str = "light") -> dict[str, str]:
    """Semantic colour tokens for ``mode``."""
    t = theme(mode)
    s = t["status"]
    return {
        "accent": t["primaryColor"], "link": t["linkColor"],
        "bg": t["backgroundColor"], "surface": t["secondaryBackgroundColor"],
        "text": t["textColor"],
        # two thirds of the way from background to text: 4.5:1 or better on both themes (checked in tests)
        "text-muted": _mix(t["backgroundColor"], t["textColor"], .66),
        "border": t["borderColor"], "grid": _mix(t["backgroundColor"], t["borderColor"], .75),
        "focus": t["linkColor"],
        "good": s["good"], "warn": s["warn"], "bad": s["bad"], "info": s["info"], "neutral": s["neutral"],
    }


def series(mode: str = "light") -> list[str]:
    """Categorical chart colours, in order: the first is always the brand accent."""
    return list(theme(mode)["chartCategoricalColors"])


def css(mode: str = "light") -> str:
    """The token block as CSS custom properties for the active theme."""
    props = {f"--lyst-{k}": v for k, v in colors(mode).items()}
    props |= {f"--lyst-{k}": v for k, v in TYPE.items()}
    props |= {f"--lyst-space-{k}": v for k, v in SPACE.items()}
    props |= {f"--lyst-{k}": v for k, v in LAYOUT.items()}
    props |= {f"--lyst-radius-{k}": v for k, v in RADIUS.items()}
    props |= {f"--lyst-motion-{k}": v for k, v in MOTION.items()}
    props |= {f"--lyst-icon-{k}": v for k, v in ICON.items()}
    body = "; ".join(f"{k}: {v}" for k, v in props.items())
    return (f":root {{{body};}}\n"
            "@media (prefers-reduced-motion: reduce) {:root {--lyst-motion-fast: 0ms; --lyst-motion-base: 0ms;}"
            " *, *::before, *::after {animation-duration: 0ms !important; transition-duration: 0ms !important;}}")


def plotly_layout(mode: str = "light", *, fmt: str = "number") -> dict[str, Any]:
    """A Plotly layout that matches the theme: quiet grid, muted axes, tabular numbers, the accent first."""
    c = colors(mode)
    axis = {"gridcolor": c["grid"], "zerolinecolor": c["border"], "linecolor": c["border"], "tickcolor": c["border"],
            "tickfont": {"color": c["text-muted"], "size": 11}, "title": {"font": {"color": c["text-muted"]}},
            "automargin": True}
    y = axis | {"percent": {"tickformat": ".0%"}, "currency": {"tickprefix": "$", "tickformat": "~s"}}.get(fmt, {})
    return {
        "colorway": series(mode),
        "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"color": c["text"], "size": 12},
        "xaxis": axis | {"showgrid": False}, "yaxis": y,
        "hoverlabel": {"bgcolor": c["bg"], "bordercolor": c["border"], "font": {"color": c["text"]}},
        "legend": {"orientation": "h", "y": 1.08, "x": 0, "font": {"color": c["text-muted"]}},
        "margin": {"l": 8, "r": 8, "t": 8, "b": 8},
    }


def contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two hex colours."""
    def lum(h: str) -> float:
        ch = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)
