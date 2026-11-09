# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Collaboration: comments, change requests and decisions on any analytical object; assignments; reviews that gate
changes to shared definitions; sharing and ownership; and personal notifications people can tune.

Objects are addressed as ``(kind, id)``: ``task`` (an investigation), ``analysis``, ``asset`` (a report, dashboard,
workflow and so on from the registry), ``metric``, ``dataset``. Reading an object's discussion needs the same access
as reading the object (a private asset's thread is visible only to the people who can see the asset).

* **Comments** have a kind: ``comment``, ``change_request`` (open until resolved) or ``decision`` (the record of
  what was decided and why). ``@name@example.com`` mentions notify that member.
* **Assignments** hand an object to a member with a note and an optional due date; completing one tells the person
  who assigned it.
* **Reviews** gate changes that affect everyone: an editor may *propose* a new or changed metric definition, a data
  contract, or the publication of a private asset to the workspace; an admin who is not the proposer approves it,
  and only then is it applied, as the approver, with the review id in the audit. A review can also be a plain
  request for someone to look at an object.
* **Sharing and ownership**: an asset can be private, shared with named members, or visible to the workspace; its
  owner (or an admin) can hand ownership to another editor. Ownership of datasets and metrics transfers the same way.
* **Notifications** go to each person's own list (mentions, assignments, review requests and decisions, completed
  assignments, comments on what they own). Each kind can be switched off per person; platform notices (contract
  breaches, connection problems, broken assets) honour the same switches.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .security import RANK, AccessDenied, Guard, Principal
from .store import Stores

OBJECT_KINDS = ("task", "analysis", "asset", "metric", "dataset")
COMMENT_KINDS = ("comment", "change_request", "decision")
NOTIFICATION_KINDS = ("mention", "assignment", "assignment_done", "review_requested", "review_decided", "comment_on_owned",
                      "contract_breaking", "contract_failed", "connection_needs_action", "asset_broken", "asset_invalidated")
REVIEW_ACTIONS = ("metric.define", "contract.define", "asset.publish", "glossary.map", "look")
_MENTION = re.compile(r"@([\w.+-]+@[\w-]+\.[\w.-]+)")


