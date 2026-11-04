# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The generated app as an MCP server: hardening, an in-process client, and a probe.

Dash 4.3+ serves an MCP endpoint at `/_mcp` when `enable_mcp=True`: every callback becomes a tool (inputs
described from the layout), plus resources for the layout and pages. This module makes that endpoint testable
without a browser or a server (`Client.for_app(app)` goes through Flask's test client) and works around two
Dash 4.4 behaviours: a callback that returns a figure is rendered to a PNG with Kaleido and a missing Kaleido
failed the whole tool call; Dash's own plumbing callbacks were listed as tools; concrete dict ids arrived as JSON
strings. `LYSTKIT_MCP_IMAGES=0` skips the PNG entirely (faster checks).
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Client", "McpError", "harden", "probe"]


class McpError(RuntimeError):
    pass


def harden() -> bool:
    """Patch Dash 4.4.x's MCP layer. True when patches were applied; a no-op on versions that fixed these."""
    try:
        from dash.mcp.primitives.tools import callback_adapter_collection as cac  # noqa: PLC0415
        from dash.mcp.primitives.tools.results import result_plotly_figure as rpf  # noqa: PLC0415
    except ImportError:
        return False
    if not getattr(cac.CallbackAdapterCollection.__init__, "_lystkit", False):
        original_init = cac.CallbackAdapterCollection.__init__

        def __init__(self, app):
            for k, v in getattr(app, "callback_map", {}).items():
                fn = getattr(v.get("callback"), "__wrapped__", v.get("callback"))
                if "_pages_content" in k or getattr(fn, "__name__", "") == "cancel_call":
                    v["mcp_enabled"] = False  # Dash's own plumbing: the page router, background cancel
            original_init(self, app)

        __init__._lystkit = True  # type: ignore[attr-defined]
        cac.CallbackAdapterCollection.__init__ = __init__
    _patch_dict_ids()
    if getattr(rpf._render_image, "_lystkit", False):
        return False
    original = rpf._render_image

    def _render_image(figure):
        if os.environ.get("LYSTKIT_MCP_IMAGES", "1") == "0":
            return None
        try:
            return original(figure)
        except Exception as err:  # noqa: BLE001 - an image is optional context, never fatal
            rpf.logger.debug("MCP: figure image skipped (%s)", err)
            return None

    _render_image._lystkit = True  # type: ignore[attr-defined]
    rpf._render_image = _render_image
    return True


def _decode_id(entry):
    if isinstance(entry, list):
        return [_decode_id(e) for e in entry]
    if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].startswith("{"):
        try:
            return {**entry, "id": json.loads(entry["id"])}
        except ValueError:
            return entry
    return entry


def _patch_dict_ids() -> None:
    """Dash 4.4.1's MCP dispatch sends a callback's concrete dict ids as JSON strings; decode them."""
    from dash.mcp.primitives.tools import callback_adapter as ca  # noqa: PLC0415

    if getattr(ca._expand_output_spec, "_lystkit", False):
        return
    orig_out, orig_dep = ca._expand_output_spec, ca._expand_dep

    def _expand_output_spec(output_id, cb_info, resolved_inputs):
        res = orig_out(output_id, cb_info, resolved_inputs)
        return [_decode_id(r) for r in res] if isinstance(res, list) else _decode_id(res)

    def _expand_dep(dep, value):
        return _decode_id(orig_dep(dep, value))

    _expand_output_spec._lystkit = True  # type: ignore[attr-defined]
    ca._expand_output_spec, ca._expand_dep = _expand_output_spec, _expand_dep


