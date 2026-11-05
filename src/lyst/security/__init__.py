# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Identity, access, audit and redaction. Every service call passes through ``Guard``.

Roles follow Acumane's MembershipRole (OWNER, ADMIN, EDITOR, VIEWER). Permissions are
checked in code at the service boundary, never left to a prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..store import Stores

ROLES = ("viewer", "editor", "admin", "owner")
RANK = {r: i for i, r in enumerate(ROLES)}

# action -> minimum role
PERMISSIONS: dict[str, str] = {
    "workspace.read": "viewer",
    "analyst.ask": "viewer",
    "data.read": "viewer",
    "data.query": "viewer",
    "data.ingest": "editor",
    "metric.read": "viewer",
    "metric.define": "admin",
    "contract.define": "admin",
    "glossary.write": "admin",
    "memory.write": "editor",
    "memory.promote": "admin",
    "task.create": "viewer",
    "task.control": "editor",
    "schedule.create": "editor",
    "approval.decide": "admin",
    "skill.propose": "editor",
    "skill.approve": "owner",
    "workspace.admin": "admin",
    "audit.read": "admin",
    "scenario.define": "editor",
    "decision.create": "editor",
    "voice.use": "viewer",
    "voice.admin": "admin",
}


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    kind: str = "user"  # user | agent | system
    token_label: str | None = None       # how the session was issued: oidc, cli, dev, default
    auth_age_seconds: float | None = None  # seconds since the session was issued (recent-auth checks)


SYSTEM = Principal("system", "system@lyst.local", kind="system")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


TOKEN_TOUCH_SECONDS = 60  # last_used_at is recorded at most once a minute per token


