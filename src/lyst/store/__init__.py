# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workspace-scoped persistence on DuckDB."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path

from ..config import Settings
from .db import Database
from .schema import CONTROL, WORKSPACE

_WS_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
log = logging.getLogger("lyst.store")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
        self._opening: dict[str, threading.Lock] = {}

    def workspace(self, workspace_id: str) -> Database:
        if not _WS_ID.match(workspace_id):
            raise ValueError("invalid workspace id")
        with self._lock:  # held only for dict lookups, never while a file is opened or migrated
            db = self._workspaces.get(workspace_id)
            if db is not None:
                return db
            opening = self._opening.setdefault(workspace_id, threading.Lock())
        with opening:  # one opener per workspace; others wait here, not on every workspace
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
                with self._lock:
                    self._workspaces[workspace_id] = db
                    self._opening.pop(workspace_id, None)
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
        """Back up every database (consistent, encrypted with the same derived keys) plus artifacts and dashboards.

        A new destination is built under a hidden sibling and renamed into place only when complete, so a failure
        never leaves a half-written backup that looks like a real one. An existing, non-empty destination is
        written in place; on failure it gets a BACKUP_INCOMPLETE note and no manifest."""
        import json
        import shutil
        from datetime import UTC, datetime

        dest = Path(dest)
        fresh = not dest.exists() or (dest.is_dir() and not any(dest.iterdir()))
        work = dest.with_name(f".partial-{dest.name}") if fresh else dest
        if fresh:
            shutil.rmtree(work, ignore_errors=True)
        try:
            (work / "workspaces").mkdir(parents=True, exist_ok=True)
            files = [self.control.backup_to(work / "control.duckdb")]
            for w in self.control.query("SELECT id FROM workspaces"):
                files.append(self.workspace(w["id"]).backup_to(work / "workspaces" / f"{w['id']}.duckdb"))
            for folder in ("artifacts", "dashboards"):
                src = self.settings.home / folder
                if src.exists():
                    shutil.copytree(src, work / folder, dirs_exist_ok=True)
            manifest = {"taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
                        "encrypted": self.settings.encrypt,
                        "files": {str(f.relative_to(work)): _sha256(f) for f in files},
                        "note": "Database files are encrypted with keys derived from the same master key; keep "
                                "LYST_MASTER_KEY (or the keyfile) with this backup or it is unreadable."}
            (work / "manifest.json").write_text(json.dumps(manifest, indent=1))
            if fresh:
                if dest.exists():
                    dest.rmdir()  # the empty directory the caller made
                work.rename(dest)
        except BaseException as exc:
            if fresh:
                shutil.rmtree(work, ignore_errors=True)
            else:
                (work / "manifest.json").unlink(missing_ok=True)
                try:
                    (work / "BACKUP_INCOMPLETE").write_text(f"{type(exc).__name__}: {exc}\n")
                except OSError:
                    pass
            raise
        return manifest

    def database_files(self) -> list[tuple[str, Path]]:
        """(database name, path) for every workspace-shaped database on disk, opened or not. Files whose stem is not a
        valid workspace id (copies, strays) are skipped with a warning rather than breaking key rotation."""
        out = [("control", self.settings.control_db)]
        wsdir = self.settings.home / "workspaces"
        if wsdir.exists():
            for p in sorted(wsdir.glob("*.duckdb")):
                if _WS_ID.match(p.stem) and p == self.settings.workspace_db(p.stem):
                    out.append((f"ws:{p.stem}", p))
                else:
                    log.warning("skipping %s: not a workspace database file", p.name)
        return out

    def rekey_copies(self, new_master: bytes, staging: Path) -> list[tuple[Path, Path]]:
        """Copy every database into ``staging`` sealed with keys derived from ``new_master``. Nothing in place changes;
        the caller swaps the files once every copy (and the secret re-wrap) has succeeded. Returns (live, copy) pairs."""
        from .db import derive_key

        staging.mkdir(parents=True, exist_ok=True)
        pairs = []
        for name, path in self.database_files():
            db = self.control if name == "control" else self.workspace(name.removeprefix("ws:"))
            assert db.name == name, f"key name mismatch for {path}"  # the key is derived from the database's own name
            target = staging / path.relative_to(self.settings.home)
            db.copy_with_key(target, derive_key(new_master, name) if self.settings.encrypt else None)
            pairs.append((path, target))
        return pairs

    def prune(self, *, event_days: int = 90, audit_days: int = 400, memory_days: int | None = None) -> dict[str, int]:
        """Retention: drop old operational rows; audit lines live longer. Returns rows removed per table."""
        removed: dict[str, int] = {}

        def count_then_delete(db, table: str, column: str, days: int) -> int:
            # one transaction, one cutoff: the count is exactly the rows this delete removed
            def tx(c) -> int:
                cutoff = c.execute("SELECT now() - to_days(CAST(? AS BIGINT))", [days]).fetchone()[0]
                n = c.execute(f"SELECT count(*) FROM {table} WHERE {column} < ?", [cutoff]).fetchone()[0] or 0
                if n:
                    c.execute(f"DELETE FROM {table} WHERE {column} < ?", [cutoff])
                return int(n)
            return db.write(tx)

        removed["task_events"] = count_then_delete(self.control, "task_events", "occurred_at", event_days)
        removed["llm_calls"] = count_then_delete(self.control, "llm_calls", "occurred_at", event_days)
        removed["audit_log"] = count_then_delete(self.control, "audit_log", "occurred_at", audit_days)
        for w in self.control.query("SELECT id FROM workspaces"):
            db = self.workspace(w["id"])
            removed["query_history"] = removed.get("query_history", 0) + count_then_delete(db, "query_history", "occurred_at", event_days)
            removed["notifications"] = removed.get("notifications", 0) + count_then_delete(db, "notifications", "created_at", event_days)
            if memory_days:
                # retention for agent-observed memory only: approved or person-validated knowledge is never aged out
                def drop_memory(c, days=memory_days) -> int:
                    # one transaction and one cutoff; the ids stay in the database (no IN list built in Python)
                    cutoff = c.execute("SELECT now() - to_days(CAST(? AS BIGINT))", [days]).fetchone()[0]
                    expired = ("SELECT id FROM memory WHERE trust = 'observed' AND layer IN ('episodic', 'task') "
                               "AND created_at < ?")
                    n = c.execute(f"SELECT count(*) FROM ({expired})", [cutoff]).fetchone()[0] or 0
                    if n:
                        c.execute(f"DELETE FROM memory_vectors WHERE memory_id IN ({expired})", [cutoff])
                        c.execute(f"DELETE FROM memory WHERE id IN ({expired})", [cutoff])
                    return int(n)
                removed["memory"] = removed.get("memory", 0) + db.write(drop_memory)
        return removed

    def close(self) -> None:
        """Close every database; one failure does not leave the others (or the control store) open."""
        with self._lock:
            dbs = list(self._workspaces.values())
            self._workspaces.clear()
        for db in [*dbs, self.control]:
            try:
                db.close()
            except Exception:  # noqa: BLE001 - best effort, keep closing the rest
                log.exception("closing database %s failed", db.name)


__all__ = ["Database", "Stores"]
