# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workspace-scoped persistence on DuckDB."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from ..config import Settings
from .db import Database
from .schema import CONTROL, WORKSPACE

_WS_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class Stores:
    """The control database plus one database file per workspace, opened on first use."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if settings.setup_required:
            from ..secrets.masterkey import MasterKeyMissing

            raise MasterKeyMissing("no master encryption key: the engine is in setup mode")
        self.control = Database(
            settings.control_db,
            name="control",
            master_key=settings.master_key,
            encrypt=settings.encrypt,
            migrations=CONTROL,
            allowed_dir=settings.home / "backups",
        )
        self._workspaces: dict[str, Database] = {}
        self._lock = threading.Lock()

    def workspace(self, workspace_id: str) -> Database:
        if not _WS_ID.match(workspace_id):
            raise ValueError("invalid workspace id")
        with self._lock:
            db = self._workspaces.get(workspace_id)
            if db is None:
                db = Database(
                    self.settings.workspace_db(workspace_id),
                    name=f"ws:{workspace_id}",
                    master_key=self.settings.master_key,
                    encrypt=self.settings.encrypt,
                    migrations=WORKSPACE,
                    allowed_dir=self.settings.home / "backups",
                )
                self._workspaces[workspace_id] = db
            return db

    def discard_workspace(self, workspace_id: str) -> None:
        """Close and delete a workspace database that was just created for a workspace whose creation failed."""
        with self._lock:
            db = self._workspaces.pop(workspace_id, None)
        if db is not None:
            db.close()
        path = self.settings.workspace_db(workspace_id)
        for p in (path, path.with_name(path.name + ".wal")):
            p.unlink(missing_ok=True)

    def backup(self, dest: Path) -> dict:
        """Back up every database (consistent, encrypted with the same derived keys) plus artifacts and dashboards."""
        import hashlib
        import json
        import shutil
        from datetime import UTC, datetime

        dest = Path(dest)
        (dest / "workspaces").mkdir(parents=True, exist_ok=True)
        files = [self.control.backup_to(dest / "control.duckdb")]
        for w in self.control.query("SELECT id FROM workspaces"):
            files.append(self.workspace(w["id"]).backup_to(dest / "workspaces" / f"{w['id']}.duckdb"))
        for folder in ("artifacts", "dashboards"):
            src = self.settings.home / folder
            if src.exists():
                shutil.copytree(src, dest / folder, dirs_exist_ok=True)
        manifest = {"taken_at": datetime.now(UTC).isoformat(timespec="seconds"), "encrypted": self.settings.encrypt,
                    "files": {str(f.relative_to(dest)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files},
                    "note": "Database files are encrypted with keys derived from the same master key; keep LYST_MASTER_KEY "
                            "(or the keyfile) with this backup or it is unreadable."}
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
        return manifest

    def database_files(self) -> list[tuple[str, Path]]:
        """(database name, path) for every database on disk, opened or not."""
        out = [("control", self.settings.control_db)]
        wsdir = self.settings.home / "workspaces"
        if wsdir.exists():
            out += [(f"ws:{p.stem}", p) for p in sorted(wsdir.glob("*.duckdb"))]
        return out

    def rekey_copies(self, new_master: bytes, staging: Path) -> list[tuple[Path, Path]]:
        """Copy every database into ``staging`` sealed with keys derived from ``new_master``. Nothing in place changes;
        the caller swaps the files once every copy (and the secret re-wrap) has succeeded. Returns (live, copy) pairs."""
        from .db import derive_key

        staging.mkdir(parents=True, exist_ok=True)
        pairs = []
        for name, path in self.database_files():
            db = self.control if name == "control" else self.workspace(name[3:])
            target = staging / path.relative_to(self.settings.home)
            db.copy_with_key(target, derive_key(new_master, name) if self.settings.encrypt else None)
            pairs.append((path, target))
        return pairs

    def prune(self, *, event_days: int = 90, audit_days: int = 400, memory_days: int | None = None) -> dict[str, int]:
        """Retention: drop old operational rows; audit lines live longer. Returns rows removed per table."""
        removed: dict[str, int] = {}

        def count_then_delete(db, table: str, column: str, days: int) -> int:
            n = db.scalar(f"SELECT count(*) FROM {table} WHERE {column} < now() - to_days(CAST(? AS BIGINT))", [days]) or 0
            if n:
                db.execute(f"DELETE FROM {table} WHERE {column} < now() - to_days(CAST(? AS BIGINT))", [days])
            return int(n)

        removed["task_events"] = count_then_delete(self.control, "task_events", "occurred_at", event_days)
        removed["llm_calls"] = count_then_delete(self.control, "llm_calls", "occurred_at", event_days)
        removed["audit_log"] = count_then_delete(self.control, "audit_log", "occurred_at", audit_days)
        for w in self.control.query("SELECT id FROM workspaces"):
            db = self.workspace(w["id"])
            removed["query_history"] = removed.get("query_history", 0) + count_then_delete(db, "query_history", "occurred_at", event_days)
            removed["notifications"] = removed.get("notifications", 0) + count_then_delete(db, "notifications", "created_at", event_days)
            if memory_days:
                # retention for agent-observed memory only: approved or person-validated knowledge is never aged out
                old = [r["id"] for r in db.query(
                    "SELECT id FROM memory WHERE trust = 'observed' AND layer IN ('episodic', 'task') "
                    "AND created_at < now() - to_days(CAST(? AS BIGINT))", [memory_days])]
                if old:
                    marks = ", ".join("?" * len(old))
                    db.execute(f"DELETE FROM memory_vectors WHERE memory_id IN ({marks})", old)
                    db.execute(f"DELETE FROM memory WHERE id IN ({marks})", old)
                removed["memory"] = removed.get("memory", 0) + len(old)
        return removed

    def close(self) -> None:
        with self._lock:
            for db in self._workspaces.values():
                db.close()
            self._workspaces.clear()
        self.control.close()


__all__ = ["Database", "Stores"]
