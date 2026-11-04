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
import queue
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import duckdb

T = TypeVar("T")

Migration = tuple[int, str]


def derive_key(master: bytes, name: str) -> str:
    return hmac.new(master, f"lyst:db:{name}".encode(), hashlib.sha256).hexdigest()


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
        quoted = str(path).replace("'", "''")
        if encrypt:
            key = derive_key(master_key, name)
            self._conn.execute(f"ATTACH '{quoted}' AS {self.ALIAS} (ENCRYPTION_KEY '{key}')")
        else:
            self._conn.execute(f"ATTACH '{quoted}' AS {self.ALIAS}")
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

    def write(self, fn: Callable[[duckdb.DuckDBPyConnection], T], timeout: float = 120) -> T:
        """Run ``fn`` in one transaction on the writer thread and return its result."""
        if self._closed:
            raise RuntimeError(f"database {self.name} is closed")
        fut: Future = Future()
        self._queue.put((fn, fut))
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            # Withdraw the write if it has not started, so a caller told "timed out" never sees it applied later.
            # If it already started it runs to its own commit or rollback; wait for that outcome rather than lie.
            if fut.cancel():
                raise TimeoutError(f"write to {self.name} not started within {timeout}s; withdrawn") from None
            return fut.result()

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

    def query(self, sql: str, params: list | tuple | None = None) -> list[dict[str, Any]]:
        with self.read() as cur:
            rel = cur.execute(sql, params or [])
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, row, strict=True)) for row in rel.fetchall()]

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
                for stmt in [s for s in sql.split(";") if s.strip()]:
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
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = self._backup_dir / f".{self.name.replace(':', '_')}.{dest.name}"
        for p in (staging, staging.with_name(staging.name + ".wal")):
            if p.exists():
                p.unlink()
        quoted = str(staging).replace("'", "''")

        def tx(c: duckdb.DuckDBPyConnection) -> None:
            if key:
                c.execute(f"ATTACH '{quoted}' AS lyst_backup (ENCRYPTION_KEY '{key}')")
            else:
                c.execute(f"ATTACH '{quoted}' AS lyst_backup")
            try:
                c.execute(f"COPY FROM DATABASE {self.ALIAS} TO lyst_backup")
            finally:
                c.execute("DETACH lyst_backup")

        # COPY FROM DATABASE cannot run inside the writer's BEGIN/COMMIT, so take the writer's cursor directly
        fut: Future = Future()
        self._queue.put((lambda c: (c.execute("COMMIT"), tx(c), c.execute("BEGIN TRANSACTION")) and None, fut))
        fut.result(timeout=600)
        if dest.exists():
            dest.unlink()
        import shutil  # noqa: PLC0415

        shutil.move(staging, dest)
        return dest

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._writer.join(timeout=30)
        self._conn.close()
