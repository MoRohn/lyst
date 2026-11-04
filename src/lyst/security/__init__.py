"""Identity, access, audit and redaction. Every service call passes through ``Guard``.

Roles follow Acumane's MembershipRole (OWNER, ADMIN, EDITOR, VIEWER). Permissions are
checked in code at the service boundary, never left to a prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
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
    "glossary.write": "admin",
    "memory.write": "editor",
    "memory.promote": "admin",
    "task.create": "viewer",
    "task.control": "editor",
    "schedule.create": "editor",
    "approval.decide": "admin",
    "skill.propose": "editor",
    "skill.approve": "owner",
    "workspace.admin": "owner",
    "audit.read": "admin",
}


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    kind: str = "user"  # user | agent | system


SYSTEM = Principal("system", "system@lyst.local", kind="system")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Guard:
    def __init__(self, stores: Stores) -> None:
        self.stores = stores

    # -- identity ---------------------------------------------------------------------------

    def create_user(self, email: str, name: str) -> Principal:
        uid = "u_" + uuid.uuid4().hex[:12]
        self.stores.control.execute("INSERT INTO users (id, email, name) VALUES (?, ?, ?)", [uid, email, name])
        return Principal(uid, email)

    def issue_token(self, user: Principal, label: str = "default") -> str:
        token = "lyst_" + secrets.token_urlsafe(32)
        self.stores.control.execute(
            "INSERT INTO api_tokens (token_hash, user_id, label) VALUES (?, ?, ?)",
            [hash_token(token), user.user_id, label],
        )
        return token

    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AccessDenied("missing credentials")
        rows = self.stores.control.query(
            "SELECT u.id, u.email FROM api_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_hash = ? AND t.revoked_at IS NULL",
            [hash_token(token)],
        )
        if not rows:
            self.audit(Principal("anonymous", ""), None, "auth.token", None, "denied")
            raise AccessDenied("invalid credentials")
        return Principal(rows[0]["id"], rows[0]["email"])

    # -- workspaces -------------------------------------------------------------------------

    def create_workspace(self, owner: Principal, workspace_id: str, name: str) -> None:
        def tx(c):
            c.execute("INSERT INTO workspaces (id, name) VALUES (?, ?)", [workspace_id, name])
            c.execute(
                "INSERT INTO memberships (workspace_id, user_id, role) VALUES (?, ?, 'owner')",
                [workspace_id, owner.user_id],
            )

        self.stores.workspace(workspace_id)  # validates id, creates the encrypted file
        self.stores.control.write(tx)
        self.audit(owner, workspace_id, "workspace.create", workspace_id, "ok")

    def add_member(self, actor: Principal, workspace_id: str, user_id: str, role: str) -> None:
        self.require(actor, workspace_id, "workspace.admin")
        if role not in RANK:
            raise ValueError("unknown role")
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
        if not self.can(principal, workspace_id, action):
            self.audit(principal, workspace_id, action, target, "denied")
            raise AccessDenied(f"{principal.email or principal.user_id} may not {action} in {workspace_id}")
        return self.role(principal, workspace_id) or "viewer"

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
