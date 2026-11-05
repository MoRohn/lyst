# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Connector drafts: Lyst proposes connections from an API's own description; a person tests and promotes them.

A draft is **configuration for an existing, reviewed connector** (today the REST connector), never generated code, so
nothing unreviewed ever runs. From an OpenAPI 3 document (or Swagger 2) Lyst finds the GET endpoints that return a list
of records and, for each, proposes the connection settings: the URL, where the list sits in the body, how pages link
(page number, offset, or a cursor/next link), a page size parameter, an "updated since" parameter for incremental
syncs, a cursor column, and the authentication header the API declares. Each proposal says why it chose what it chose.

The lifecycle is draft → tested → promoted (or rejected):

* **test** runs the connector against the live API with a token the person supplies for this call only (never stored
  on the draft): connection test, first and second page, field list and a few sample records with values masked. No
  dataset is written.
* **promote** needs an admin who is not the person who created the draft. It creates the real connection through the
  normal path (the token is tested again and sealed by the credential service) and records who approved it.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import urljoin

from ..security import AccessDenied, Guard, Principal
from ..store import Stores

_PAGE = ("page", "page_number", "pageNumber", "p")
_SIZE = ("per_page", "page_size", "pageSize", "limit", "size", "count", "perPage")
_OFFSET = ("offset", "start", "skip")
_CURSOR = ("cursor", "after", "next", "page_token", "pageToken", "starting_after", "continuation")
_SINCE = re.compile(r"(updated|modified|changed|since|from)[_-]?(after|since|at|from|gte)?$|^since$|^updated_since$", re.I)
_TIME_FIELD = re.compile(r"(updated|modified|changed)(_at|At|_on|_date)?$", re.I)
_LIST_KEYS = ("data", "items", "results", "records", "rows", "values", "entries", "objects")


