# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The app factory and frame: one call gives a themed, multipage, agent-readable Dash app.

    app = create_app(__name__, app_dir=APP_DIR, title="Revenue by region", description="...")
    state.register_filters(FILTERS)
    app.layout = frame(app, title=..., subtitle=..., nav=[nav_item("Overview", "/", "layout-dashboard"), ...],
                       header=[...], filters=FILTERS, credit="Data: Lyst governed metrics.")
    server = app.server

`create_app` sets up pages from `<app_dir>/pages/`, the MCP endpoint at `/_mcp` (Dash >= 4.3) so an agent can
call the app's callbacks, background callbacks on a diskcache manager, the theme (Mantine theme, CSS variables for
both schemes, the kit's CSS and clientside functions inlined so the app works offline), a `/health` endpoint and
a visible callback-error banner. `frame` builds the shell and the global stores: `lk-scheme` (figure callbacks take
`Input(SCHEME, "data")`), `lk-filters` (see `state`) and the URL sync.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import dash
import dash_mantine_components as dmc
from dash import ClientsideFunction, Dash, Input, Output, State, dcc, html, set_props

from . import state as S
from .components import nav_link, shell
from .icons import icon
from .theme import Theme

__all__ = ["KIT_CSS", "KIT_JS", "SCHEME", "create_app", "frame", "nav_group", "nav_item", "scheme_toggle", "theme_of"]

SCHEME = S.SCHEME
_STATIC = Path(__file__).resolve().parent / "static"
KIT_CSS = (_STATIC / "lystkit.css").read_text(encoding="utf-8")
KIT_JS = (_STATIC / "lystkit.js").read_text(encoding="utf-8")
log = logging.getLogger("lyst.dashkit")

_INDEX = """<!DOCTYPE html>
<html>
<head>
{%metas%}
<title>{%title%}</title>
{%favicon%}
{%css%}
<style id="lystkit-theme">@@THEME_CSS@@</style>
<style id="lystkit-css">@@KIT_CSS@@</style>
</head>
<body>
{%app_entry%}
<footer>
{%config%}
{%scripts%}
<script id="lystkit-js">@@KIT_JS@@</script>
{%renderer%}
</footer>
</body>
</html>"""

_HOOKS_DONE = False


def create_app(import_name: str, *, app_dir: str | Path, title: str, description: str | None = None, theme: Theme | None = None,
               mcp: bool = True, background: bool = True, pages: bool = True, **dash_kwargs: Any) -> Dash:
    """A Dash app with the kit's defaults (see the module docstring)."""
    global _HOOKS_DONE  # noqa: PLW0603
    from . import mcp as kit_mcp  # noqa: PLC0415

    app_dir = Path(app_dir).resolve()
    theme = theme or Theme.load(app_dir)
    manager = None
    if background:
        import diskcache  # noqa: PLC0415
        from dash import DiskcacheManager  # noqa: PLC0415

        cache_dir = app_dir / ".cache" / "callbacks"
        cache_dir.mkdir(parents=True, exist_ok=True)
        manager = DiskcacheManager(diskcache.Cache(str(cache_dir)))
    if not _HOOKS_DONE:
        dmc.pre_render_color_scheme()  # no flash of the wrong scheme on load
        _HOOKS_DONE = True
    stylesheets = list(dash_kwargs.pop("external_stylesheets", []))
    if theme.font_url:
        stylesheets.insert(0, theme.font_url)
    app = Dash(import_name, use_pages=pages, pages_folder=str(app_dir / "pages") if pages else "", assets_folder=str(app_dir / "assets"),
               suppress_callback_exceptions=True, title=title, update_title=None, description=description,
               background_callback_manager=manager, enable_mcp=mcp, health_endpoint="health", external_stylesheets=stylesheets,
               on_error=_on_error, **dash_kwargs)
    app.index_string = _INDEX.replace("@@THEME_CSS@@", theme.css()).replace("@@KIT_CSS@@", KIT_CSS).replace("@@KIT_JS@@", KIT_JS)
    app.lk_theme = theme  # pages read it through theme_of()
    if mcp:
        from dash.mcp import configure_mcp_server  # noqa: PLC0415

        configure_mcp_server(expose_callback_docstrings=True)
        kit_mcp.harden()
    _register_frame_callbacks(app)
    _register_resources_per_worker(app)
    return app


