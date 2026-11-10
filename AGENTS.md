# AGENTS.md: a map, not a manual

For coding agents (and people) working in this repository. Read this first, then only the guide the task needs.
Rules marked **checked** are also enforced by a test, so breaking one fails CI rather than relying on memory.

## Where things are

- Architecture and the engine: `docs/ARCHITECTURE.md`; the composition root `src/lyst/app.py`; lane services are
  wired in `src/lyst/lane.py` (add yours under `# -- other lanes`).
- The task runtime (leases, checkpoints, resume, budgets, approvals): `src/lyst/runtime/__init__.py`.
- The harness (contracts, receipts, failure classes, harness rules, state): `src/lyst/harness.py`, `docs/HARNESS.md`.
- Learning from people (a verdict on an answer, what Lyst stops reusing because of it): `src/lyst/feedback.py`,
  `docs/LEARNING.md`.
- Graph runs (planner, builders, checker, router, learned rules): `src/lyst/graphs.py`, `docs/GRAPH_RUNS.md`.
- Events (envelope, transactional outbox, durable consumers, chains, SSE stream): `src/lyst/events.py`, `docs/EVENTS.md`.
- Analyst capabilities: `src/lyst/analyst/registry.py` (the tool contract) and `src/lyst/analyst/engine.py`.
- The business brief (every metric for one period, what moved more than usual and why): `src/lyst/analyst/brief.py`.
- Period-close checks (arrival, clean, tie-out to a second source, flux by segment): `src/lyst/analyst/close.py`.
- Receivables (open invoices today, aging, owed by client, days to pay): `src/lyst/analyst/receivables.py`.
- The AI SDK wire format (Lyst's work as a UI message stream for `useChat`): `src/lyst/uistream.py`, `src/lyst/api/aisdk.py`,
  `docs/AI_SDK.md`; `tests/aisdk` reads it with the real `ai` package.
- Lyst Apps (generated micro-apps, specs bound to governed metrics): `src/lyst/apps.py`, `src/lyst/api/apps.py`.
- HTTP API: `src/lyst/api/__init__.py` plus one routes file per lane in `src/lyst/api/`; the compatibility record
  is `docs/api/contract.txt` (regenerate with `scripts/api_contract.py`).
- Schema: `src/lyst/store/schema.py` (append a migration; never edit an applied one).
- UI (Streamlit, an HTTP client of the API): `src/lyst/ui/`; design rules in `docs/ux/`.
- Security and governance: `docs/SECURITY.md`, `src/lyst/security/`, `src/lyst/governance.py`.
- What exists and how it is tested: `docs/CAPABILITY_MATRIX.md`. Claim nothing beyond it.

## Testing map

- `uv run ruff check src tests scripts` and `LYST_ENCRYPT=0 uv run pytest -q` (the variable only matters where DuckDB
  cannot download its encryption extension, such as a cloud container).
- Runtime invariants: `tests/test_runtime_integrity.py`, `tests/test_backup_integrity.py`. Keep both green.
- Harness and its boundaries: `tests/test_harness.py`, `tests/test_harness_boundaries.py`.
- API compatibility: `tests/test_api_contract.py`. Answer quality: `uv run lyst eval --baseline docs/quality/eval-baseline.json`.

## Product rules

- A task ends only through `_fail` or `_finish`, which are fenced on the worker's lease. **checked**
- The UI never imports the store, runtime, data or security layers; it calls the API. **checked**
- Backend code never imports `lyst.ui`. **checked**
- Migration versions are never reused; each lane appends in its own range (needs you 160-169, data quality 150-159, learning 120-129, events 110-119, harness 100-109, graph 90-99, UI 80-89,
  specializations 70-79, voice 60-69, anticipate 50-59, tenth 40-49, adapt 30-39, ops 20-29, platform below 20).
  **checked** for uniqueness and the harness, events and learning ranges.
- Work that must happen because of an event is a durable consumer (`events.consume`), idempotent by event id; the
  in-memory `subscribers` list is best effort. Events never carry answer text or data rows.
- A model's confidence is never evidence. Done means the contract's checks passed on stored records.
- Speech never authenticates or authorizes; audio is ephemeral (`docs/VOICE.md`).
- Do not describe Lyst as super-intelligence or ASI in product copy or docs.

## Security rules

- Every service call checks permission through `Guard.require`; the model cannot grant itself access.
- Irreversible or outward-facing steps wait for a person's approval; the proposer of a capability cannot approve it.
- Never log or store secrets; credentials go through `src/lyst/secrets/` (`docs/decisions/0006-credential-boundary.md`).
- Queries go through the read-only SQL guard (`src/lyst/data/sqlguard.py`).

## When something fails

Fix the class of failure, not the one output: add a guide here or near the code, a test, a better tool contract, a
permission gate or durable state. In the product, the harness does the same for tasks: it classifies failed runs and
proposes a rule for a person to approve (`docs/HARNESS.md`).
