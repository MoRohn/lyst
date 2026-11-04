# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workspace-scoped persistence on DuckDB."""

from __future__ import annotations

import re
import threading

from ..config import Settings
from .db import Database
from .schema import CONTROL, WORKSPACE

_WS_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class Stores:
    """The control database plus one database file per workspace, opened on first use."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.control = Database(
            settings.control_db,
            name="control",
            master_key=settings.master_key,
            encrypt=settings.encrypt,
            migrations=CONTROL,
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
                )
                self._workspaces[workspace_id] = db
            return db

    def close(self) -> None:
        with self._lock:
            for db in self._workspaces.values():
                db.close()
            self._workspaces.clear()
        self.control.close()


__all__ = ["Database", "Stores"]