def _register_resources_per_worker(app: Dash) -> None:
    """Dash fills `registered_paths` (the component bundles it serves) only when it renders the index page. Under
    gunicorn with several workers, a worker whose first request is a bundle answers "not a registered library".
    Register them on each process's first request."""
    import threading  # noqa: PLC0415

    state = {"done": False}
    lock = threading.Lock()

    def _register():
        if state["done"]:
            return
        with lock:
            if state["done"]:
                return
            try:
                app._generate_scripts_html()  # noqa: SLF001
                app._generate_css_dist_html()  # noqa: SLF001
            except Exception as err:  # noqa: BLE001
                # not marked done: the next request retries instead of serving without bundles for the worker's life
                state["failures"] = state.get("failures", 0) + 1
                (log.error if state["failures"] >= 3 else log.warning)(
                    "could not pre-register component bundles (attempt %d): %s", state["failures"], err)
                return
            state["done"] = True

    app.server.before_request(_register)


def theme_of(app: Dash | None = None) -> Theme:
    """The theme of `app` (default: the running app)."""
    app = app or dash.get_app()
    return getattr(app, "lk_theme", None) or Theme.load(None)


def _on_error(err: Exception) -> None:
    """Callback errors become a visible banner (and a log line), not a silent stale chart."""
    log.exception("callback error: %s", err)
    try:
        set_props("lk-error", {"hide": False, "children": f"Something went wrong: {type(err).__name__}: {err}"})
    except Exception:  # noqa: BLE001, S110 - the banner is best effort
        pass


# ------------------------------------------------------------------------------------- frame

def nav_item(label: str, href: str, icon_name: str) -> dict:
    return {"label": label, "href": href, "icon": icon_name}


def nav_group(label: str) -> dict:
    return {"group": label}


def scheme_toggle() -> dmc.ColorSchemeToggle:
    """The light/dark toggle (Mantine persists the choice in localStorage)."""
    return dmc.ColorSchemeToggle(id="lk-scheme-toggle", lightIcon=icon("sun", 18), darkIcon=icon("moon", 18), variant="default", size="lg",
                                 radius="md", **{"aria-label": "Toggle colour scheme"})


def frame(app: Dash, *, title: str, subtitle: str | None, nav: Sequence[dict], header: Any = None, filters: Sequence[S.Filter] = (),
          credit: str | None = None, navbar_extra: Any = None, header_right: Any = None) -> dmc.MantineProvider:
    """The layout around `dash.page_container`: providers, stores, navbar, header, chips."""
    theme = theme_of(app)
    links = []
    for item in nav:
        if "group" in item:
            links.append(html.Div(item["group"], className="lk-nav-group"))
        else:
            links.append(nav_link(item["label"], item["href"], item["icon"]))
    foot = [navbar_extra] if navbar_extra is not None else []
    if credit:
        foot.append(html.Div(credit, className="lk-credit"))
    head = html.Div([
        html.Div(header, className="lk-header-left", style={"flex": 1, "minWidth": 0}),
        html.Div(id=S.CHIPS, className="lk-chip-row"),
        html.Div([header_right, scheme_toggle()], style={"display": "flex", "gap": 8, "alignItems": "center"}),
    ], style={"display": "flex", "alignItems": "center", "gap": 12, "width": "100%"})
    stores = [dcc.Location(id=S.URL, refresh=False), dcc.Store(id=SCHEME, data="light"), S.store(filters), *S.url_stores()]
    error = dmc.Alert(id="lk-error", hide=True, color="red", withCloseButton=True, title="Callback error", className="lk-error",
                      style={"position": "fixed", "bottom": 16, "right": 16, "zIndex": 400, "maxWidth": 420})
    body = shell(title=title, subtitle=subtitle, nav=links, header=head, content=dash.page_container, navbar_footer=foot or None)
    tree = dmc.MantineProvider([*stores, body, error], theme=theme.mantine(), defaultColorScheme="light", id="lk-provider")
    _validation_layout(app, tree)
    return tree


def _validation_layout(app: Dash, tree: Any) -> None:
    """Every page's layout next to the frame, as `app.validation_layout`, so the MCP layer finds page components."""
    pages = []
    for page in dash.page_registry.values():
        lay = page.get("layout")
        try:
            pages.append(lay() if callable(lay) else lay)
        except Exception as err:  # noqa: BLE001
            log.warning("validation layout: page %s skipped (%s)", page.get("path"), err)
    app.validation_layout = html.Div([tree, *pages])


def _register_frame_callbacks(app: Dash) -> None:
    """The frame's clientside callbacks, on the app itself, so each app gets its own in one process."""
    app.clientside_callback(ClientsideFunction("lk", "scheme"), Output(SCHEME, "data"), Input("lk-scheme-toggle", "computedColorScheme"),
                            State(SCHEME, "data"))
    app.clientside_callback(ClientsideFunction("lk", "burger"), Output("lk-shell", "navbar"), Input("lk-burger", "opened"),
                            State("lk-shell", "navbar"), prevent_initial_call=True)
