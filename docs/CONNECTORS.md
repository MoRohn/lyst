# Connectors

A connector gives Lyst isolated, least-privilege, read-only access to a source system. Connections belong to
a workspace; secrets live in their own column of the encrypted control database and never leave the service
(the API returns configuration without them). A connection is tested before it is saved.

| Kind | What it reads | Incremental | Notes |
|---|---|---|---|
| `postgres` | Tables and views in the listed schemas | Yes, by a cursor column (`id`, `updated_at`) | psycopg 3; every transaction `READ ONLY`, 60 s statement timeout, server-side cursors, identifiers validated |
| `files` | CSV, TSV and Parquet files in one directory | No (a sync reloads the file) | The directory must be under `LYST_IMPORT_ROOT` (default `$LYST_HOME/imports`) |

## Syncing

`sync` loads a source table into a governed dataset. The first sync is a full load; with a cursor column,
later syncs read only rows past the last cursor value and the new dataset version is the previous version's
rows plus the new ones. Every sync is a dataset version with a hash, so earlier states stay reproducible, and
an audit line. A sync can be scheduled (`sync connection <id> table <t> by <cursor>`), which goes through the
same approval as any schedule and runs as its owner.

## Writing a connector

Add a module in `src/lyst/connectors/` with a class that declares a `Manifest` (kind, label, fields with
their `secret` flag, capabilities) and implements `test()`, `discover() -> list[SourceTable]` and
`read(table, cursor_column, after) -> Iterator[pyarrow.Table]`, decorated with `@register`. Nothing in the
core application changes; the API and the Data › Connections page read the manifest.

Rules every connector keeps: it reads, never writes; it validates identifiers it puts into SQL; it fails
loudly on a connection problem so that nothing untestable is saved; it never logs secrets.

## Not yet built

MySQL, SQL Server, Snowflake, BigQuery, Redshift, Databricks, REST/GraphQL, SaaS applications (Salesforce,
Workday, NetSuite, ServiceNow, Jira), object storage. The SDK is the path for each; the commercial ones stay
optional integrations behind the same interface.
