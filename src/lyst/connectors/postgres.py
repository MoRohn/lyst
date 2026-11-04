# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""PostgreSQL connector: psycopg 3, read-only transactions, a statement timeout, server-side cursors."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa

from . import Connector, ConnectorError, Field, Manifest, SourceTable, register, validate_identifier


def _q(ident: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in ident.split("."))


@register
class PostgresConnector(Connector):
    manifest = Manifest(
        kind="postgres", label="PostgreSQL", auth="password",
        fields=(Field("host", "Host"), Field("port", "Port", default="5432"), Field("dbname", "Database"),
                Field("user", "User"), Field("password", "Password", secret=True, required=False),
                Field("sslmode", "SSL mode", required=False, default="prefer"),
                Field("schemas", "Schemas to discover (comma list)", required=False, default="public")),
        notes="Use a role with SELECT only. Every transaction is opened READ ONLY with a 60 s statement timeout; "
              "Lyst never writes to the source.")

    def _connect(self):
        import psycopg  # noqa: PLC0415

        try:
            conn = psycopg.connect(host=self.config["host"], port=int(self.config.get("port") or 5432),
                                   dbname=self.config["dbname"], user=self.config["user"],
                                   password=self.secrets.get("password") or None, sslmode=self.config.get("sslmode") or "prefer",
                                   connect_timeout=10, application_name="lyst")
        except Exception as exc:
            raise ConnectorError(f"could not connect: {exc}") from exc
        conn.read_only = True
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60s'")
        return conn

    def test(self) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT version(), current_user, current_setting('transaction_read_only')")
            version, user, ro = cur.fetchone()
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = ANY(%s)", (self._schemas(),))
            tables = cur.fetchone()[0]
        return {"ok": True, "server": version.split(",")[0], "user": user, "read_only_transactions": ro == "on",
                "tables_visible": tables}

    def _schemas(self) -> list[str]:
        return [s.strip() for s in (self.config.get("schemas") or "public").split(",") if s.strip()]

    def discover(self) -> list[SourceTable]:
        out: list[SourceTable] = []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT c.table_schema, c.table_name, c.column_name, c.data_type FROM information_schema.columns c "
                "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                "WHERE c.table_schema = ANY(%s) AND t.table_type IN ('BASE TABLE', 'VIEW') "
                "ORDER BY c.table_schema, c.table_name, c.ordinal_position", (self._schemas(),))
            tables: dict[tuple[str, str], list[dict[str, str]]] = {}
            for schema, name, col, dtype in cur.fetchall():
                tables.setdefault((schema, name), []).append({"name": col, "type": dtype})
            cur.execute("SELECT schemaname, relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname = ANY(%s)", (self._schemas(),))
            est = {(s, n): int(r) for s, n, r in cur.fetchall()}
        for (schema, name), cols in tables.items():
            out.append(SourceTable(schema, name, cols, est.get((schema, name))))
        return out

    def read(self, table: str, *, cursor_column: str | None = None, after: Any = None,
             batch_rows: int = 50_000) -> Iterator[pa.Table]:
        validate_identifier(table, "table")
        sql = f"SELECT * FROM {_q(table)}"
        params: tuple = ()
        if cursor_column:
            validate_identifier(cursor_column, "cursor column")
            if after is not None:
                sql += f" WHERE {_q(cursor_column)} > %s"
                params = (after,)
            sql += f" ORDER BY {_q(cursor_column)}"
        with self._connect() as conn, conn.cursor(name="lyst_read") as cur:
            cur.itersize = batch_rows
            cur.execute(sql, params)
            names = None
            while True:
                rows = cur.fetchmany(batch_rows)
                if not rows:
                    break
                names = names or [d.name for d in cur.description]
                yield pa.table({n: [r[i] for r in rows] for i, n in enumerate(names)})