class Guard:
    def __init__(self, stores: Stores) -> None:
        self.stores = stores
        # Authentication is a read. Recording last_used_at on every request made every poll a write on the
        # control database's single writer, so reads queued behind writes (P1). Once a minute is enough to say
        # when a token was last used.
        self._touched: dict[str, float] = {}
        self._touch_lock = threading.Lock()

    # -- identity ---------------------------------------------------------------------------

    def create_user(self, email: str, name: str) -> Principal:
        email = email.strip().lower()  # stored as it is looked up, so a mixed-case address is still found
        uid = "u_" + uuid.uuid4().hex[:12]
        self.stores.control.execute("INSERT INTO users (id, email, name) VALUES (?, ?, ?)", [uid, email, name])
        return Principal(uid, email)

    def issue_token(self, user: Principal, label: str = "default", *, hours: int | None = None) -> str:
        token = "lyst_" + secrets.token_urlsafe(32)
        self.stores.control.execute(
            "INSERT INTO api_tokens (token_hash, user_id, label, expires_at) VALUES (?, ?, ?, "
            "CASE WHEN ? IS NULL THEN NULL ELSE now() + to_hours(CAST(? AS BIGINT)) END)",
            [hash_token(token), user.user_id, label, hours, hours or 0],
        )
        return token

    def revoke_token(self, token: str) -> None:
        self.stores.control.execute("UPDATE api_tokens SET revoked_at = now() WHERE token_hash = ?", [hash_token(token)])

    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AccessDenied("missing credentials")
        h = hash_token(token)
        rows = self.stores.control.query(
            "SELECT u.id, u.email, t.label, epoch(now() - t.created_at) AS age FROM api_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_hash = ? AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > now())",
            [h],
        )
        if not rows:
            self.audit(Principal("anonymous", ""), None, "auth.token", None, "denied")
            raise AccessDenied("invalid or expired credentials")
        now = time.monotonic()
        with self._touch_lock:
            due = self._touched.get(h, -TOKEN_TOUCH_SECONDS) <= now - TOKEN_TOUCH_SECONDS
            if due:
                self._touched[h] = now
                if len(self._touched) > 10_000:  # forget tokens not seen for a touch interval (revoked, expired, idle)
                    self._touched = {k: t for k, t in self._touched.items() if t > now - TOKEN_TOUCH_SECONDS}
        if due:
            self.stores.control.execute("UPDATE api_tokens SET last_used_at = now() WHERE token_hash = ?", [h])
        return Principal(rows[0]["id"], rows[0]["email"], token_label=rows[0]["label"], auth_age_seconds=float(rows[0]["age"] or 0))

    def user_from_oidc(self, subject: str, email: str, name: str) -> Principal:
        """Find or create the user a verified ID token describes. Email is the identity; the subject is kept."""
        rows = self.stores.control.query("SELECT id, email FROM users WHERE lower(email) = ?", [email.strip().lower()])
        if rows:
            self.stores.control.execute("UPDATE users SET subject = ?, last_login_at = now() WHERE id = ?",
                                        [subject, rows[0]["id"]])
            p = Principal(rows[0]["id"], rows[0]["email"])
        else:
            p = self.create_user(email, name or email.split("@")[0])
            self.stores.control.execute("UPDATE users SET subject = ?, last_login_at = now() WHERE id = ?", [subject, p.user_id])
        self.audit(p, None, "auth.oidc", subject, "ok")
        return p

    def user_by_email(self, email: str) -> Principal | None:
        rows = self.stores.control.query("SELECT id, email FROM users WHERE lower(email) = ?", [email.strip().lower()])
        return Principal(rows[0]["id"], rows[0]["email"]) if rows else None

    def members(self, actor: Principal, workspace_id: str) -> list[dict[str, Any]]:
        self.require(actor, workspace_id, "workspace.read")
        return self.stores.control.query(
            "SELECT u.id AS user_id, u.email, u.name, m.role FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.workspace_id = ? ORDER BY m.role, u.email", [workspace_id])

    def remove_member(self, actor: Principal, workspace_id: str, user_id: str) -> None:
        self.require(actor, workspace_id, "workspace.admin")

        def tx(c):
            # the check and the delete in one transaction: two removals cannot each see the other owner remaining
            if not c.execute("SELECT count(*) FROM memberships WHERE workspace_id = ? AND user_id = ?",
                             [workspace_id, user_id]).fetchone()[0]:
                raise KeyError(user_id)
            owners = c.execute("SELECT count(*) FROM memberships WHERE workspace_id = ? AND role = 'owner' AND user_id <> ?",
                               [workspace_id, user_id]).fetchone()[0]
            if not owners:
                raise ValueError("a workspace keeps at least one owner")
            c.execute("DELETE FROM memberships WHERE workspace_id = ? AND user_id = ?", [workspace_id, user_id])

        self.stores.control.write(tx)
        self.audit(actor, workspace_id, "member.remove", user_id, "ok")

    # -- workspaces -------------------------------------------------------------------------

    def create_workspace(self, owner: Principal, workspace_id: str, name: str) -> None:
        def tx(c):
            default = self.stores.settings.llm.external_default
            if default is None:  # follow the installation: external use is on once OpenRouter is connected install-wide
                default = self.stores.settings.llm.provider == "openrouter" and bool(c.execute(
                    "SELECT count(*) FROM credentials WHERE provider = 'openrouter' AND scope_kind = 'installation' AND status = 'active'"
                ).fetchall()[0][0])
            c.execute("INSERT INTO workspaces (id, name, allow_external_llm) VALUES (?, ?, ?)", [workspace_id, name, default])
            c.execute(
                "INSERT INTO memberships (workspace_id, user_id, role) VALUES (?, ?, 'owner')",
                [workspace_id, owner.user_id],
            )

        existed = self.stores.settings.workspace_db(workspace_id).exists() if _ws_id_ok(workspace_id) else True
        self.stores.workspace(workspace_id)  # validates id, creates the encrypted file
        try:
            self.stores.control.write(tx)
        except BaseException:
            if not existed:  # a failed creation leaves no orphaned workspace file behind
                self.stores.discard_workspace(workspace_id)
            raise
        self.audit(owner, workspace_id, "workspace.create", workspace_id, "ok")

    def add_member(self, actor: Principal, workspace_id: str, user_id: str, role: str) -> None:
        self.require(actor, workspace_id, "workspace.admin")
        if role not in RANK:
            raise ValueError("unknown role")
        if role == "owner" and self.role(actor, workspace_id) != "owner":
            raise AccessDenied("only an owner can make another owner")
        if not self.stores.control.scalar("SELECT count(*) FROM users WHERE id = ?", [user_id]):
            raise KeyError(user_id)
        self.stores.control.execute(
            "INSERT OR REPLACE INTO memberships (workspace_id, user_id, role) VALUES (?, ?, ?)",
            [workspace_id, user_id, role],
        )
        self.audit(actor, workspace_id, "member.set", f"{user_id}:{role}", "ok")

    def role(self, principal: Principal, workspace_id: str) -> str | None:
        if principal.kind == "system":
            return "owner"
        return self.stores.control.scalar(
            "SELECT role FROM memberships WHERE workspace_id = ? AND user_id = ?",
            [workspace_id, principal.user_id],
        )

    def workspaces_for(self, principal: Principal) -> list[dict[str, Any]]:
        return self.stores.control.query(
            "SELECT w.id, w.name, m.role, w.autonomy FROM workspaces w JOIN memberships m "
            "ON m.workspace_id = w.id WHERE m.user_id = ? ORDER BY w.name",
            [principal.user_id],
        )

    # -- authorization ----------------------------------------------------------------------

    def can(self, principal: Principal, workspace_id: str, action: str) -> bool:
        need = PERMISSIONS.get(action)
        if need is None:
            return False  # unknown actions are denied, not allowed
        have = self.role(principal, workspace_id)
        return have is not None and RANK[have] >= RANK[need]

    def require(self, principal: Principal, workspace_id: str, action: str, target: str | None = None) -> str:
        need = PERMISSIONS.get(action)
        have = self.role(principal, workspace_id) if need is not None else None  # one membership lookup, not two
        if need is None or have is None or RANK[have] < RANK[need]:
            self.audit(principal, workspace_id, action, target, "denied")
            raise AccessDenied(f"{principal.email or principal.user_id} may not {action} in {workspace_id}")
        return have

    # -- audit ------------------------------------------------------------------------------

    def audit(
        self,
        actor: Principal,
        workspace_id: str | None,
        action: str,
        target: str | None,
        outcome: str,
        detail: dict | None = None,
    ) -> None:
        self.stores.control.execute(
            "INSERT INTO audit_log (id, actor, workspace_id, action, target, outcome, detail) "
            "VALUES (nextval('audit_seq'), ?, ?, ?, ?, ?, ?)",
            [actor.user_id, workspace_id, action, target, outcome, json.dumps(redact_obj(detail or {}))],
        )


def _ws_id_ok(workspace_id: str) -> bool:
    from ..store import _WS_ID  # noqa: PLC0415

    return bool(_WS_ID.match(workspace_id or ""))


# -- sensitive information ------------------------------------------------------------------

_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "phone": re.compile(r"\+?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    "secret": re.compile(r"\b(?:sk|pk|lyst|ghp|xox[bp])[-_][A-Za-z0-9_\-]{16,}\b"),
}


def detect_sensitive(text: str) -> list[str]:
    return [kind for kind, pat in _PATTERNS.items() if pat.search(text)]


def redact(text: str) -> str:
    for kind, pat in _PATTERNS.items():
        text = pat.sub(f"[{kind}]", text)
    return text


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj


def untrusted(source: str, text: str) -> str:
    """Wrap retrieved content so a model sees it as data. The real defence is that tool
    permissions never depend on model output; this only lowers the odds of confusion."""
    fence = secrets.token_hex(4)
    body = text.replace("</untrusted", "&lt;/untrusted")
    return f'<untrusted source="{source}" id="{fence}">\n{body}\n</untrusted id="{fence}">'
