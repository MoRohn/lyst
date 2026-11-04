# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Platform notices: a finding in the inbox for something the workspace's owners should act on, raised by a
platform service rather than a metric monitor (a broken data contract, a connection that needs attention).

They use the same ``findings`` and ``notifications`` tables as monitors, so the inbox, the API and the webhook-free
acknowledge flow are shared. ``dedupe_key`` keeps a repeated condition from notifying twice.
"""

from __future__ import annotations

import uuid

from .store import Stores


def raise_notice(stores: Stores, ws: str, *, source: str, kind: str, label: str, summary: str, priority: float,
                 dedupe_key: str, metric: str = "", period: str = "", recipients: set[str] | None = None) -> str | None:
    """Record a finding and an inbox notification for the workspace's owners and admins plus ``recipients``.
    Returns the finding id, or None when the same condition was already raised."""
    db = stores.workspace(ws)
    if db.scalar("SELECT count(*) FROM findings WHERE dedupe_key = ?", [dedupe_key]):
        return None
    fid = "f_" + uuid.uuid4().hex[:10]
    db.execute("INSERT INTO findings (id, monitor_id, kind, period, label, metric, summary, priority, direction, z, value, baseline, task_id, "
               "status, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'down', NULL, NULL, NULL, NULL, 'new', ?)",
               [fid, source, kind, period, label, metric, summary, priority, dedupe_key])
    rows = stores.control.query("SELECT user_id FROM memberships WHERE workspace_id = ? AND role IN ('owner', 'admin')", [ws])
    for uid in {r["user_id"] for r in rows} | {r for r in (recipients or set()) if r}:
        db.execute("INSERT INTO notifications (id, user_id, finding_id, channel, status) VALUES (?, ?, ?, 'inbox', 'unread')",
                   ["n_" + uuid.uuid4().hex[:10], uid, fid])
    return fid


__all__ = ["raise_notice"]