@dataclass
class Client:
    """A minimal MCP client (JSON-RPC over Streamable HTTP, JSON responses)."""
    post: Callable[[dict, dict], tuple[dict | list | None, dict]]
    session: str | None = None
    _id: int = 0
    info: dict = field(default_factory=dict)

    @classmethod
    def for_app(cls, app, path: str = "/_mcp") -> Client:
        tc = app.server.test_client()

        def post(body: dict, headers: dict):
            r = tc.post(path, data=json.dumps(body), headers={"Content-Type": "application/json", **headers})
            return (r.get_json(silent=True) if r.data else None), dict(r.headers)
        return cls(post).initialize()

    @classmethod
    def http(cls, url: str, timeout: float = 60) -> Client:
        def post(body: dict, headers: dict):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",  # noqa: S310 - local app URL
                                         headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - local app URL
                raw = r.read()
                return (json.loads(raw) if raw else None), dict(r.headers)
        return cls(post).initialize()

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        self._id += 1
        headers = {"Mcp-Session-Id": self.session} if self.session else {}
        body, hdrs = self.post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}, headers)
        sid = hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id")
        if sid:
            self.session = sid
        if isinstance(body, list):
            body = next((m for m in body if m.get("id") == self._id), body[-1])
        if body is None:
            return None
        if "error" in body:
            raise McpError(body["error"].get("message", str(body["error"])))
        return body.get("result")

    def initialize(self) -> Client:
        self.info = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "lystkit", "version": "0.1"}}) or {}
        return self

    def tools(self) -> list[dict]:
        return (self._rpc("tools/list") or {}).get("tools", [])

    def resources(self) -> list[dict]:
        return (self._rpc("resources/list") or {}).get("resources", [])

    def call_raw(self, name: str, arguments: dict | None = None) -> dict:
        return self._rpc("tools/call", {"name": name, "arguments": arguments or {}}) or {}

    def call(self, name: str, **arguments: Any) -> Any:
        """Call a tool and return its structured result. Raises McpError on isError."""
        res = self.call_raw(name, arguments)
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            raise McpError(f"{name}: {text}")
        sc = res.get("structuredContent")
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        if isinstance(sc, dict) and "response" in sc:
            return sc["response"]
        return sc if sc is not None else res.get("content")


def _defaults(tool: dict) -> dict:
    """Arguments for a default call: each input's layout value, read from its description ("value: 2" lines)."""
    import ast  # noqa: PLC0415

    args = {}
    for name, prop in (tool.get("inputSchema", {}).get("properties") or {}).items():
        desc = prop.get("description") or ""
        for line in desc.splitlines():
            for key in ("value: ", "data: ", "checked: "):
                if line.startswith(key) and name not in args:
                    raw = line[len(key):].strip()
                    try:
                        args[name] = ast.literal_eval(raw)
                    except (ValueError, SyntaxError):
                        args[name] = raw
    return args


def probe(app, *, call: bool = True, skip: set[str] | None = None) -> dict:
    """The agent-readiness report: tools, typed and documented shares, resources, and (with `call`) each callback
    tool called with its layout defaults."""
    os.environ.setdefault("LYSTKIT_MCP_IMAGES", "0")
    c = Client.for_app(app)
    tools = c.tools()
    builtin = {"get_dash_component"}
    rows = []
    for t in tools:
        if t["name"] in builtin:
            continue
        props = (t.get("inputSchema") or {}).get("properties") or {}
        typed = all("type" in p or "anyOf" in p or "enum" in p for p in props.values())
        desc = t.get("description") or ""
        documented = "\n\n" in desc or not desc.split(":")[0].count(".")
        row = {"name": t["name"], "inputs": len(props), "typed": typed, "documented": documented, "called": None}
        if call and t["name"] not in (skip or set()):
            args = _defaults(t)
            required = set((t.get("inputSchema") or {}).get("required") or [])
            if required - set(args):
                row["called"] = "skipped (needs " + ", ".join(sorted(required - set(args))) + ")"
            else:
                try:
                    c.call(t["name"], **args)
                    row["called"] = "ok"
                except McpError as err:
                    row["called"] = f"ERROR {err}"
        rows.append(row)
    res = c.resources()
    n = max(1, len(rows))
    return {"server": c.info.get("serverInfo", {}), "tools": rows, "resources": [r["uri"] for r in res],
            "typed_share": sum(r["typed"] for r in rows) / n, "documented_share": sum(r["documented"] for r in rows) / n,
            "errors": [r for r in rows if isinstance(r["called"], str) and r["called"].startswith("ERROR")]}
