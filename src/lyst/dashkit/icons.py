# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Icons without a network call: a curated Lucide subset (ISC, see catalog/LUCIDE-LICENSE.txt) drawn as CSS masks.

Fetching icons from an icon API in every viewer's browser fails offline, in screenshots and behind a firewall.
These are bundled in `catalog/icons.json` and drawn with `mask-image`, so an icon takes the colour of its text.

    icon("trending-up")                       # 16 px, current text colour
    icon("dice-5", size=18, color="var(--lk-accent)")
    names()                                   # the available names
"""

from __future__ import annotations

import json
import urllib.parse
from functools import lru_cache
from pathlib import Path

from dash import html

__all__ = ["icon", "names", "svg_uri"]

_CATALOG = Path(__file__).resolve().parent / "catalog" / "icons.json"


@lru_cache(maxsize=1)
def _icons() -> dict:
    return json.loads(_CATALOG.read_text(encoding="utf-8"))["icons"]


def names() -> list[str]:
    return sorted(_icons())


@lru_cache(maxsize=512)
def svg_uri(name: str, stroke_width: float = 2.0) -> str:
    """A `url("data:image/svg+xml,…")` for the icon, usable as a CSS mask or background."""
    body = _icons().get(name)
    if body is None:
        raise KeyError(f"unknown icon {name!r}; see lyst.dashkit.icons.names()")
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' "
           f"stroke-width='{stroke_width}' stroke-linecap='round' stroke-linejoin='round'>{body}</svg>")
    return 'url("data:image/svg+xml,' + urllib.parse.quote(svg, safe="/:=' ") + '")'


def icon(name: str, size: int = 16, *, color: str | None = None, stroke: float = 2.0, className: str = "", **kwargs) -> html.Span:
    """An inline icon. `aria-hidden` by default: give the control around it a label."""
    uri = svg_uri(name, stroke)
    style = {"width": size, "height": size, "WebkitMaskImage": uri, "maskImage": uri}
    if color:
        style["backgroundColor"] = color
    style.update(kwargs.pop("style", {}))
    return html.Span(className=f"lk-icon {className}".strip(), style=style, **{"aria-hidden": "true"}, **kwargs)
