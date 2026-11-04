# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""One source of colour and type for a generated dashboard: its `theme.toml`.

Every surface reads from it: the Mantine theme (`MantineProvider(theme=theme.mantine())`), the CSS variables
both colour schemes switch between (`theme.css()`, inlined by `lyst.dashkit.app.create_app`), the Plotly
templates (`theme.template("dark")`) and the semantic colours figure factories use (`theme.palette("light").good`).

    theme = Theme.load(APP_DIR)
    p = theme.palette(scheme)           # scheme: "light" | "dark"
    p.accent, p.good, p.bad, p.muted, p.ink, p.categorical, p.sequential
    theme.check()                       # contrast, no purples, semantic colours legible on cards

`theme.toml` has `[meta]` (name, font, font_url, radius) and `[light]` / `[dark]` with the same keys: bg,
surface, surface_2, border, ink, ink_muted, accent, good, bad, warn, muted, grid, categorical, sequential,
diverging. Missing keys fall back to Lyst's defaults.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import plotly.graph_objects as go

__all__ = ["FONT_STACK", "Palette", "Theme", "contrast", "hex_to_oklch", "mix", "oklch_to_hex", "palette_template", "ramp"]

FONT_STACK = "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
_KEYS = ("bg", "surface", "surface_2", "border", "ink", "ink_muted", "accent", "good", "bad", "warn", "muted", "grid")
# Lyst's defaults: one blue accent, non-violet categorical slots, light and dark.
_DEFAULT = {
    "light": dict(bg="#f5f6f8", surface="#ffffff", surface_2="#eef0f4", border="#e3e5ea", ink="#16181d", ink_muted="#5b6577",
                  accent="#1f5eff", good="#0f9d8a", bad="#d64550", warn="#d9822b", muted="#a3acbb", grid="#e9ebf0",
                  categorical=["#1f5eff", "#0f9d8a", "#d9822b", "#2f8f3f", "#d64550"],
                  sequential=["#eef3ff", "#c3d3ff", "#8fabff", "#5a82ff", "#1f5eff"],
                  diverging=["#b33a2a", "#e6a08c", "#f3f3f1", "#8fabff", "#1f5eff"]),
    "dark": dict(bg="#0f1115", surface="#171a20", surface_2="#1e222a", border="#262a33", ink="#e6e8ec", ink_muted="#8b95a7",
                 accent="#6e93ff", good="#2dc2ac", bad="#ec6b74", warn="#f0a35a", muted="#4a5363", grid="#232833",
                 categorical=["#6e93ff", "#2dc2ac", "#f0a35a", "#5bbf6a", "#ec6b74"],
                 sequential=["#172238", "#1f3566", "#2d4fa3", "#4a73e0", "#6e93ff"],
                 diverging=["#c9503b", "#e48d72", "#2a3a3d", "#8fabff", "#6e93ff"]),
}


# ------------------------------------------------------------------------------ colour maths

def _srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lin_to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _hex_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _rgb_hex(r: float, g: float, b: float) -> str:
    return "#" + "".join(f"{round(min(1, max(0, v)) * 255):02x}" for v in (r, g, b))


def hex_to_oklch(h: str) -> tuple[float, float, float]:
    """(L 0-1, C, H degrees) of a hex colour."""
    r, g, b = (_srgb_to_lin(v) for v in _hex_rgb(h))
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return L, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360


def _oklch_rgb(L: float, C: float, H: float) -> tuple[float, float, float]:
    a, b = C * math.cos(math.radians(H)), C * math.sin(math.radians(H))
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    bl = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return r, g, bl


def oklch_to_hex(L: float, C: float, H: float) -> str:
    """Hex for an OKLCH colour, reducing chroma until it fits the sRGB gamut."""
    for _ in range(40):
        rgb = _oklch_rgb(L, C, H)
        if all(-1e-4 <= v <= 1 + 1e-4 for v in rgb):
            return _rgb_hex(*(_lin_to_srgb(max(0.0, v)) for v in rgb))
        C *= 0.92
    return _rgb_hex(*(_lin_to_srgb(min(1.0, max(0.0, v))) for v in _oklch_rgb(L, 0, H)))


