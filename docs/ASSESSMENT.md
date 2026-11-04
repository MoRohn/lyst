# Assessment before the next stage ("Intelligence Ascension")

Taken on 2026-11-04 against commit `main` after the production-foundation slice, by reading the code and
running the suite (`uv run pytest`: 82 passed; `uv run lyst eval`: 15/15). Classification follows the
continuation prompt: Production-ready, Functional but limited, Partially implemented, Missing, Deficient.

| Area | State | Evidence and the honest limitation |
|---|---|---|
| Store: encrypted DuckDB, one writer per file, migrations | Production-ready for one engine process | `test_store.py`. No multi-process writer, no online backup |
| Security: roles, workspace isolation, audit, redaction, SQL guard | Production-ready | `test_security.py`, `test_sqlguard.py`. Column policies by role; no row-level policies yet |
| Identity: OIDC + PKCE, sessions, members | Functional but limited | Tested against a stub IdP only; not yet exercised against Keycloak or Authentik |
| Data: CSV/TSV/Parquet ingest, versions, profiling, query history | Functional but limited | File upload only; no database or SaaS connectors; no lineage records beyond `analyses.evidence` |
| Semantic: versioned metrics (simple, ratio, filtered), glossary, ambiguity | Functional but limited | No fiscal calendars, units or currency, metric dependencies or entity model |
| Analyst loop | Functional but limited | Rules pick one of eight plans; "why" runs one decomposition and reports the top driver. No competing hypotheses, no multi-period investigation, no depth control, no related-metric corroboration |
| Validation and evidence | Functional but limited | Coverage, freshness, bridge closure, recomputation; queries with ids. Missing: dataset version hashes and timestamps in the package, join and grain checks on raw SQL, verification intensity by consequence |
| Models: OpenRouter gateway on Acumane's rules | Functional but limited | Tested against a stub; never called the real OpenRouter (no Lyst key); no streaming or embeddings |
| Runtime: leases, checkpoints, resume, pause/cancel/retry, budgets, approvals, schedules | Production-ready for sequential plans | Steps run in order; no DAG dependencies or parallel steps; schedules only re-ask a question |
| Memory | Partially implemented | Layers, trust, promotion, revocation and keyword recall exist; nothing recalls into reasoning beyond context listing; no documents, no vector search |
| Skills (Capability Foundry) | Functional but limited | Propose, sandbox test, approval, register, use, retire. Lyst does not yet detect a gap and author a skill itself |
| Evaluation | Functional but limited | 15 scored cases, stored runs. No productivity-multiplier measure, no agent-effectiveness metrics |
| Deliverables | Functional but limited | Excel and Markdown. No PDF, decks or generated dashboards |
| Proactive monitoring | Missing | Schedules exist; no monitors, change detection, prioritisation or notifications |
| Advanced analytics | Partially implemented | Driver decomposition, ETS forecast with backtest, STL anomalies, two-proportion and Welch tests (vendored). No change-point detection, scenarios, optimisation, DiD/ITS |
| UI | Functional but limited | Six areas, live progress, light/dark. No role-adaptive presentation, no chart-element follow-ups |
| Observability and deployment | Functional but limited | JSON logs, Prometheus metrics, Compose written but not run |
| MCP | Missing | — |

## What that implies for order

Rohn's priority list fits the measured gaps. The deepest deficiency is the analyst's reasoning: one plan
per intent and a single explanation. So the next slices are, in order:

1. **Reasoning engine**: depth controller (fast / analytical / deep / project), competing hypotheses for
   "why" questions tested deterministically, multi-period investigation with change-point detection,
   related-metric corroboration, and an evidence package with dataset versions, hashes and timestamps.
2. **Living knowledge**: entities, metric dependencies and lineage in DuckDB; corrections and prior analyses
   recalled into reasoning; documents with citations.
3. **Execution**: DAG steps with parallel independent branches, failure classification, verification
   intensity by consequence.
4. **Analytics**: change points, scenario comparison, linear optimisation, DiD and interrupted time series,
   with assumption statements.
5. **Foundry**: gap detection that drafts a skill and its tests for approval (scenario E).
6. **Proactive**: monitors on approved metrics that investigate, rank and notify (scenario D).
7. **Studio**: generated Streamlit dashboards with refresh schedules (scenario B).
8. **Experience and performance**: role-adaptive answers, follow-ups from chart elements, latency work.

Each slice ships with tests and an update to `CAPABILITY_MATRIX.md`; nothing is marked done on a plan.

## After the Ascension slices (same day)

| Area | Now | Evidence |
|---|---|---|
| Analyst loop | Depth control; deep investigations test seven hypotheses by computation, scan a horizon for change points, corroborate with related metrics; member filters; forecasts with scenarios; event impact (ITS); correlation with detrending; gap → Foundry; sentence → dashboard | `test_reasoning.py`, `test_prescriptive.py`, `test_foundry.py`, `test_studio.py` |
| Validation and evidence | Sources with versions, hashes, timestamps; verification intensity by consequence with independent pandas recomputation; join fan-out and stale-source checks | `test_execution.py`, `test_adversarial.py` |
| Knowledge | Entities, relations, automatic lineage, documents with citations, contradiction disclosure, prior-answer consistency | `test_knowledge.py`, `test_adversarial.py` |
| Runtime | DAG plans, parallel waves, optional steps, failure classes, idempotent long-running projects | `test_execution.py`, scenario G |
| Proactive | Approved monitors that detect, investigate, rank, deduplicate, notify | `test_monitoring.py` |
| Foundry | Templates or a model draft a skill; sandbox, adversarial cases, benchmark, approval, reuse | `test_foundry.py` |
| Evaluation | 19 scored cases, 130 tests, a labelled productivity estimate | `lyst eval`, `pytest` |

Still missing, in priority order: a real OpenRouter or local model exercised end to end (needs a key or an
installed model), database and SaaS connectors, vector search, PDF and deck deliverables, an MCP server
over the registry, tracing, backup automation, load tests, and a Compose run on a Docker host.
