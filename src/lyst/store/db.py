# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""One DuckDB file, owned by this process, with serialized writes and parallel reads.

DuckDB allows one read-write process per file, and inside that process many cursors.
Lyst leans on exactly that and nothing more:

* The engine process (API + workers) is the only process that opens a database file.
  Streamlit and any other client go through the API. DuckDB's own file lock enforces this:
  a second process that tries to open the file fails instead of corrupting it.
* Every mutation goes through ``Database.write``, which runs on a single writer thread per
  file. Mutations are therefore serialized, each one in its own transaction, with no
  write-write conflicts to retry.
* Reads use ``Database.read``, which takes a fresh cursor. Cursors see the last committed
  state (MVCC snapshot) and run in parallel with the writer.
* Files are encrypted at rest with DuckDB's built-in AES-GCM database encryption. The key
  for each file is derived from the master key (HMAC-SHA256), so a stolen workspace file
  plus another workspace's key reveals nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import queue
import re
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import duckdb

T = TypeVar("T")

Migration = tuple[int, str]

log = logging.getLogger("lyst.store")
_HEX_KEY = re.compile(r"^[0-9a-f]{64}$")


def derive_key(master: bytes, name: str) -> str:
    return hmac.new(master, f"lyst:db:{name}".encode(), hashlib.sha256).hexdigest()


def _attach(c, path: Path | str, alias: str, key: str | None) -> None:
    """ATTACH a file, optionally encrypted. The key is checked to be a derived hex key (so it cannot break out of
    its literal), and a failure is re-raised without the statement text, which would carry the key."""
    quoted = str(path).replace("'", "''")
    if key is None:
        c.execute(f"ATTACH '{quoted}' AS {alias}")
        return
    if not _HEX_KEY.match(key):
        raise ValueError("an encryption key must be a 64-character hex digest")
    try:
        c.execute(f"ATTACH '{quoted}' AS {alias} (ENCRYPTION_KEY '{key}')")
    except duckdb.Error as exc:
        detail = str(exc).replace(key, "<key>")
        raise type(exc)(f"could not open {Path(path).name} with its encryption key: {detail}") from None


