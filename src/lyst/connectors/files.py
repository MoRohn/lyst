# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Files connector: CSV and Parquet files in one directory on the engine host (a drop folder, a mounted
share). The directory must be inside ``LYST_IMPORT_ROOT`` (default ``$LYST_HOME/imports``) so a
connection cannot point Lyst at arbitrary paths."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from . import Connector, ConnectorError, Field, Manifest, SourceTable, register


def import_root() -> Path:
    return Path(os.environ.get("LYST_IMPORT_ROOT") or (Path(os.environ.get("LYST_HOME", "var")) / "imports")).resolve()


@register
class FilesConnector(Connector):
    manifest = Manifest(kind="files", label="Files on the engine host",
                        fields=(Field("path", "Directory (inside the import root)"),),
                        capabilities=("test", "discover", "read"),
                        notes="CSV and Parquet files in a directory under LYST_IMPORT_ROOT. Each file is a table; a sync "
                              "reloads the whole file (no incremental mode).")

    def _dir(self) -> Path:
        root = import_root()
        p = Path(self.config["path"]).expanduser()
        p = (root / p).resolve() if not p.is_absolute() else p.resolve()
        if root != p and root not in p.parents:
            raise ConnectorError(f"{p} is outside the import root {root}")
        if not p.is_dir():
            raise ConnectorError(f"{p} is not a directory")
        return p

    def test(self) -> dict[str, Any]:
        d = self._dir()
        files = [f for f in d.iterdir() if f.suffix.lower() in (".csv", ".parquet", ".tsv")]
        return {"ok": True, "directory": str(d), "files": len(files)}

    def discover(self) -> list[SourceTable]:
        out = []
        for f in sorted(self._dir().iterdir()):
            if f.suffix.lower() not in (".csv", ".parquet", ".tsv"):
                continue
            if f.suffix.lower() == ".parquet":
                schema = pq.read_schema(f)
                cols = [{"name": c.name, "type": str(c.type)} for c in schema]
                rows = pq.read_metadata(f).num_rows
            else:
                head = pacsv.read_csv(f, read_options=pacsv.ReadOptions(block_size=1 << 16),
                                      parse_options=pacsv.ParseOptions(delimiter="\t" if f.suffix.lower() == ".tsv" else ","))
                cols = [{"name": c.name, "type": str(c.type)} for c in head.schema]
                rows = None
            out.append(SourceTable("", f.name, cols, rows))
        return out

    def read(self, table: str, *, cursor_column: str | None = None, after: Any = None,
             batch_rows: int = 50_000) -> Iterator[pa.Table]:
        f = self._dir() / Path(table).name
        if not f.exists():
            raise ConnectorError(f"no file named {table}")
        if f.suffix.lower() == ".parquet":
            pf = pq.ParquetFile(f)
            for batch in pf.iter_batches(batch_size=batch_rows):
                yield pa.Table.from_batches([batch])
        else:
            yield pacsv.read_csv(f, parse_options=pacsv.ParseOptions(delimiter="\t" if f.suffix.lower() == ".tsv" else ","))
