# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Publishing: a finished analysis as a read-only, versioned page for an audience, revocable at any time.

A publication is a snapshot, not a live view: the briefing of a finished analysis (answer, evidence, tables,
checks and the queries behind it) rendered to self-contained HTML with no scripts and no external requests. It
has an audience:

* ``members``: workspace members with at least ``min_role``, signed in (``GET .../publications/{id}/content``).
* ``link``: anyone holding the link (``GET /p/<token>``), for a limited time (30 days unless set). Only the token's
  SHA-256 is stored; the link is shown once. Link publications are refused when the analysis read a dataset
  classified confidential or restricted, when a registered check refuses, or when the workspace's export ceiling
  (``governance.check_export``) does.

Republishing a newer analysis under the same publication makes a new version behind the same link; earlier
versions stay readable by members. Revoking stops the link and members' access at once. Every view is counted and
recorded (``publication_views``), and every publish and revoke is audited.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import secrets
import uuid
from collections.abc import Callable
from typing import Any

from .data.sqlguard import QueryRejected, check_read_query
from .deliverables import briefing
from .security import RANK, SYSTEM, AccessDenied, Guard, Principal
from .store import Stores

OUTSIDE_BLOCKED = {"confidential", "restricted"}
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def _inline(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", t)
    return t


def markdown_to_html(md: str) -> str:
    """The briefing's Markdown subset (headings, lists, pipe tables, code blocks, emphasis) to escaped HTML."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif ln.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|(\s*-+\s*\|)+\s*$", lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", lines[i].strip().strip("|"))])
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")
            continue
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    return "\n".join(out)


PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{title}</title><style>
:root{{color-scheme:light dark;--fg:#1c2321;--muted:#5b6661;--line:#d9dfdc;--accent:#0B6B64;--bg:#fff}}
@media (prefers-color-scheme:dark){{:root{{--fg:#e6ebe9;--muted:#9aa7a1;--line:#33403b;--accent:#4fb8ad;--bg:#121816}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:860px;margin:0 auto;padding:32px 16px 64px}} h1{{font-size:1.6rem;line-height:1.25}} h2{{margin-top:2rem;color:var(--accent)}}
table{{border-collapse:collapse;width:100%;overflow-x:auto;display:block;font-variant-numeric:tabular-nums}}
th,td{{border-bottom:1px solid var(--line);padding:6px 10px;text-align:left}} pre{{overflow-x:auto;background:rgba(127,127,127,.1);padding:12px}}
footer{{margin-top:3rem;color:var(--muted);font-size:.85rem;border-top:1px solid var(--line);padding-top:1rem}}
</style></head><body><main>{body}<footer>{footer}</footer></main></body></html>"""


class PublishingService:
    def __init__(self, stores: Stores, guard: Guard, data, runtime) -> None:
        self.stores = stores
        self.guard = guard
        self.data = data
        self.runtime = runtime
        self.publish_checks: list[Callable[[Principal, str, dict[str, Any]], None]] = []
        self.governance: Any = None

    def _task(self, actor: Principal, ws: str, task_id: str) -> dict[str, Any]:
        t = self.runtime.get(actor, task_id)
        if t.get("workspace_id", ws) != ws:
            raise KeyError(task_id)
        if t["status"] != "done" or not t.get("result"):
            raise ValueError("only a finished analysis can be published")
        return t

    def datasets_read(self, ws: str, task_id: str) -> list[str]:
        """The datasets the analysis's queries read, parsed by the same guard that ran them."""
        policy = self.data.policy_for(SYSTEM, ws)
        found: set[str] = set()
        for r in self.stores.workspace(ws).query("SELECT sql FROM query_history WHERE task_id = ? AND outcome = 'ok'", [task_id]):
            try:
                found |= set(check_read_query(r["sql"], policy).datasets)
            except QueryRejected:
                continue
        return sorted(found)

    def _check(self, actor: Principal, ws: str, audience: str, datasets: list[str], task_id: str | None = None) -> None:
        scope = {"datasets": datasets, "purpose": "publish", "audience": audience, "task_id": task_id}
        for check in self.publish_checks:
            check(actor, ws, scope)
        if audience != "link":
            return
        if datasets:
            marks = self.stores.workspace(ws).query(
                f"SELECT name, classification FROM datasets WHERE name IN ({','.join('?' * len(datasets))})", datasets)
            blocked = [f"{m['name']} ({m['classification']})" for m in marks if m["classification"] in OUTSIDE_BLOCKED]
            if blocked:
                raise AccessDenied(f"a link reaches people outside the workspace, and this analysis read {', '.join(blocked)}; "
                                   "publish it to members instead")
        gov = self.governance
        if gov is not None and hasattr(gov, "check_export") and scope.get("task_id"):
            gov.check_export(actor, ws, scope["task_id"])  # a link is an export: the workspace's export ceiling applies
        elif gov is not None and hasattr(gov, "evaluate"):
            verdict = gov.evaluate(actor, ws, datasets=datasets, purpose="publish")
            if isinstance(verdict, dict) and (verdict.get("outcome") == "refused" or verdict.get("allowed") is False):
                raise AccessDenied(verdict.get("reason") or "the workspace's data policy does not allow publishing this")

    def _render(self, task: dict[str, Any], title: str, version: int, audience: str) -> tuple[str, str]:
        md = briefing(task["result"], task)
        stamp = dt.datetime.now(dt.UTC).strftime("%d %b %Y %H:%M UTC")
        footer = (f"Published with Lyst · version {version} · {stamp} · "
                  + ("shared by link; numbers are as of publication and do not update" if audience == "link"
                     else "for workspace members; numbers are as of publication"))
        page = PAGE.format(lang="en", title=html.escape(title), body=markdown_to_html(md), footer=html.escape(footer))
        return md, page

    def publish(self, actor: Principal, ws: str, task_id: str, *, audience: str = "members", title: str | None = None,
                min_role: str = "viewer", expires_days: int | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.ingest" if audience == "members" else "workspace.admin", task_id)
        if audience not in ("members", "link"):
            raise ValueError("audience must be members or link")
        if min_role not in RANK:
            raise ValueError("unknown role")
        task = self._task(actor, ws, task_id)
        datasets = self.datasets_read(ws, task_id)
        self._check(actor, ws, audience, datasets, task_id)
        pid = "pub_" + uuid.uuid4().hex[:10]
        title = (title or task["result"].get("question") or task.get("title") or "Analysis")[:200]
        token = secrets.token_urlsafe(24) if audience == "link" else None
        expires = None
        if audience == "link":
            days = 30 if expires_days is None else max(1, min(int(expires_days), 365))
            expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
        md, page = self._render(task, title, 1, audience)

        def tx(c):
            c.execute("INSERT INTO publications (id, title, audience, min_role, link_hash, expires_at, status, current_version, "
                      "created_by) VALUES (?, ?, ?, ?, ?, ?, 'published', 1, ?)",
                      [pid, title, audience, min_role, hashlib.sha256(token.encode()).hexdigest() if token else None, expires,
                       actor.user_id])
            c.execute("INSERT INTO publication_versions (publication_id, version, task_id, markdown, html, sha256, datasets, "
                      "created_by) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                      [pid, task_id, md, page, hashlib.sha256(page.encode()).hexdigest(), json.dumps(datasets), actor.user_id])

        self.stores.workspace(ws).write(tx)
        if token:  # the installation-wide index from a link's fingerprint to its workspace
            self.stores.control.execute("INSERT INTO publication_links (link_hash, workspace_id, publication_id) VALUES (?, ?, ?)",
                                        [hashlib.sha256(token.encode()).hexdigest(), ws, pid])
        self.guard.audit(actor, ws, "publication.publish", pid, "ok", {"audience": audience, "task": task_id, "datasets": datasets})
        out = self.get(actor, ws, pid)
        if token:
            out["url"] = f"/p/{token}"
            out["note"] = "the link is shown once; Lyst keeps only its fingerprint"
        return out

    def republish(self, actor: Principal, ws: str, pid: str, task_id: str, *, note: str = "") -> dict[str, Any]:
        pub = self.get(actor, ws, pid)
        self.guard.require(actor, ws, "data.ingest" if pub["audience"] == "members" else "workspace.admin", pid)
        if pub["status"] != "published":
            raise ValueError(f"this publication is {pub['status']}")
        task = self._task(actor, ws, task_id)
        datasets = self.datasets_read(ws, task_id)
        self._check(actor, ws, pub["audience"], datasets, task_id)
        version = pub["current_version"] + 1
        md, page = self._render(task, pub["title"], version, pub["audience"])

        def tx(c):
            c.execute("INSERT INTO publication_versions (publication_id, version, task_id, markdown, html, sha256, datasets, note, "
                      "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [pid, version, task_id, md, page, hashlib.sha256(page.encode()).hexdigest(), json.dumps(datasets), note,
                       actor.user_id])
            c.execute("UPDATE publications SET current_version = ? WHERE id = ?", [version, pid])

        self.stores.workspace(ws).write(tx)
        self.guard.audit(actor, ws, "publication.publish", pid, "ok", {"version": version, "task": task_id})
        return self.get(actor, ws, pid)

    def revoke(self, actor: Principal, ws: str, pid: str) -> dict[str, Any]:
        pub = self.get(actor, ws, pid)
        if pub["created_by"] != actor.user_id:
            self.guard.require(actor, ws, "workspace.admin", pid)
        self.stores.workspace(ws).execute("UPDATE publications SET status = 'revoked', revoked_by = ?, revoked_at = now(), "
                                          "link_hash = NULL WHERE id = ?", [actor.user_id, pid])
        self.stores.control.execute("DELETE FROM publication_links WHERE workspace_id = ? AND publication_id = ?", [ws, pid])
        self.guard.audit(actor, ws, "publication.revoke", pid, "ok", {"audience": pub["audience"]})
        return self.get(actor, ws, pid)

    def get(self, actor: Principal, ws: str, pid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read", pid)
        rows = self.stores.workspace(ws).query(
            "SELECT id, title, audience, min_role, expires_at, status, current_version, views, created_by, created_at, "
            "revoked_by, revoked_at FROM publications WHERE id = ?", [pid])
        if not rows:
            raise KeyError(pid)
        p = rows[0]
        p["versions"] = self.stores.workspace(ws).query(
            "SELECT version, task_id, sha256, datasets, note, created_by, created_at FROM publication_versions "
            "WHERE publication_id = ? ORDER BY version", [pid])
        for v in p["versions"]:
            v["datasets"] = json.loads(v["datasets"])
        return p

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        return self.stores.workspace(ws).query(
            "SELECT id, title, audience, status, current_version, views, expires_at, created_by, created_at FROM publications "
            "ORDER BY created_at DESC")

    def content(self, actor: Principal, ws: str, pid: str, version: int | None = None) -> str:
        role = self.guard.require(actor, ws, "workspace.read", pid)
        pub = self.get(actor, ws, pid)
        if pub["status"] != "published":
            raise KeyError(pid)
        if RANK[role] < RANK[pub["min_role"]]:
            raise AccessDenied(f"this publication is for {pub['min_role']}s and above")
        return self._page(ws, pid, version or pub["current_version"], actor.user_id)

    def _page(self, ws: str, pid: str, version: int, viewer: str) -> str:
        rows = self.stores.workspace(ws).query("SELECT html FROM publication_versions WHERE publication_id = ? AND version = ?",
                                               [pid, version])
        if not rows:
            raise KeyError(f"{pid} v{version}")

        def tx(c):
            c.execute("INSERT INTO publication_views (publication_id, version, viewer) VALUES (?, ?, ?)", [pid, version, viewer])
            c.execute("UPDATE publications SET views = views + 1 WHERE id = ?", [pid])

        self.stores.workspace(ws).write(tx)
        return rows[0]["html"]

    def by_link(self, token: str) -> str | None:
        """The current version behind a link, or None (unknown, revoked or expired: the same answer for all three)."""
        if not token or len(token) > 64:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        link = self.stores.control.query("SELECT workspace_id FROM publication_links WHERE link_hash = ?", [digest])
        if not link:
            return None
        ws = link[0]["workspace_id"]
        rows = self.stores.workspace(ws).query(
            "SELECT id, current_version FROM publications WHERE link_hash = ? AND status = 'published' "
            "AND audience = 'link' AND (expires_at IS NULL OR expires_at > now())", [digest])
        return self._page(ws, rows[0]["id"], rows[0]["current_version"], "link") if rows else None


__all__ = ["CSP", "PublishingService", "markdown_to_html"]
