# Engineering excellence register

The evidence log for the engineering-excellence audit. Each finding was reproduced before it was fixed, and a
fix counts as **Verified Fixed** only when a test (or a recorded run) failed before the change and passes after.
Machine-readable status for each pass: `docs/quality/pass-N.json`. Product-experience items live in
`docs/PRODUCT_EXCELLENCE.md`, honest open gaps in `docs/GAPS.md`; neither is repeated here.

Status words: **Verified Fixed** · **Implemented but Unverified** · **Partially Resolved** · **Confirmed Open** ·
**Not Yet Evaluated**.

## Architecture as the code runs it (checked against source, 2026-11-04)

```
Browser ─▶ Streamlit (src/lyst/ui, no data, HTTP only via ui/client.py)
            │  Bearer token (OIDC session, pasted token, or the local shared token: see U1)
            ▼
FastAPI engine (src/lyst/api) ─ Guard.authenticate → Guard.require(workspace, action) → audit_log (hash-chained)
            │
            ├─ TaskRuntime (src/lyst/runtime): tasks/task_steps/task_events in control.duckdb
            │     claim (lease) → heartbeat → waves of steps on a thread pool → checkpoint per step → fenced finish
            │     steps = registry capabilities (src/lyst/analyst/*): understand → contextualize → plan → execute → validate → deliver → learn
            ├─ Data (src/lyst/data): SQL guard (sqlglot) + column and row policies → workspace DuckDB (one file per workspace)
            ├─ Semantic layer, knowledge, workflows (compiled repeat work), skills (sandboxed subprocess)
            ├─ Credentials (src/lyst/secrets): AES-256-GCM envelope or OpenBao; broker for gateway and connectors only
            ├─ Connectors (src/lyst/connectors): every call wrapped so errors are scrubbed of the connection's secrets (C1)
            └─ LLM gateway (src/lyst/llm): OpenRouter or a local OpenAI-compatible model; numbers in model wording verified
Storage: one process owns every DuckDB file (DuckDB's file lock enforces it); one writer thread per file, reads on cursors.
```

Boundaries that matter: the browser never holds a provider credential; Streamlit never opens a database; every
mutation of a file is serialized on its writer thread; a task's outcome is written only by the worker holding its lease.

## Findings

