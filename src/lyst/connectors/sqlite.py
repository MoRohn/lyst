# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""SQLite connector: a database file under the import root, opened read-only (``mode=ro``)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa

from . import Connector, ConnectorError, Field, Manifest, SourceTable, register, validate_identifier
from .files import import_root


@register
class SQLiteConnector(Connector):
    manifest = Manifest(kind="sqlite", label="SQLite file",
                        fields=(Field("path", "Database file (inside the import root)"),),
                        notes="Opened read-only. Tables and views are discovered; a cursor column (an integer id or a timestamp) "
                              "makes later syncs incremental.")

    def _path(self) -> Path:
        root = import_root()
        p = Path(self.config["path"]).expanduser()
        p = (root / p).resolve() if not p.is_absolute() else p.resolve()
        if root not in p.parents:
            raise ConnectorError(f"{p} is outside the import root {root}")
        if not p.is_file():
            raise ConnectorError(f"{p} is not a file")
        return p

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self._path()}?mode=ro", uri=True, timeout=10)

    def test(self) -> dict[str, Any]:
        with self._connect() as c:
            n = c.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view')").fetchone()[0]
            version = c.execute("SELECT sqlite_version()").fetchone()[0]
        return {"ok": True, "sqlite": version, "tables_visible": n, "read_only": True}

    def discover(self) -> list[SourceTable]:
        out = []
        with self._connect() as c:
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                cols = [{"name": r[1], "type": r[2] or "any"} for r in c.execute(f'PRAGMA table_info("{name}")')]
                out.append(SourceTable("", name, cols))
        return out

    def read(self, table: str, *, cursor_column: str | None = None, after: Any = None,
             batch_rows: int = 50_000) -> Iterator[pa.Table]:
        validate_identifier(table, "table")
        sql, params = f'SELECT * FROM "{table}"', ()
        if cursor_column:
            validate_identifier(cursor_column, "cursor column")
            if after is not None:
                sql += f' WHERE "{cursor_column}" > ?'
                params = (after,)
            sql += f' ORDER BY "{cursor_column}"'
        with self._connect() as c:
            cur = c.execute(sql, params)
            names = [d[0] for d in cur.description]
            while True:
                rows = cur.fetchmany(batch_rows)
                if not rows:
                    break
                yield pa.table({n: [r[i] for r in rows] for i, n in enumerate(names)})
