# Lyst architecture

Lyst is a modular monolith: one engine process (FastAPI plus worker threads) that owns every DuckDB file,
and a Streamlit interface that talks to it only over HTTP. Distributed services come later, and only when
measured load calls for them.

```
 Streamlit UI (src/lyst/ui)  ──HTTP──▶  FastAPI  (src/lyst/api)
   Analyst · Work · Data ·               │
   Intelligence · Build · Settings       ├─ Guard: identity, roles, audit, redaction   (security/)
                                         ├─ Analyst engine + capability registry       (analyst/)
                                         ├─ Task runtime: workers, leases, checkpoints (runtime/)
                                         ├─ Data: ingest, catalog, SQL guard           (data/)
                                         ├─ Semantic: metrics, glossary                (semantic/)
                                         ├─ Memory: layers, trust, revocation          (memory.py)
                                         ├─ LLM gateway: OpenRouter or local, optional (llm/)
                                         └─ Store: encrypted DuckDB, one writer each   (store/)
                                                  control.duckdb  +  workspaces/<id>.duckdb
```

## Responsibilities

| Layer | Owns | Never does |
|---|---|---|
| Streamlit | Presentation, forms, live progress (a fragment polls the task every second) | Open a database, hold long-running work, decide permissions |
| FastAPI | Typed, versioned endpoints (`/v1/...`), authentication | Business logic beyond calling a service |
| Guard | Who may do what, in which workspace; the audit log | Trust a model's output for a permission |
| Analyst engine | Turning a request into a plan of capabilities and a verified answer | Produce a number that no query computed |
| Runtime | Leases, step checkpoints, resume, pause/cancel/retry, budgets, approvals, schedules | Re-run a finished step |
| Store | DuckDB files, serialized writes, migrations, encryption | Accept a second writer process |

## The analyst loop

`Understand → Contextualize → Plan → Investigate → Execute → Validate → Interpret/Deliver → Learn`

Each phase is a registered capability (`analyst/registry.py`) with an owning specialist role, a permission
and a phase. The first three steps are fixed; `plan` writes the rest of the steps, which the runtime stores
with the task so a resumed task follows the same plan.

* **Understand** reads intent with rules (`rule_intent`) and resolves metrics through the semantic layer. If a
  model is configured and the rules found no metric, the model may choose only among approved metric names.
* **Contextualize** loads the metric definitions with their dataset versions, recalls memory, and anchors the
  period on the data's latest date ("last quarter" is the last complete quarter in the data, stated as an
  assumption).
* **Plan** picks capabilities: `metric_value` for the period, the prior period and a year earlier;
  `driver_analysis` for "why"; `metric_series` and `anomaly_scan` for trends; `forecast_metric`;
  `profile_dataset` and `quality_scan`; `run_sql`. An ambiguous term (two approved metrics share it) or an
  unknown metric becomes a blocker, and the answer asks instead of guessing.
* **Validate** is independent of the steps it checks: period coverage, freshness, every driver bridge closes
  to the metric's change (to 1e-6), and the metric recomputed from its parts in Python equals the SQL value.
* **Deliver** writes Answer, Evidence, Business implications, Recommended actions and Supporting work
  (assumptions, checks, every query with its id, the plan, reproduction notes). Confidence is the validation
  outcome ("verified", "verified with warnings", "not verified"), never a model probability. If a model
  rewrites the narrative, any number in its text that no tool produced makes Lyst keep the template text.
* **Learn** stores the analysis with its evidence and an episodic memory marked `observed`.

Specialist roles (lead analyst, business analyst, data discovery, analytics engineer, statistical analyst,
data scientist, quality, communications, knowledge curator) are labels on capabilities, not separate model
processes. They are composable; nothing runs that the plan did not ask for.

## Inference

Every model call goes through the gateway in `llm/gateway.py`, a port of Acumane's AcuRout: workload-based
pools and routing, admission limits, adaptive per-model limits, retries with fallback through the tier's
models, and a row per upstream call. OpenRouter is the main provider (decision 0005); a local
OpenAI-compatible server is the alternative. See [MODELS.md](MODELS.md).

## Reasoning depth and hypotheses

`understand` sets a depth: **fast** (values, breakdowns, guarded SQL, skills), **analytical** (one-period
"why", trends, forecasts, profiles), **deep** ("why" over a horizon, or a request to investigate or test
alternatives) and **project** (build or automate requests, the Studio). A deep plan adds `horizon_series`,
`change_points`, `hypothesize`, `test_hypotheses` and `related_metrics` (`analyst/reasoning.py`). The seven
hypotheses (concentration, broad-based, mix, seasonality, single month, data artifact, numerator vs
denominator) each have a deterministic test; the answer separates established findings from hypothesis
verdicts and names the explanation the evidence favours. Nothing in this path calls a model.

## Durable execution

