# DuckDB schema, persistence and concurrency

## Files

| File | Holds |
|---|---|
| `$LYST_HOME/control.duckdb` | workspaces, users, memberships, api_tokens, audit_log, tasks, task_steps, task_events, approvals, schedules, llm_calls |
| `$LYST_HOME/workspaces/<id>.duckdb` | schema `data` (physical dataset tables `<name>__v<n>`), datasets, dataset_versions, column_policies, metrics, metric_versions, glossary, query_history, analyses, memory, skills |
| `$LYST_HOME/.lyst-key` | development master key (0600). Production sets `LYST_MASTER_KEY` |

Schemas are versioned in `src/lyst/store/schema.py` and applied by `Database.migrate`, which records each
version in `_migrations`. A migration is never edited after it ships; a change is a new version.

## Concurrency, as DuckDB actually behaves

DuckDB allows one read-write process per file and many cursors inside that process. Lyst relies on exactly
that:

1. **One engine process opens the files.** Streamlit and other clients use the API. DuckDB's file lock makes
   a second opener fail; `tests/test_store.py::test_second_process_cannot_open_a_live_file` checks it.
2. **One writer thread per file.** `Database.write(fn)` queues `fn`, which runs on that file's writer thread
   inside `BEGIN … COMMIT`, rolled back on any exception. Writes are serialized, so there are no write-write
   conflicts to retry (`test_concurrent_writes_are_serialized_and_reads_run_alongside`: 8 threads × 25
   read-modify-write updates end at exactly 200).
3. **Reads take their own cursor** and see the last committed snapshot, in parallel with the writer.
4. **Durable event log.** `task_events` and `audit_log` are append-only tables written in their own
   transactions; task step outputs commit in the same transaction that marks the step done.

What this does not give: several engine processes writing the same workspace. If that becomes necessary, the
plan is a small coordination service that owns the writer, not a second database for analytics.

## Datasets and versions

Ingest reads CSV, TSV or Parquet with PyArrow in Python (DuckDB's own file readers are disabled on purpose),
normalizes column names, profiles the frame, and creates `data.<name>__v<n>` plus a `dataset_versions` row with
the source file's SHA-256, row count, schema and profile. Re-uploading the same bytes is a no-op. Queries name
the dataset; the guard maps the name to the current physical table, so older versions stay reproducible.

## Encryption

Each file is attached with `ENCRYPTION_KEY`, a per-file key derived as `HMAC-SHA256(master, "lyst:db:<name>")`.
`test_files_are_encrypted_at_rest` checks that a planted value is absent from the raw bytes and that the file
will not open without the key or with a wrong one.

## Backup and restore

Not automated yet. A consistent backup today is: stop the engine, copy `$LYST_HOME`, keep the master key
separately. An online backup through DuckDB's `EXPORT DATABASE` from the writer thread is a Phase 7 item.