def ramp(base: str, n: int = 10) -> list[str]:
    """n shades of `base` from very light to very dark at its hue (Mantine's 10-shade palette shape), with
    `base` itself placed at the slot nearest its lightness."""
    L, C, H = hex_to_oklch(base)
    lights = [0.97 - i * (0.97 - 0.28) / (n - 1) for i in range(n)]
    out = [oklch_to_hex(light, C * (0.25 + 0.75 * min(1, (1 - abs(light - L) * 1.2))), H) for light in lights]
    out[min(range(n), key=lambda i: abs(lights[i] - L))] = base
    return out


def mix(a: str, b: str, t: float) -> str:
    """Linear mix in sRGB: t=0 gives a, t=1 gives b (tints and hover surfaces)."""
    ra, rb = _hex_rgb(a), _hex_rgb(b)
    return _rgb_hex(*(x + (y - x) * t for x, y in zip(ra, rb, strict=True)))


def _rgba(h: str, alpha: float) -> str:
    r, g, b = (round(v * 255) for v in _hex_rgb(h))
    return f"rgba({r},{g},{b},{alpha})"


def _luminance(h: str) -> float:
    r, g, b = (_srgb_to_lin(v) for v in _hex_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours."""
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _is_purple(h: str) -> bool:
    L, C, H = hex_to_oklch(h)
    return C > 0.04 and 280 <= H <= 330


# ------------------------------------------------------------------------------- templates

_TEMPLATES: dict = {}


def palette_template(p: Palette) -> go.layout.Template:
    """A Plotly template for one palette: transparent backgrounds (the card shows through), the UI font, quiet
    grids, the categorical colourway, themed hover labels. Cached."""
    key = (p.scheme, p.ink, p.ink_muted, p.grid, p.border, p.surface, tuple(p.categorical), tuple(p.sequential),
           tuple(p.diverging), p.font)
    if key in _TEMPLATES:
        return _TEMPLATES[key]
    axis = dict(gridcolor=p.grid, zerolinecolor=p.border, linecolor=p.border, tickcolor=p.border,
                tickfont=dict(color=p.ink_muted, size=11), title=dict(font=dict(color=p.ink_muted, size=12)),
                showline=False, ticks="", automargin=True, zeroline=False)
    t = go.layout.Template(layout=go.Layout(
        font=dict(family=p.font, color=p.ink, size=12),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        colorway=p.categorical,
        margin=dict(l=8, r=8, t=8, b=8, pad=2),
        xaxis=dict(axis, showgrid=False), yaxis=dict(axis, showgrid=True),
        hoverlabel=dict(bgcolor=p.surface, bordercolor=p.border, font=dict(color=p.ink, family=p.font, size=12)),
        legend=dict(orientation="h", x=0, xanchor="left", y=1.02, yanchor="bottom", bgcolor="rgba(0,0,0,0)",
                    font=dict(color=p.ink_muted, size=11), itemclick="toggle", title=dict(text="")),
        hovermode="closest", separators=".,",
        colorscale=dict(sequential=[[i / (len(p.sequential) - 1), c] for i, c in enumerate(p.sequential)],
                        diverging=[[i / (len(p.diverging) - 1), c] for i, c in enumerate(p.diverging)]),
        coloraxis=dict(colorbar=dict(outlinewidth=0, tickfont=dict(color=p.ink_muted, size=10), thickness=10)),
        annotationdefaults=dict(font=dict(color=p.ink_muted, size=11), showarrow=False),
    ))
    _TEMPLATES[key] = t
    return t


# ---------------------------------------------------------------------------------- theme

@dataclass(frozen=True)
class Palette:
    """The resolved colours of one scheme, for figure factories and components."""
    scheme: str
    bg: str
    surface: str
    surface_2: str
    border: str
    ink: str
    ink_muted: str
    accent: str
    good: str
    bad: str
    warn: str
    muted: str
    grid: str
    categorical: list = field(default_factory=list)
    sequential: list = field(default_factory=list)
    diverging: list = field(default_factory=list)
    font: str = FONT_STACK

    def rgba(self, colour: str, alpha: float) -> str:
        """A translucent version of a hex colour (bands, dimmed marks)."""
        return _rgba(colour, alpha)

    def cat(self, i: int) -> str:
        return self.categorical[i % len(self.categorical)]

    def colour_map(self, members) -> dict:
        """Stable member -> categorical colour, in the order given."""
        return {m: self.cat(i) for i, m in enumerate(members)}


@dataclass(frozen=True)
class Theme:
    name: str
    schemes: dict
    font: str = "Inter"
    font_url: str | None = None
    radius: str = "md"

    @classmethod
    def load(cls, app_dir: str | Path | None = None, *, path: str | Path | None = None) -> Theme:
        """Read `<app_dir>/theme.toml` (or `path`); missing keys fall back to the defaults."""
        p = Path(path) if path else (Path(app_dir) / "theme.toml" if app_dir else None)
        cfg = tomllib.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
        meta = cfg.get("meta", {})
        schemes = {}
        for s in ("light", "dark"):
            tok = dict(_DEFAULT[s])
            tok.update(cfg.get(s, {}))
            schemes[s] = tok
        return cls(name=meta.get("name", "Lyst dashboard"), schemes=schemes, font=meta.get("font", "Inter"),
                   font_url=meta.get("font_url"), radius=meta.get("radius", "md"))

    @property
    def font_stack(self) -> str:
        return f"'{self.font}', {FONT_STACK}" if self.font else FONT_STACK

    def palette(self, scheme: str | None = "light") -> Palette:
        s = scheme if scheme in ("light", "dark") else "light"
        t = self.schemes[s]
        return Palette(scheme=s, **{k: t[k] for k in _KEYS}, categorical=list(t["categorical"]),
                       sequential=list(t["sequential"]), diverging=list(t["diverging"]), font=self.font_stack)

    # -- Mantine -------------------------------------------------------------------------
    def mantine(self) -> dict:
        """The `theme` prop for `dmc.MantineProvider`: the accent as a 10-shade primary palette, the font, radius
        and quiet component defaults."""
        light, dark = self.schemes["light"]["accent"], self.schemes["dark"]["accent"]
        shades = ramp(light)
        Ld = hex_to_oklch(dark)[0]
        lights = [hex_to_oklch(c)[0] for c in shades]
        dark_i = min(range(10), key=lambda i: abs(lights[i] - Ld))
        shades[dark_i] = dark
        light_i = shades.index(light)
        d = self.schemes["dark"]
        dark_scale = [d["ink"], mix(d["ink"], d["ink_muted"], 0.5), d["ink_muted"], mix(d["ink_muted"], d["border"], 0.5),
                      d["border"], mix(d["border"], d["surface_2"], 0.5), d["surface_2"], d["bg"],
                      mix(d["bg"], "#000000", 0.3), mix(d["bg"], "#000000", 0.55)]
        return {
            "primaryColor": "brand",
            "primaryShade": {"light": light_i, "dark": dark_i},
            "colors": {"brand": shades, "dark": dark_scale},
            "fontFamily": self.font_stack,
            "headings": {"fontFamily": self.font_stack, "fontWeight": "650"},
            "defaultRadius": self.radius,
            "cursorType": "pointer",
            "autoContrast": True,
            "luminanceThreshold": 0.45,
            "components": {
                "Card": {"defaultProps": {"withBorder": True, "radius": self.radius, "padding": "md"}},
                "Paper": {"defaultProps": {"radius": self.radius}},
                "Badge": {"defaultProps": {"radius": "sm", "variant": "light"}},
                "SegmentedControl": {"defaultProps": {"radius": self.radius}},
                "Tooltip": {"defaultProps": {"withArrow": True, "openDelay": 150}},
            },
        }

    # -- CSS -----------------------------------------------------------------------------
    def css(self) -> str:
        """CSS variables for both schemes, keyed on Mantine's `data-mantine-color-scheme` attribute, so a scheme
        switch restyles every card, KPI and grid without a callback."""
        blocks = []
        for s in ("light", "dark"):
            t = self.schemes[s]
            accent_soft = mix(t["surface"], t["accent"], 0.12 if s == "light" else 0.22)
            v = {
                "--lk-bg": t["bg"], "--lk-surface": t["surface"], "--lk-surface-2": t["surface_2"],
                "--lk-border": t["border"], "--lk-ink": t["ink"], "--lk-ink-muted": t["ink_muted"],
                "--lk-accent": t["accent"], "--lk-accent-soft": accent_soft, "--lk-good": t["good"],
                "--lk-bad": t["bad"], "--lk-warn": t["warn"], "--lk-muted": t["muted"], "--lk-grid": t["grid"],
                "--lk-good-soft": mix(t["surface"], t["good"], 0.13 if s == "light" else 0.2),
                "--lk-bad-soft": mix(t["surface"], t["bad"], 0.12 if s == "light" else 0.2),
                "--lk-shadow": ("0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06)" if s == "light"
                                else "0 1px 2px rgba(0,0,0,.3)"),
                "--mantine-color-body": t["bg"], "--mantine-color-text": t["ink"],
                "--mantine-color-dimmed": t["ink_muted"], "--mantine-color-default-border": t["border"],
                "--mantine-color-default": t["surface"], "--mantine-color-default-hover": t["surface_2"],
            }
            for i, c in enumerate(t["categorical"]):
                v[f"--lk-cat-{i}"] = c
            body = ";".join(f"{k}:{val}" for k, val in v.items())
            blocks.append(f':root[data-mantine-color-scheme="{s}"]{{{body}}}')
        return "\n".join(blocks)

    # -- Plotly --------------------------------------------------------------------------
    def template(self, scheme: str | None = "light") -> go.layout.Template:
        return palette_template(self.palette(scheme))

    # -- checks --------------------------------------------------------------------------
    def check(self, *, forbid_purple: bool = True) -> dict[str, list[dict]]:
        """Per scheme: body text and muted text contrast, semantic colours legible on cards, no purples among the
        marks, categorical slots distinct from one another. {"light": [findings], "dark": [...]}."""
        out = {}
        for s in ("light", "dark"):
            t = self.schemes[s]
            found = []

            def add(level, check, detail, found=found):
                found.append({"level": level, "check": check, "detail": detail})

            c = contrast(t["ink"], t["bg"])
            add("PASS" if c >= 4.5 else "FAIL", "body text", f"ink on bg {c:.1f}:1 (4.5 needed)")
            c = contrast(t["ink_muted"], t["surface"])
            add("PASS" if c >= 4.5 else ("WARN" if c >= 3 else "FAIL"), "muted text", f"ink_muted on surface {c:.1f}:1 (4.5 for body, 3 for large)")
            for k in ("good", "bad", "accent"):
                c = contrast(t[k], t["surface"])
                add("PASS" if c >= 3 else "WARN", f"{k} on cards", f"{t[k]} on surface {c:.1f}:1 (3 for marks and deltas)")
            marks = [t["accent"], t["good"], t["bad"], t["warn"], *t["categorical"]]
            purple = [m for m in marks if _is_purple(m)]
            if forbid_purple:
                add("PASS" if not purple else "FAIL", "no purples", "none" if not purple else f"purple marks: {purple}")
            cats = t["categorical"]
            close = [(a, b) for i, a in enumerate(cats) for b in cats[i + 1:]
                     if abs(hex_to_oklch(a)[2] - hex_to_oklch(b)[2]) % 360 < 18 and abs(hex_to_oklch(a)[0] - hex_to_oklch(b)[0]) < 0.12]
            add("PASS" if not close else "WARN", "categorical distinct", "all slots distinct" if not close else f"similar: {close}")
            out[s] = found
        return out