Tasks live in `control.duckdb`. A worker claims a task in one write transaction (queued, or running with an
expired lease), renews the lease before each step, and commits each step's output in the same transaction
that marks it done. If the process dies, the lease expires and another worker resumes from the first
unfinished step, reloading every finished output. Steps that write (e.g. `learn`) key their writes on the
task id, so a re-run step does not duplicate them. `tests/test_scenarios.py::test_h_resume_after_interruption`
proves this: the crash happens mid-plan, a second worker finishes, finished steps are not re-run, there is one
stored analysis, and the answer equals an uninterrupted run.

Plans are DAGs: each step declares `after` (the keys it needs). The runtime runs every ready step of a wave
concurrently on a small pool (`LYST_TASK_PARALLELISM`, default 4), commits each step's checkpoint as it
finishes, and starts the next wave. Capabilities marked `optional` (anomaly scan, change points, related
metrics) are skipped on failure instead of failing the task. Failures are classified: permission and input
errors fail at once, model and data errors are reported as such, transient errors retry once.

Budgets: `max_steps` and `budget_seconds` per task. Step failures retry once when the error may be
transient; permission and validation errors fail at once. Capabilities flagged `requires_approval` stop the
task in `awaiting_approval` until an admin decides. Schedules are created disabled with an approval; once
approved they submit tasks as their owner with autonomy `autonomous`.

Autonomy modes (`assist`, `delegate`, `autonomous`, `governed`) are stored per workspace and per task. Today
they decide whether approval-gated steps may run without a person; per-tool and per-classification policy is
Phase 5.

## Proactive intelligence

`monitoring.py`: an approved monitor on a governed metric is ticked by the worker loop. It detects a robust
z-score shift in the latest complete period, an actual outside the ETS forecast interval, and data-quality
problems inside the period; a material shift submits a deep investigation as the monitor's owner; findings
are ranked (size of the move, direction against "higher is better", relative size), deduplicated per
monitor, period and kind, written to each owner's and admin's inbox, and POSTed to the workspace webhook
with redacted text.

## The Studio

`studio.py`: "create an automated <metrics> reporting dashboard by <dimension>, refreshed weekly" designs on
governed metrics, runs the compiled SQL through the guard, writes Parquet extracts, generates a standalone
Streamlit app (it reads only its extracts, Acumane's arch-runtime rule) and a README with every definition,
verifies by compiling and rendering headlessly with Streamlit's `AppTest`, and proposes a refresh schedule.
The refresh is an ordinary task (`refresh dashboard <id>`) that rebuilds into a staging folder and only
replaces the published extracts when every check passes; otherwise the dashboard is marked stale.

## Security model

* **Identity**: bearer API tokens, stored as SHA-256 hashes. OIDC is not built yet (Phase 2 remainder); the
  `Guard.authenticate` boundary is where it will plug in.
* **Authorization**: roles viewer < editor < admin < owner per workspace (Acumane's `MembershipRole`). Every
  service method calls `Guard.require` with a named action; unknown actions are denied. Denials are audited.
* **Isolation**: one DuckDB file per workspace with its own derived key. A query can only name datasets of
  its own workspace; the guard rejects schema-qualified names.
* **SQL**: parsed with SQLGlot, one read-only statement, no table functions, a deny list of file and system
  functions, column policies by role, and a row limit. Defence in depth: every connection runs with
  `enable_external_access = false` and `lock_configuration = true`, so even a guard bypass cannot read files.
* **Untrusted content**: user text and retrieved content reach a model wrapped as data, and no permission
  depends on model output. External model endpoints receive nothing unless the workspace allows it, and then
  only redacted text. Audit details are redacted.
* **Encryption at rest**: DuckDB's AES-GCM database encryption, a key per file derived by HMAC-SHA256 from a
  master key (`LYST_MASTER_KEY`, or a 0600 keyfile for development). Key rotation and a KMS adapter are not
  built.

## CASSCA applied

| | How Lyst applies it today |
|---|---|
| **Context** | Tasks carry the request, plan, every step output and an event log; follow-ups reuse the resolved metric |
| **Acumen** | Answers use approved, versioned metric definitions; ambiguity is surfaced, not guessed |
| **Speed** | Embedded DuckDB, no network hop to a database; rules before models; the demo's margin investigation runs in about one second |
| **Security** | Permission checks in code, encrypted files, guarded SQL, external access off, audit log |
| **Cost** | No model call unless configured and needed; every call is recorded in `llm_calls` |
| **Access** | Workspace roles, column policies, approvals, proposer cannot approve their own capability |

## Why not X

* **Another database for metadata**: DuckDB holds it. One writer per file is enough for a single engine
  process; a coordination service is justified only once several engine processes must share a workspace.
* **LangGraph or PydanticAI**: the plan is data and the runtime checkpoints each step itself, which is what
  durable resume needs. An agent framework can sit behind a capability later without changing the runtime.
* **Celery or Ray**: not needed for one process with worker threads; the claim-and-lease protocol already works
  across processes if the store moves behind a coordination service.