def split_statements(sql: str) -> list[str]:
    """Split a migration body on ';' outside string literals, quoted identifiers, comments and $$ bodies."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:  # doubled quote is an escaped quote
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i:j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            buf.append(sql[i:j])
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j])
            i = j
        elif sql.startswith("$$", i):
            j = sql.find("$$", i + 2)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j])
            i = j
        elif ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    out.append("".join(buf))
    return [s for s in out if s.strip() and not all(
        line.strip().startswith("--") or not line.strip() for line in s.splitlines())]


class Database:
    ALIAS = "lystdb"

    def __init__(
        self,
        path: Path,
        *,
        name: str,
        master_key: bytes,
        encrypt: bool = True,
        migrations: list[Migration] | None = None,
        allowed_dir: Path | None = None,
    ) -> None:
        self.path = path
        self.name = name
        self._key = derive_key(master_key, name) if encrypt else None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(":memory:")
        _attach(self._conn, path, self.ALIAS, self._key)
        self._conn.execute(f"USE {self.ALIAS}")
        # Generated or user SQL must never reach the filesystem or the network through this
        # connection. Lock the configuration so a query cannot turn it back on. The one directory
        # left open is Lyst's own backup folder, so a consistent copy can be attached there.
        self._backup_dir = allowed_dir
        if allowed_dir is not None:
            allowed_dir.mkdir(parents=True, exist_ok=True)
            self._conn.execute(f"SET allowed_directories = ['{str(allowed_dir).replace(chr(39), chr(39) * 2)}']")
        self._conn.execute("SET enable_external_access = false")
        self._conn.execute("SET lock_configuration = true")
        self._queue: queue.Queue[tuple[Callable[[duckdb.DuckDBPyConnection], Any], Future] | None] = (
            queue.Queue()
        )
        self._writer = threading.Thread(target=self._run_writer, name=f"writer:{name}", daemon=True)
        self._closed = False
        self._writer.start()
        if migrations:
            self.migrate(migrations)

    # -- writes -----------------------------------------------------------------------------

    def _run_writer(self) -> None:
        cur = self._conn.cursor()
        cur.execute(f"USE {self.ALIAS}")
        while True:
            item = self._queue.get()
            if item is None:
                cur.close()
                return
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                cur.execute("BEGIN TRANSACTION")
                result = fn(cur)
                cur.execute("COMMIT")
                fut.set_result(result)
            except BaseException as exc:  # noqa: BLE001 - surfaced to caller
                try:
                    cur.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                fut.set_exception(exc)

    def _submit(self, fn: Callable[[duckdb.DuckDBPyConnection], Any]) -> Future:
        """Queue ``fn`` for the writer thread (the one place the queue protocol lives)."""
        if self._closed:
            raise RuntimeError(f"database {self.name} is closed")
        fut: Future = Future()
        self._queue.put((fn, fut))
        return fut

    def write(self, fn: Callable[[duckdb.DuckDBPyConnection], T], timeout: float = 120,
              grace: float | None = None) -> T:
        """Run ``fn`` in one transaction on the writer thread and return its result."""
        fut = self._submit(fn)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            # Withdraw the write if it has not started, so a caller told "timed out" never sees it applied later.
            # If it already started it runs to its own commit or rollback; wait a bounded grace for that outcome.
            if fut.cancel():
                raise TimeoutError(f"write to {self.name} not started within {timeout}s; withdrawn") from None
            try:
                return fut.result(timeout=timeout if grace is None else grace)
            except TimeoutError:
                raise TimeoutError(f"write to {self.name} still running after {timeout}s plus the grace period; "
                                   "it may still commit") from None

    def execute(self, sql: str, params: list | tuple | None = None) -> None:
        self.write(lambda c: c.execute(sql, params or []))

    # -- reads ------------------------------------------------------------------------------

    @contextmanager
    def read(self) -> Iterator[duckdb.DuckDBPyConnection]:
        cur = self._conn.cursor()
        try:
            cur.execute(f"USE {self.ALIAS}")
            yield cur
        finally:
            cur.close()

    def query(self, sql: str, params: list | tuple | None = None, *,
              max_rows: int | None = None) -> list[dict[str, Any]]:
        """Rows as dicts. ``max_rows`` stops reading after that many rows (the rest is never fetched)."""
        with self.read() as cur:
            rel = cur.execute(sql, params or [])
            cols = [d[0] for d in rel.description]
            rows = rel.fetchall() if max_rows is None else rel.fetchmany(max(0, int(max_rows)))
            return [dict(zip(cols, row, strict=True)) for row in rows]

    def scalar(self, sql: str, params: list | tuple | None = None) -> Any:
        with self.read() as cur:
            row = cur.execute(sql, params or []).fetchone()
            return row[0] if row else None

    # -- schema -----------------------------------------------------------------------------

    def migrate(self, migrations: list[Migration]) -> int:
        def apply(c: duckdb.DuckDBPyConnection) -> int:
            c.execute(
                "CREATE TABLE IF NOT EXISTS _migrations (version INTEGER PRIMARY KEY, "
                "applied_at TIMESTAMPTZ DEFAULT now())"
            )
            done = {r[0] for r in c.execute("SELECT version FROM _migrations").fetchall()}
            applied = 0
            for version, sql in sorted(migrations):
                if version in done:
                    continue
                for stmt in split_statements(sql):
                    c.execute(stmt)
                c.execute("INSERT INTO _migrations (version) VALUES (?)", [version])
                applied += 1
            return applied

        return self.write(apply)

    def backup_to(self, dest: Path) -> Path:
        """A consistent, still-encrypted copy of this database, taken on the writer thread in one snapshot."""
        return self.copy_with_key(dest, self._key)

    def copy_with_key(self, dest: Path, key: str | None) -> Path:
        """A consistent copy sealed with ``key`` (None: plain). Master-key rotation copies with the new derived key."""
        if self._backup_dir is None:
            raise RuntimeError("this database was opened without a backup directory")
        if key is not None and not _HEX_KEY.match(key):
            raise ValueError("an encryption key must be a 64-character hex digest")
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = self._backup_dir / f".{self.name.replace(':', '_')}.{dest.name}"
        for p in (staging, staging.with_name(staging.name + ".wal")):
            if p.exists():
                p.unlink()

        def copy(c: duckdb.DuckDBPyConnection) -> None:
            # COPY FROM DATABASE cannot run inside the writer's BEGIN/COMMIT: step out of it, and always step back
            # in, so the writer's own COMMIT or ROLLBACK has a transaction to end.
            c.execute("COMMIT")
            try:
                _attach(c, staging, "lyst_backup", key)
                try:
                    c.execute(f"COPY FROM DATABASE {self.ALIAS} TO lyst_backup")
                finally:
                    c.execute("DETACH lyst_backup")
            finally:
                c.execute("BEGIN TRANSACTION")

        self._submit(copy).result(timeout=600)
        import shutil  # noqa: PLC0415

        # Land next to dest first, then swap atomically: the previous copy survives until the new one is in place.
        landing = dest.with_name(f".{dest.name}.incoming")
        shutil.move(staging, landing)
        os.replace(landing, dest)
        return dest

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._writer.join(timeout=30)
        if self._writer.is_alive():
            # A write is still running: closing the connection under it would abort it mid-transaction. Leave the
            # connection to the (daemon) writer and say so.
            log.error("database %s: the writer is still busy after 30s; its connection was left open", self.name)
            return
        self._conn.close()