class CollaborationService:
    def __init__(self, stores: Stores, guard: Guard, *, semantic=None, contracts=None, assets=None) -> None:
        self.stores = stores
        self.guard = guard
        self.semantic = semantic
        self.contracts = contracts
        self.assets = assets

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- objects --------------------------------------------------------------------------------

    def _object(self, actor: Principal, ws: str, kind: str, oid: str) -> dict[str, Any]:
        """The object's title and owner, after checking the actor may read it."""
        if kind not in OBJECT_KINDS:
            raise ValueError(f"object kind must be one of {', '.join(OBJECT_KINDS)}")
        self.guard.require(actor, ws, "workspace.read", f"{kind}:{oid}")
        db = self._db(ws)
        if kind == "task":
            r = self.stores.control.query("SELECT title, created_by AS owner FROM tasks WHERE id = ? AND workspace_id = ?", [oid, ws])
        elif kind == "analysis":
            r = db.query("SELECT question AS title, created_by AS owner FROM analyses WHERE id = ?", [oid])
        elif kind == "asset":
            if self.assets is None:
                raise KeyError(oid)
            a = self.assets.get(actor, ws, oid)  # raises KeyError when the actor may not see it
            r = [{"title": a["title"], "owner": a["owner"]}]
        elif kind == "metric":
            r = db.query("SELECT name AS title, owner FROM metrics WHERE name = ?", [oid])
        else:
            r = db.query("SELECT title, owner FROM datasets WHERE name = ?", [oid])
        if not r:
            raise KeyError(f"{kind} {oid}")
        return r[0]

    def _member(self, ws: str, user_id: str) -> str | None:
        return self.stores.control.scalar("SELECT role FROM memberships WHERE workspace_id = ? AND user_id = ?", [ws, user_id])

    def _name(self, user_id: str | None) -> str:
        if not user_id:
            return "someone"
        r = self.stores.control.query("SELECT name, email FROM users WHERE id = ?", [user_id])
        return (r[0]["name"] or r[0]["email"]) if r else user_id

    # -- notifications --------------------------------------------------------------------------

    def wants(self, ws: str, user_id: str, kind: str) -> bool:
        v = self._db(ws).scalar("SELECT enabled FROM notification_prefs WHERE user_id = ? AND kind = ?", [user_id, kind])
        return True if v is None else bool(v)

    def notify(self, ws: str, user_id: str | None, kind: str, text: str, *, object_kind: str | None = None, object_id: str | None = None,
               actor: Principal | None = None) -> bool:
        if not user_id or (actor and actor.user_id == user_id) or not self._member(ws, user_id) or not self.wants(ws, user_id, kind):
            return False
        self._db(ws).execute("INSERT INTO user_notifications (id, user_id, kind, object_kind, object_id, text, actor) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             ["un_" + uuid.uuid4().hex[:12], user_id, kind, object_kind, object_id, text[:500], actor.user_id if actor else None])
        return True

    def notifications(self, actor: Principal, ws: str, *, unread_only: bool = False) -> list[dict[str, Any]]:
        """This person's notifications, newest first: their own list plus platform notices in the inbox."""
        self.guard.require(actor, ws, "workspace.read")
        db = self._db(ws)
        own = db.query("SELECT id, kind, object_kind, object_id, text, actor, status, created_at FROM user_notifications WHERE user_id = ?"
                       + (" AND status = 'unread'" if unread_only else ""), [actor.user_id])
        inbox = db.query("SELECT n.id, f.kind, 'finding' AS object_kind, f.id AS object_id, f.summary AS text, NULL AS actor, "
                         "CASE WHEN n.status = 'read' THEN 'read' ELSE 'unread' END AS status, f.created_at FROM notifications n "
                         "JOIN findings f ON f.id = n.finding_id WHERE n.user_id = ? AND n.channel = 'inbox'"
                         + (" AND n.status <> 'read'" if unread_only else ""), [actor.user_id])
        rows = own + [r for r in inbox if r["kind"] not in NOTIFICATION_KINDS or self.wants(ws, actor.user_id, r["kind"])]
        return sorted(rows, key=lambda r: str(r["created_at"]), reverse=True)

    def mark_read(self, actor: Principal, ws: str, notification_id: str) -> None:
        self.guard.require(actor, ws, "workspace.read")
        db = self._db(ws)
        db.execute("UPDATE user_notifications SET status = 'read' WHERE id = ? AND user_id = ?", [notification_id, actor.user_id])
        db.execute("UPDATE notifications SET status = 'read' WHERE id = ? AND user_id = ?", [notification_id, actor.user_id])

    def preferences(self, actor: Principal, ws: str) -> dict[str, bool]:
        self.guard.require(actor, ws, "workspace.read")
        return {k: self.wants(ws, actor.user_id, k) for k in NOTIFICATION_KINDS}

    def set_preferences(self, actor: Principal, ws: str, prefs: dict[str, bool]) -> dict[str, bool]:
        self.guard.require(actor, ws, "workspace.read")
        for k, v in prefs.items():
            if k not in NOTIFICATION_KINDS:
                raise ValueError(f"unknown notification kind {k}")
            self._db(ws).execute("INSERT OR REPLACE INTO notification_prefs (user_id, kind, enabled) VALUES (?, ?, ?)", [actor.user_id, k, bool(v)])
        return self.preferences(actor, ws)

    # -- comments -------------------------------------------------------------------------------

    def comment(self, actor: Principal, ws: str, object_kind: str, object_id: str, body: str, *, kind: str = "comment",
                parent_id: str | None = None) -> dict[str, Any]:
        obj = self._object(actor, ws, object_kind, object_id)
        if kind not in COMMENT_KINDS:
            raise ValueError(f"comment kind must be one of {', '.join(COMMENT_KINDS)}")
        if kind == "decision":
            self.guard.require(actor, ws, "task.control", f"{object_kind}:{object_id}")  # decisions are recorded by editors and up
        body = (body or "").strip()
        if not body:
            raise ValueError("a comment needs text")
        if parent_id and not self._db(ws).scalar("SELECT count(*) FROM comments WHERE id = ? AND object_kind = ? AND object_id = ?",
                                                 [parent_id, object_kind, object_id]):
            raise ValueError("the reply's parent is not a comment on this object")
        emails = sorted(set(m.lower() for m in _MENTION.findall(body)))
        mentioned = []
        for e in emails:
            u = self.stores.control.query("SELECT id FROM users WHERE lower(email) = ?", [e])
            if u and self._member(ws, u[0]["id"]):
                mentioned.append(u[0]["id"])
        cid = "cm_" + uuid.uuid4().hex[:12]
        status = "open" if kind == "change_request" else "none"
        self._db(ws).execute("INSERT INTO comments (id, object_kind, object_id, parent_id, kind, body, mentions, status, author) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [cid, object_kind, object_id, parent_id, kind, body, json.dumps(mentioned),
                                                                    status, actor.user_id])
        who = self._name(actor.user_id)
        label = {"comment": "commented on", "change_request": "requested a change to", "decision": "recorded a decision on"}[kind]
        for uid in mentioned:
            self.notify(ws, uid, "mention", f"{who} mentioned you on {object_kind} \"{obj['title'][:80]}\": {body[:160]}",
                        object_kind=object_kind, object_id=object_id, actor=actor)
        if obj["owner"] not in mentioned:
            self.notify(ws, obj["owner"], "comment_on_owned", f"{who} {label} your {object_kind} \"{obj['title'][:80]}\": {body[:160]}",
                        object_kind=object_kind, object_id=object_id, actor=actor)
        self.guard.audit(actor, ws, f"comment.{kind}", f"{object_kind}:{object_id}", "ok", {"comment": cid, "mentions": len(mentioned)})
        return {"id": cid, "kind": kind, "status": status, "mentions": mentioned}

    def comments(self, actor: Principal, ws: str, object_kind: str, object_id: str) -> list[dict[str, Any]]:
        self._object(actor, ws, object_kind, object_id)
        rows = self._db(ws).query("SELECT * FROM comments WHERE object_kind = ? AND object_id = ? ORDER BY created_at", [object_kind, object_id])
        for r in rows:
            r["mentions"] = json.loads(r["mentions"])
            r["author_name"] = self._name(r["author"])
        return rows

    def resolve(self, actor: Principal, ws: str, comment_id: str, *, note: str | None = None) -> dict[str, Any]:
        rows = self._db(ws).query("SELECT * FROM comments WHERE id = ?", [comment_id])
        if not rows:
            raise KeyError(comment_id)
        c = rows[0]
        obj = self._object(actor, ws, c["object_kind"], c["object_id"])
        if c["kind"] != "change_request" or c["status"] != "open":
            raise ValueError("only an open change request can be resolved")
        if actor.user_id not in (c["author"], obj["owner"]) and not self.guard.can(actor, ws, "workspace.admin"):
            raise AccessDenied("the requester, the owner or an admin resolves a change request")
        self._db(ws).execute("UPDATE comments SET status = 'resolved', resolved_by = ?, resolved_at = now() WHERE id = ?", [actor.user_id, comment_id])
        if note:
            self.comment(actor, ws, c["object_kind"], c["object_id"], note, parent_id=comment_id)
        self.notify(ws, c["author"], "review_decided", f"{self._name(actor.user_id)} resolved your change request on \"{obj['title'][:80]}\"",
                    object_kind=c["object_kind"], object_id=c["object_id"], actor=actor)
        self.guard.audit(actor, ws, "comment.resolve", comment_id, "ok")
        return {"id": comment_id, "status": "resolved"}

    def decisions(self, actor: Principal, ws: str, *, object_kind: str | None = None, object_id: str | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
        """Recorded decisions, newest first, across the workspace or for one object."""
        self.guard.require(actor, ws, "workspace.read")
        sql, params = "SELECT * FROM comments WHERE kind = 'decision'", []
        if object_kind and object_id:
            sql += " AND object_kind = ? AND object_id = ?"
            params = [object_kind, object_id]
        out = []
        for r in self._db(ws).query(sql + " ORDER BY created_at DESC", params):
            try:
                self._object(actor, ws, r["object_kind"], r["object_id"])
            except (KeyError, AccessDenied):
                continue
            r["mentions"] = json.loads(r["mentions"])
            out.append(r)
            if len(out) >= max(1, min(int(limit), 1000)):
                break
        return out

    # -- assignments ----------------------------------------------------------------------------

    def assign(self, actor: Principal, ws: str, object_kind: str, object_id: str, assignee: str, *, note: str = "",
               due: str | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "task.control", f"{object_kind}:{object_id}")
        obj = self._object(actor, ws, object_kind, object_id)
        if not self._member(ws, assignee):
            raise ValueError("the assignee is not a member of this workspace")
        aid = "asg_" + uuid.uuid4().hex[:10]
        self._db(ws).execute("INSERT INTO assignments (id, object_kind, object_id, assignee, assigned_by, note, due, status) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')", [aid, object_kind, object_id, assignee, actor.user_id, note, due])
        self.notify(ws, assignee, "assignment", f"{self._name(actor.user_id)} assigned you {object_kind} \"{obj['title'][:80]}\""
                    + (f": {note[:160]}" if note else "") + (f" (due {due})" if due else ""), object_kind=object_kind, object_id=object_id, actor=actor)
        self.guard.audit(actor, ws, "assignment.create", aid, "ok", {"object": f"{object_kind}:{object_id}", "assignee": assignee})
        return {"id": aid, "status": "open", "assignee": assignee}

    def assignments(self, actor: Principal, ws: str, *, mine: bool = False, status: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        sql, params = "SELECT * FROM assignments WHERE 1 = 1", []
        if mine:
            sql += " AND assignee = ?"
            params.append(actor.user_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        return self._db(ws).query(sql + " ORDER BY status, due NULLS LAST, created_at DESC", params)

    def complete(self, actor: Principal, ws: str, assignment_id: str, *, note: str = "") -> dict[str, Any]:
        rows = self._db(ws).query("SELECT * FROM assignments WHERE id = ?", [assignment_id])
        if not rows:
            raise KeyError(assignment_id)
        a = rows[0]
        self.guard.require(actor, ws, "workspace.read")
        if actor.user_id not in (a["assignee"], a["assigned_by"]) and not self.guard.can(actor, ws, "workspace.admin"):
            raise AccessDenied("the assignee, the person who assigned it or an admin completes an assignment")
        if a["status"] != "open":
            raise ValueError("this assignment is already closed")
        self._db(ws).execute("UPDATE assignments SET status = 'done', completed_at = now() WHERE id = ?", [assignment_id])
        obj = self._object(actor, ws, a["object_kind"], a["object_id"])
        if note:
            self.comment(actor, ws, a["object_kind"], a["object_id"], note)
        self.notify(ws, a["assigned_by"], "assignment_done", f"{self._name(actor.user_id)} finished {a['object_kind']} \"{obj['title'][:80]}\""
                    + (f": {note[:160]}" if note else ""), object_kind=a["object_kind"], object_id=a["object_id"], actor=actor)
        self.guard.audit(actor, ws, "assignment.complete", assignment_id, "ok")
        return {"id": assignment_id, "status": "done"}

    # -- reviews --------------------------------------------------------------------------------

    def propose(self, actor: Principal, ws: str, action: str, *, object_kind: str, object_id: str, payload: dict[str, Any],
                summary: str, reviewer: str | None = None) -> dict[str, Any]:
        """Ask for a change that needs an admin (or, for ``look``, for someone to look at an object). Nothing changes
        until it is approved."""
        if action not in REVIEW_ACTIONS:
            raise ValueError(f"action must be one of {', '.join(REVIEW_ACTIONS)}")
        self.guard.require(actor, ws, "task.control" if action != "look" else "workspace.read", f"{object_kind}:{object_id}")
        min_role = "viewer" if action == "look" else "admin"
        if action == "metric.define":
            from .semantic import MetricSpec  # noqa: PLC0415

            MetricSpec.model_validate(payload)  # a malformed definition is refused now, not at approval
        elif action == "contract.define":
            from .contracts import ContractSpec  # noqa: PLC0415

            ContractSpec.model_validate(payload)
            self._object(actor, ws, "dataset", object_id)
        elif action == "glossary.map":
            # "always read this term as that metric": the target must be an approved metric now, not at approval
            if not str(payload.get("term") or "").strip() or not payload.get("maps_to_metric"):
                raise ValueError("a glossary mapping names a term and the metric it means")
            self._object(actor, ws, "metric", payload["maps_to_metric"])
        else:
            self._object(actor, ws, object_kind, object_id)
        if reviewer and not self._member(ws, reviewer):
            raise ValueError("the reviewer is not a member of this workspace")
        if action == "look" and not reviewer:
            raise ValueError("say who should look")
        rid = "rv_" + uuid.uuid4().hex[:10]
        self._db(ws).execute("INSERT INTO reviews (id, object_kind, object_id, action, payload, summary, requested_by, reviewer, min_role, status) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                             [rid, object_kind, object_id, action, json.dumps(payload, default=str), summary[:300], actor.user_id, reviewer, min_role])
        targets = [reviewer] if reviewer else [r["user_id"] for r in self.stores.control.query(
            "SELECT user_id FROM memberships WHERE workspace_id = ? AND role IN ('owner', 'admin')", [ws])]
        for uid in targets:
            self.notify(ws, uid, "review_requested", f"{self._name(actor.user_id)} asks for your review: {summary[:200]}",
                        object_kind=object_kind, object_id=object_id, actor=actor)
        self.guard.audit(actor, ws, "review.request", rid, "ok", {"action": action, "object": f"{object_kind}:{object_id}"})
        return {"id": rid, "status": "pending", "action": action}

    def reviews(self, actor: Principal, ws: str, *, status: str | None = None, for_me: bool = False,
                limit: int = 200) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query("SELECT * FROM reviews" + (" WHERE status = ?" if status else "") + " ORDER BY created_at DESC",
                                  [status] if status else [])
        role = self.guard.role(actor, ws) or "viewer"
        out = []
        for r in rows:
            r["payload"] = json.loads(r["payload"])
            r["can_decide"] = (r["status"] == "pending" and r["requested_by"] != actor.user_id and RANK[role] >= RANK[r["min_role"]]
                               and (not r["reviewer"] or r["reviewer"] == actor.user_id or r["min_role"] == "admin"))
            if for_me and not r["can_decide"]:
                continue
            out.append(r)
            if len(out) >= max(1, min(int(limit), 1000)):
                break
        return out

    def decide(self, actor: Principal, ws: str, review_id: str, *, approve: bool, note: str = "") -> dict[str, Any]:
        rows = self._db(ws).query("SELECT * FROM reviews WHERE id = ?", [review_id])
        if not rows:
            raise KeyError(review_id)
        r = rows[0]
        if r["status"] != "pending":
            raise ValueError(f"this review is already {r['status']}")
        if r["requested_by"] == actor.user_id:
            raise AccessDenied("a change is approved by someone other than the person who proposed it")
        role = self.guard.role(actor, ws)
        if not role or RANK[role] < RANK[r["min_role"]]:
            raise AccessDenied(f"deciding this review needs the {r['min_role']} role")
        if r["action"] == "look" and r["reviewer"] and r["reviewer"] != actor.user_id:
            raise AccessDenied("this review was asked of someone else")
        payload = json.loads(r["payload"])
        applied: dict[str, Any] | None = None
        if approve:
            if r["action"] == "metric.define":
                from .semantic import MetricSpec  # noqa: PLC0415

                version = self.semantic.define(actor, ws, MetricSpec.model_validate(payload), note=f"approved review {review_id}: {r['summary']}")
                applied = {"metric": payload["name"], "version": version}
            elif r["action"] == "contract.define":
                from .contracts import ContractSpec  # noqa: PLC0415

                out = self.contracts.define(actor, ws, r["object_id"], ContractSpec.model_validate(payload), note=f"approved review {review_id}")
                applied = {"dataset": r["object_id"], "version": out["version"], "check": out["check"]["outcome"]}
            elif r["action"] == "glossary.map":
                self.semantic.set_term(actor, ws, payload["term"], payload.get("definition") or "",
                                       payload["maps_to_metric"], source=f"review {review_id}")
                applied = {"term": payload["term"].lower().strip(), "maps_to_metric": payload["maps_to_metric"]}
            elif r["action"] == "asset.publish":
                self._db(ws).execute("UPDATE assets SET visibility = 'workspace', updated_at = now() WHERE id = ?", [r["object_id"]])
                applied = {"asset": r["object_id"], "visibility": "workspace"}
        status = "approved" if approve else "changes_requested"
        self._db(ws).execute("UPDATE reviews SET status = ?, decided_by = ?, decision_note = ?, decided_at = now() WHERE id = ?",
                             [status, actor.user_id, note, review_id])
        verdict = "approved" if approve else "asked for changes on"
        self.notify(ws, r["requested_by"], "review_decided", f"{self._name(actor.user_id)} {verdict} \"{r['summary'][:160]}\""
                    + (f": {note[:160]}" if note else ""), object_kind=r["object_kind"], object_id=r["object_id"], actor=actor)
        self.guard.audit(actor, ws, "review.decide", review_id, status, {"action": r["action"], "applied": applied})
        return {"id": review_id, "status": status, "applied": applied}

    # -- sharing and ownership ------------------------------------------------------------------

    def share(self, actor: Principal, ws: str, asset_id: str, *, visibility: str | None = None, users: list[str] | None = None) -> dict[str, Any]:
        """Owner or admin: make an asset private, share it with named members, or (owners and admins) show it to the
        workspace. An editor who is not the owner proposes publication through a review instead."""
        a = self.assets.get(actor, ws, asset_id)
        is_admin = self.guard.can(actor, ws, "workspace.admin")
        if a["owner"] != actor.user_id and not is_admin:
            raise AccessDenied("only the owner or an admin shares an asset")
        db = self._db(ws)
        if visibility:
            if visibility not in ("private", "workspace"):
                raise ValueError("visibility is private or workspace")
            db.execute("UPDATE assets SET visibility = ?, updated_at = now() WHERE id = ?", [visibility, asset_id])
        for uid in users or []:
            if not self._member(ws, uid):
                raise ValueError(f"{uid} is not a member of this workspace")
            db.execute("INSERT OR REPLACE INTO asset_shares (asset_id, user_id, shared_by) VALUES (?, ?, ?)", [asset_id, uid, actor.user_id])
            self.notify(ws, uid, "assignment", f"{self._name(actor.user_id)} shared \"{a['title'][:80]}\" with you", object_kind="asset",
                        object_id=asset_id, actor=actor)
        self.guard.audit(actor, ws, "asset.share", asset_id, "ok", {"visibility": visibility, "users": len(users or [])})
        return {"asset_id": asset_id, "visibility": visibility or a["visibility"],
                "shared_with": [r["user_id"] for r in db.query("SELECT user_id FROM asset_shares WHERE asset_id = ?", [asset_id])]}

    def transfer(self, actor: Principal, ws: str, object_kind: str, object_id: str, to_user: str, *, note: str = "") -> dict[str, Any]:
        """Hand ownership of an asset, dataset or metric to another member with at least the editor role."""
        if object_kind not in ("asset", "dataset", "metric"):
            raise ValueError("ownership transfers apply to assets, datasets and metrics")
        obj = self._object(actor, ws, object_kind, object_id)
        if obj["owner"] != actor.user_id and not self.guard.can(actor, ws, "workspace.admin"):
            raise AccessDenied("only the owner or an admin transfers ownership")
        role = self._member(ws, to_user)
        if not role or RANK[role] < RANK["editor"]:
            raise ValueError("the new owner must be a member with at least the editor role")
        table, key = {"asset": ("assets", "id"), "dataset": ("datasets", "name"), "metric": ("metrics", "name")}[object_kind]
        self._db(ws).execute(f"UPDATE {table} SET owner = ? WHERE {key} = ?", [to_user, object_id])
        self.notify(ws, to_user, "assignment", f"{self._name(actor.user_id)} made you the owner of {object_kind} \"{obj['title'][:80]}\""
                    + (f": {note[:160]}" if note else ""), object_kind=object_kind, object_id=object_id, actor=actor)
        self.guard.audit(actor, ws, "ownership.transfer", f"{object_kind}:{object_id}", "ok", {"from": obj["owner"], "to": to_user})
        return {"object_kind": object_kind, "object_id": object_id, "owner": to_user, "previous_owner": obj["owner"]}

    # -- one object's collaboration view ---------------------------------------------------------

    def activity(self, actor: Principal, ws: str, object_kind: str, object_id: str) -> dict[str, Any]:
        obj = self._object(actor, ws, object_kind, object_id)
        db = self._db(ws)
        reviews = db.query("SELECT id, action, summary, status, requested_by, reviewer, decided_by, decision_note, created_at, decided_at "
                           "FROM reviews WHERE object_kind = ? AND object_id = ? ORDER BY created_at DESC", [object_kind, object_id])
        return {"object_kind": object_kind, "object_id": object_id, "title": obj["title"], "owner": obj["owner"],
                "owner_name": self._name(obj["owner"]), "comments": self.comments(actor, ws, object_kind, object_id),
                "open_change_requests": db.scalar("SELECT count(*) FROM comments WHERE object_kind = ? AND object_id = ? AND kind = 'change_request' "
                                                  "AND status = 'open'", [object_kind, object_id]),
                "assignments": db.query("SELECT * FROM assignments WHERE object_kind = ? AND object_id = ? ORDER BY created_at DESC", [object_kind, object_id]),
                "reviews": reviews}


__all__ = ["COMMENT_KINDS", "NOTIFICATION_KINDS", "OBJECT_KINDS", "REVIEW_ACTIONS", "CollaborationService"]