| ID | Severity | Area | Finding | Root cause | Fix | Status |
|---|---|---|---|---|---|---|
| Q1 | High | CI | No workflow ran the tests; `6f49b1b` reached main with a failing adversarial test | Only the website workflow existed | `.github/workflows/ci.yml`: lockfile check, ruff, SPDX, pytest with PostgreSQL 17, `lyst eval`, license gate | Verified Fixed (PR #6 run) |
| R1 | Critical | Runtime | A step longer than the 30 s lease ran twice | Lease renewed only between waves; a second worker claimed the expired lease | Heartbeat renews every lease/3 while the task runs; step checkpoints fenced on the lease owner | Verified Fixed (`test_a_step_longer_than_the_lease_is_not_run_twice`) |
| R2 | High | Runtime | A cancel during the last step was replaced by `done` | Terminal writes were unconditional | `_finish` writes only while this worker holds the running lease | Verified Fixed (`test_cancel_during_the_last_step_is_not_overwritten_by_done`) |
| R3 | High | Runtime | A task that crashes its worker is re-claimed forever | `attempts` counted, never checked | `takeovers` column (control migration 9); fail after `LYST_TASK_MAX_TAKEOVERS` (5) | Verified Fixed (`test_a_task_that_keeps_killing_its_worker_is_abandoned`) |
| S-DB1 | High | Storage | `Database.write` timed out but the write committed later (reproduced: caller told "timed out", row present) | The future was left queued | Cancel if not started; if started, wait for the real outcome | Verified Fixed (`test_a_write_that_times_out_in_the_queue_is_withdrawn_not_applied_later`) |
| T1 | Medium | Tenancy / audit | Deleting another workspace's connection id answered 200 and audited `connection.delete ok` | No existence check in the workspace | 404, nothing audited as success | Verified Fixed (`test_tenant_isolation.py`) |
| T2 | — | Tenancy | Scenario 7 as a sweep: every API route with a path parameter, called by a member of another workspace | — | — | Verified: no 2xx, no 5xx, no victim data or secret in any body, every refusal audited (79 route/method pairs) |
| C1 | High | Secrets | A driver error that echoes its DSN stored the database password in `connections.last_error` and returned it to the browser; discover/sync failures answered 500 | Pattern redaction cannot know an arbitrary password is secret | Every connector wrapped: failures become `ConnectorError` with the raw and URL-encoded secret values removed, no chained cause | Verified Fixed (`test_secret_leakage.py`) |
| U1 | Critical | Identity | `lyst ui` listened on every interface and lent the owner's dev token to any browser; a LAN browser opened the UI signed in as the owner (reproduced with Playwright on 192.168.68.73) | Streamlit binds all interfaces by default; the token fallback ignored who was asking | `lyst ui` binds localhost unless `STREAMLIT_SERVER_ADDRESS` says otherwise; shared token only with `LYST_UI_LOCAL=1`, a loopback client and no proxy headers | Verified Fixed (browser re-run: LAN refused; network-bound UI asks for a token; local UI signed in) |
| A1 | Medium | Analyst | "What was revenue last quarter by region?" answers with the total only; the breakdown is in a table and a finding | The value template leads with the total for breakdown intent | Not changed: `analyst/engine.py` is being edited by the build thread | Confirmed Open |
| P1 | Medium | Performance | Submit p95 is 300 ms with 100 people at once (objective in PERFORMANCE.md: 200 ms) | Not isolated. Hypothesis, unverified: GIL-bound analysis on 4 worker threads plus 100 polling clients | — | Confirmed Open |
| H1 | Low | Runtime | The heartbeat cannot help if the whole process stalls longer than the lease (GIL starvation, debugger) | Leases are time-based | Fencing (R1/R2) keeps a stale worker from writing results; a stalled step may still run twice | Partially Resolved |

## Measurements (Mac mini, Apple Silicon, Python 3.13, DuckDB 2.0; 2026-11-04)

Load (`scripts/load_test.py`, engine with `LYST_WORKERS=4`, Northwind demo, no model, one round of the five-question bank):

| People at once | Tasks | Failures | Submit p50 / p95 | End-to-end p50 / p95 | Tasks/s |
|---:|---:|---:|---:|---:|---:|
| 8 | 8 | 0 | 25 ms / 26 ms | 0.61 s / 0.71 s | 10.6 |
| 25 | 25 | 0 | 66 ms / 70 ms | 1.40 s / 2.11 s | 11.5 |
| 100 | 100 | 0 | 220 ms / 300 ms | 7.56 s / 11.25 s | 8.6 |

Large workload (`scripts/bench_large.py --rows 5000000`, in-process): generate 1.1 GB peak RSS; ingest 0.66 s
(peak 1.38 GB); guarded `GROUP BY region` 33 ms, equal to numpy; "What was revenue last quarter?" 0.16 s end to
end, answer $44.40M equal to the independent numpy sum $44,398,871.39; peak 1.44 GB.

Write path: 0.24 ms per single-row transaction on an encrypted file, 0.11 ms per row inside one transaction.

What these do not say: no model call is included; the demo dataset is small; one machine; not a capacity guarantee.

## Scenarios (section 21)

| # | Scenario | Status |
|---|---|---|
| 2 | Concurrent jobs across workspaces | Partially: load test (one workspace) and route sweep (two); a mixed multi-workspace load run is not yet done |
| 3 | Mid-execution worker failure | Verified by tests: resume from checkpoints (`test_h_resume_after_interruption`), no duplicate step under lease expiry (R1), fenced results (R2), crash loops abandoned (R3) |
| 4 | Compromised or missing credentials | Partially: connector errors scrubbed (C1); revoked credentials already covered by `test_gaps.py` |
| 6 | Browser closed mid-task | Design verified in code: tasks run in the engine and the Home page restores `?task=` from the URL; not yet driven end to end in a browser |
| 7 | Cross-tenant security | Verified (T2) |
| 8 | Large analytical workload | Measured (above) |
| 1, 5, 9, 10 | Financial investigation, misleading data, workflow reuse, full journey | Not Yet Evaluated in this pass (existing coverage: `test_adversarial.py`, `test_workflows.py`, `scripts/bench_workflows.py`) |

## Not yet evaluated, and why

Model routing quality/latency/cost (no OpenRouter key connected yet; GAPS M1), a real identity provider (GAPS S10),
Docker/Compose (no Docker on this machine; GAPS O1), untrusted MCP servers, knowledge-layer retrieval quality,
backup during an active workload, multi-workspace load.
