# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Read-only SQL guard. Ports the idea of Acumane's ``packages/arch/src/sql-guard.ts``
(parse with a real parser, walk the whole tree, allow a narrow shape) to SQLGlot and DuckDB.

A query passes only if:
* it is one statement, built from SELECT / WITH / set operations;
* every table it reads is a governed dataset of this workspace (or a CTE it defines);
* it calls no table functions and no function on the deny list;
* it does not read a column whose policy requires a higher role than the caller has.

Passing queries are rewritten to the dataset's current physical table and wrapped in a row
limit. The DuckDB connection additionally runs with external access disabled, so a bypass
of this guard still cannot reach files or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DENIED_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text",
    "read_blob", "parquet_scan", "csv_scan", "glob", "query", "query_table", "getenv",
    "current_setting", "duckdb_settings", "duckdb_secrets", "duckdb_extensions", "load",
    "install", "sniff_csv", "parquet_metadata", "parquet_schema", "iceberg_scan", "delta_scan",
    "sqlite_scan", "postgres_scan", "mysql_scan", "httpfs", "read_ndjson", "read_xlsx",
}

ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)

_FORBIDDEN_NAMES = (
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command", "Copy", "Pragma", "Set",
    "Use", "Transaction", "Commit", "Rollback", "Merge", "Into", "Attach", "Detach", "Describe",
    "LoadData", "Export", "Install",
)
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))


class QueryRejected(ValueError):
    pass


@dataclass
class GuardPolicy:
    tables: dict[str, str]  # dataset name -> physical table in schema "data"
    restricted_columns: dict[str, set[str]] = field(default_factory=dict)  # dataset -> cols denied
    max_rows: int = 10_000


@dataclass
class GuardedQuery:
    sql: str
    datasets: list[str]


def _name(node: exp.Expression) -> str:
    return (node.name or "").lower()


def check_read_query(text: str, policy: GuardPolicy) -> GuardedQuery:
    try:
        statements = [s for s in sqlglot.parse(text, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise QueryRejected(f"could not parse the query: {exc.errors[0]['description'] if exc.errors else exc}") from None
    if len(statements) != 1:
        raise QueryRejected("send exactly one statement")
    tree = statements[0]
    if not isinstance(tree, ALLOWED_ROOTS):
        raise QueryRejected(f"only read queries are allowed, not {tree.key.upper()}")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise QueryRejected(f"{node.key.upper()} is not allowed in a read query")
        if isinstance(node, exp.Func):
            fname = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if fname in DENIED_FUNCTIONS:
                raise QueryRejected(f"function {fname} is not allowed")
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Func):
            raise QueryRejected("table functions are not allowed")

    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE)}
    allowed = {k.lower(): v for k, v in policy.tables.items()}
    used: list[str] = []
    alias_to_dataset: dict[str, str] = {}

    for table in list(tree.find_all(exp.Table)):
        name = _name(table)
        if table.args.get("db") or table.args.get("catalog"):
            raise QueryRejected("refer to datasets by name only, without a schema or catalog")
        if name in cte_names:
            continue
        if name not in allowed:
            raise QueryRejected(f"unknown dataset '{table.name}'")
        used.append(name)
        alias = table.alias_or_name.lower()
        alias_to_dataset[alias] = name
        alias_to_dataset.setdefault(name, name)
        new = exp.Table(this=exp.to_identifier(allowed[name], quoted=True), db=exp.to_identifier("data"))
        new.set("alias", table.args.get("alias") or exp.TableAlias(this=exp.to_identifier(name)))
        table.replace(new)

    restricted = {k.lower(): {c.lower() for c in v} for k, v in policy.restricted_columns.items() if v}
    if restricted and any(d in restricted for d in used):
        _check_columns(tree, used, alias_to_dataset, restricted)

    limited = exp.select("*").from_(exp.Subquery(this=tree, alias=exp.TableAlias(this="q"))).limit(
        policy.max_rows
    )
    return GuardedQuery(sql=limited.sql(dialect="duckdb"), datasets=sorted(set(used)))


def _check_columns(tree, used, alias_to_dataset, restricted) -> None:
    for star in tree.find_all(exp.Star):
        parent = star.parent
        qualifier = parent.table.lower() if isinstance(parent, exp.Column) and parent.table else None
        targets = [alias_to_dataset.get(qualifier, qualifier)] if qualifier else used
        for d in targets:
            if d in restricted:
                raise QueryRejected(
                    f"SELECT * would read restricted columns of {d}; list the columns you need"
                )
    for col in tree.find_all(exp.Column):
        cname = col.name.lower()
        q = col.table.lower() if col.table else None
        candidates = [alias_to_dataset.get(q, q)] if q else used
        for d in candidates:
            if cname in restricted.get(d, set()):
                raise QueryRejected(f"column {d}.{cname} is restricted for your role")
