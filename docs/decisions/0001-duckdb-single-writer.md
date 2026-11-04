# 0001. DuckDB with one writer process per file

**Decision.** All persistence is DuckDB. The engine process owns every file; writes go through one writer
thread per file; reads use separate cursors. No Neon, no Postgres for metadata.

**Why.** DuckDB permits one read-write process per file. Serializing writes inside that process removes
write conflicts and keeps transactions simple, and DuckDB's file lock turns a misconfigured second writer into
an error instead of corruption.

**Cost.** Several engine processes cannot write one workspace. When that is needed, add a coordination
service in front of the writer; keep DuckDB as the analytical store.