def _resolve(ref: dict[str, Any] | None, doc: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Follow a local $ref (``#/components/schemas/X`` or ``#/definitions/X``)."""
    node = ref or {}
    while isinstance(node, dict) and "$ref" in node and depth < 10:
        path = node["$ref"]
        if not path.startswith("#/"):
            return {}
        cur: Any = doc
        for part in path[2:].split("/"):
            cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        node, depth = cur, depth + 1
    return node if isinstance(node, dict) else {}


def _list_shape(schema: dict[str, Any], doc: dict[str, Any]) -> tuple[str | None, dict[str, Any], str | None] | None:
    """(records_key, item schema, next key) when the response is a list of objects, directly or under one key."""
    s = _resolve(schema, doc)
    if s.get("type") == "array":
        return None, _resolve(s.get("items"), doc), None
    props = s.get("properties") or {}
    arrays = [(k, _resolve(v, doc)) for k, v in props.items() if _resolve(v, doc).get("type") == "array"]
    if not arrays:
        return None
    arrays.sort(key=lambda kv: (kv[0] not in _LIST_KEYS, kv[0]))
    key, arr = arrays[0]
    nxt = next((k for k in props if re.fullmatch(r"next(_?(page|cursor|link|url|token))?|next_page_token|nextPageToken|cursor", k, re.I)), None)
    if not nxt:
        for k, v in props.items():  # a nested meta/links/pagination object holding the next link
            sub = _resolve(v, doc).get("properties") or {}
            inner = next((s2 for s2 in sub if re.fullmatch(r"next(_?(page|cursor|link|url|token))?|cursor", s2, re.I)), None)
            if inner and k in ("meta", "links", "pagination", "paging", "page_info"):
                nxt = f"{k}.{inner}"
                break
    return key, _resolve(arr.get("items"), doc), nxt


def propose_from_openapi(doc: dict[str, Any], *, base_url: str | None = None) -> list[dict[str, Any]]:
    """One proposal per GET endpoint that returns a list of records."""
    if not isinstance(doc, dict) or not ("openapi" in doc or "swagger" in doc) or not isinstance(doc.get("paths"), dict):
        raise ValueError("this is not an OpenAPI 3 or Swagger 2 document (no 'openapi'/'swagger' and 'paths')")
    if base_url:
        server = base_url
    elif doc.get("servers"):
        server = str(doc["servers"][0].get("url") or "")
    elif doc.get("host"):
        server = f"https://{doc['host']}{doc.get('basePath', '')}"
    else:
        server = ""
    auth_header, auth_kind = None, None
    schemes = (doc.get("components") or {}).get("securitySchemes") or doc.get("securityDefinitions") or {}
    for s in schemes.values():
        if s.get("type") == "apiKey" and s.get("in") == "header":
            auth_header, auth_kind = s.get("name"), "api key header"
            break
        if s.get("type") == "http" and str(s.get("scheme", "")).lower() == "bearer" or s.get("type") == "oauth2":
            auth_header, auth_kind = "Authorization", "bearer token"
    out = []
    for path, item in doc["paths"].items():
        op = (item or {}).get("get")
        if not op or "{" in path:  # item endpoints (/orders/{id}) are not lists to sync
            continue
        resp = (op.get("responses") or {}).get("200") or (op.get("responses") or {}).get("default") or {}
        schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema") if "content" in resp else resp.get("schema")
        shape = _list_shape(schema or {}, doc)
        if not shape:
            continue
        records_key, item_schema, next_key = shape
        params = {(p := _resolve(x, doc)).get("name"): p for x in (op.get("parameters") or []) + (item.get("parameters") or [])
                  if _resolve(x, doc).get("in") == "query"}
        names = list(params)
        why: list[str] = []
        cfg: dict[str, Any] = {"url": urljoin(server.rstrip("/") + "/", path.lstrip("/")) if server else path}
        if records_key:
            cfg["records_key"] = records_key
            why.append(f"records are under '{records_key}'")
        cursor_param = next((n for n in names if n in _CURSOR), None)
        page_param = next((n for n in names if n in _PAGE), None)
        if next_key:
            cfg["next_key"] = next_key
            if cursor_param:
                cfg["page_param"] = cursor_param
            why.append(f"pages link through '{next_key}'" + (f" sent back as '{cursor_param}'" if cursor_param else ""))
        elif page_param:
            cfg["page_param"] = page_param
            why.append(f"pages are numbered with '{page_param}'")
        elif any(n in _OFFSET for n in names):
            why.append("the API pages by offset, which the REST connector does not follow yet: only the first page is read")
        size = next((n for n in names if n in _SIZE), None)
        if size:
            cfg["page_size_param"] = size
            cap = params[size].get("schema", params[size]).get("maximum")
            cfg["page_size"] = str(min(int(cap), 200) if cap else 200)
        since = next((n for n in names if _SINCE.search(n)), None)
        fields = sorted((item_schema.get("properties") or {}).keys())
        cursor_column = next((f for f in fields if _TIME_FIELD.search(f)), None)
        if since:
            cfg["since_param"] = since
            why.append(f"incremental syncs ask for records with '{since}'" + (f", using '{cursor_column}' as the cursor" if cursor_column else ""))
        if auth_header:
            cfg["auth_header"] = auth_header
            why.append(f"the API declares a {auth_kind} ({auth_header})")
        table = re.sub(r"[^a-z0-9_]+", "_", path.strip("/").split("/")[-1].lower()) or "records"
        out.append({"endpoint": path, "summary": op.get("summary") or op.get("operationId") or "", "kind": "rest", "table": table,
                    "config": cfg, "cursor_column": cursor_column if since else None, "fields": fields, "why": why,
                    "needs_token": bool(auth_header)})
    out.sort(key=lambda p: p["endpoint"])
    return out


def _mask(v: Any) -> Any:
    if isinstance(v, str):
        return v[:2] + "…" if len(v) > 2 else "…"
    if isinstance(v, int | float) and not isinstance(v, bool):
        return type(v)(0)
    if isinstance(v, dict):
        return {k: _mask(x) for k, x in list(v.items())[:10]}
    if isinstance(v, list):
        return [_mask(x) for x in v[:3]]
    return v


class DraftService:
    def __init__(self, stores: Stores, guard: Guard, connections) -> None:
        self.stores = stores
        self.guard = guard
        self.connections = connections

    def _row(self, ws: str, draft_id: str) -> dict[str, Any]:
        rows = self.stores.control.query("SELECT * FROM connector_drafts WHERE id = ? AND workspace_id = ?", [draft_id, ws])
        if not rows:
            raise KeyError(draft_id)
        r = rows[0]
        r["proposal"] = json.loads(r["proposal"])
        r["test_report"] = json.loads(r["test_report"]) if r["test_report"] else None
        return r

    def create(self, actor: Principal, ws: str, *, name: str, openapi: dict[str, Any], base_url: str | None = None) -> list[dict[str, Any]]:
        """Drafts for every list endpoint in an API description. Editors and up; nothing connects yet."""
        self.guard.require(actor, ws, "data.ingest", name)
        proposals = propose_from_openapi(openapi, base_url=base_url)
        if not proposals:
            raise ValueError("no GET endpoint in this description returns a list of records")
        title = str((openapi.get("info") or {}).get("title") or name)
        out = []
        for p in proposals:
            did = "cd_" + uuid.uuid4().hex[:10]
            self.stores.control.execute(
                "INSERT INTO connector_drafts (id, workspace_id, name, kind, source, proposal, status, created_by) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)",
                [did, ws, f"{name} {p['table']}", p["kind"], f"openapi:{title}", json.dumps(p), actor.user_id])
            out.append({"id": did, "name": f"{name} {p['table']}", "status": "draft", **p})
        self.guard.audit(actor, ws, "connector.draft", name, "ok", {"drafts": len(out), "source": title})
        return out

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read")
        rows = self.stores.control.query("SELECT * FROM connector_drafts WHERE workspace_id = ? ORDER BY created_at DESC", [ws])
        for r in rows:
            r["proposal"] = json.loads(r["proposal"])
            r["test_report"] = json.loads(r["test_report"]) if r["test_report"] else None
        return rows

    def get(self, actor: Principal, ws: str, draft_id: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.read", draft_id)
        return self._row(ws, draft_id)

    def edit(self, actor: Principal, ws: str, draft_id: str, config: dict[str, Any], cursor_column: str | None = None) -> dict[str, Any]:
        """Correct a proposal before testing; a changed draft must be tested again."""
        self.guard.require(actor, ws, "data.ingest", draft_id)
        d = self._row(ws, draft_id)
        if d["status"] in ("promoted", "rejected"):
            raise ValueError(f"this draft is {d['status']}")
        from . import CONNECTORS  # noqa: PLC0415

        allowed = {f.name for f in CONNECTORS[d["kind"]].manifest.fields if not f.secret}
        unknown = sorted(set(config) - allowed)
        if unknown:
            raise ValueError(f"not settings of the {d['kind']} connector: {', '.join(unknown)}")
        p = {**d["proposal"], "config": {**d["proposal"]["config"], **config}, "cursor_column": cursor_column or d["proposal"].get("cursor_column")}
        self.stores.control.execute("UPDATE connector_drafts SET proposal = ?, status = 'draft', test_report = NULL, updated_at = now() WHERE id = ?",
                                    [json.dumps(p), draft_id])
        return self._row(ws, draft_id)

    def test(self, actor: Principal, ws: str, draft_id: str, *, token: str | None = None) -> dict[str, Any]:
        """A dry run against the live API: connection test, two pages, fields and masked samples. Nothing is stored but
        the report; the token is used for this call only."""
        self.guard.require(actor, ws, "data.ingest", draft_id)
        d = self._row(ws, draft_id)
        p = d["proposal"]
        conn = self.connections._connector(d["kind"], p["config"], {"token": token} if token else {})
        from .reliability import ACTIONS, classify  # noqa: PLC0415

        report: dict[str, Any] = {"ok": False}
        try:
            report["test"] = conn.test()
            pages = []
            for batch in conn.read(p["table"], cursor_column=None, after=None, batch_rows=1):  # one table per page
                pages.append(batch)
                if len(pages) >= 2:
                    break
            rows = pages[0].to_pylist() if pages else []
            report.update(ok=True, pages_read=len(pages), first_page_rows=len(rows),
                          second_page_differs=(len(pages) > 1 and pages[1].to_pylist() != rows),
                          fields=sorted({k for r in rows for k in r}), sample=[_mask(r) for r in rows[:3]])
            declared = set(p.get("fields") or [])
            if declared and report["fields"]:
                report["undeclared_fields"] = sorted(set(report["fields"]) - declared)
                report["missing_fields"] = sorted(declared - set(report["fields"]))
            if p.get("cursor_column") and p["cursor_column"] not in report["fields"]:
                report["warnings"] = [f"the cursor column {p['cursor_column']} is not in the records; incremental syncs will reload in full"]
        except Exception as exc:  # noqa: BLE001 - the report carries the scrubbed reason and what to do
            category = classify(exc)
            report.update(error=str(exc)[:300], category=category, action=ACTIONS[category])
        status = "tested" if report["ok"] else "draft"
        self.stores.control.execute("UPDATE connector_drafts SET status = ?, test_report = ?, updated_at = now() WHERE id = ?",
                                    [status, json.dumps(report, default=str), draft_id])
        self.guard.audit(actor, ws, "connector.draft_test", draft_id, "ok" if report["ok"] else "failed", {"category": report.get("category")})
        return {"id": draft_id, "status": status, "report": report}

    def promote(self, actor: Principal, ws: str, draft_id: str, *, token: str | None = None, note: str = "") -> dict[str, Any]:
        """An admin other than the drafter turns a tested draft into a real connection (tested again, secret sealed)."""
        self.guard.require(actor, ws, "workspace.admin", draft_id)
        d = self._row(ws, draft_id)
        if d["created_by"] == actor.user_id:
            raise AccessDenied("a drafted connection is promoted by an admin other than the person who drafted it")
        if d["status"] != "tested":
            raise ValueError("test the draft successfully before promoting it" if d["status"] == "draft" else f"this draft is {d['status']}")
        p = d["proposal"]
        values = {**p["config"], **({"token": token} if token else {})}
        conn = self.connections.create(actor, ws, kind=d["kind"], name=d["name"], values=values)
        self.stores.control.execute("UPDATE connector_drafts SET status = 'promoted', connection_id = ?, reviewed_by = ?, review_note = ?, "
                                    "updated_at = now() WHERE id = ?", [conn["id"], actor.user_id, note or None, draft_id])
        self.guard.audit(actor, ws, "connector.promote", draft_id, "ok", {"connection": conn["id"], "drafted_by": d["created_by"]})
        return {"id": draft_id, "status": "promoted", "connection_id": conn["id"], "table": p["table"], "cursor_column": p.get("cursor_column")}

    def reject(self, actor: Principal, ws: str, draft_id: str, *, note: str = "") -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.admin", draft_id)
        self._row(ws, draft_id)
        self.stores.control.execute("UPDATE connector_drafts SET status = 'rejected', reviewed_by = ?, review_note = ?, updated_at = now() WHERE id = ?",
                                    [actor.user_id, note or None, draft_id])
        self.guard.audit(actor, ws, "connector.reject", draft_id, "ok")
        return {"id": draft_id, "status": "rejected"}


__all__ = ["DraftService", "propose_from_openapi"]
