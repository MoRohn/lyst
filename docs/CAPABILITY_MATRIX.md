# Capability matrix

"Tested" means an automated test in `tests/` exercises it. Nothing marked planned exists in code.

| Area | Capability | State | Evidence |
|---|---|---|---|
| Store | Encrypted DuckDB per workspace, derived keys | Tested | `test_store.py` |
| Store | Serialized writes, parallel reads, rollback on error | Tested | `test_store.py` |
| Store | Queries cannot reach files or change settings | Tested | `test_store.py::test_queries_cannot_reach_the_filesystem` |
| Data | CSV/TSV/Parquet ingest with versions and source hash | Tested | `test_api.py`, demo seed |
| Data | Profiling (roles, nulls, PII patterns, suggestions) | Tested | `test_c_ledger_discrepancies_are_found` |
| Data | Read-only SQL guard with column policies | Tested | `test_sqlguard.py`, `test_security.py` |
| Data | Query history | Built | written on every query |
| Semantic | Versioned metrics (simple, ratio, filtered), glossary | Tested | `test_scenarios.py` |
| Semantic | Ambiguity surfaced instead of guessed | Tested | `test_ambiguous_term_asks_instead_of_guessing` |
| Analyst | Value with prior period and year-ago comparison | Tested | `test_a_values_match_pandas` |
| Analyst | Driver attribution (exact rate/mix for ratios, contribution for totals) | Tested | `test_a_operating_margin_decline_finds_emea` |
| Analyst | Breakdown, trend, anomaly scan, ETS forecast with backtest | Tested | `test_runtime.py::test_trend_and_forecast` |
| Analyst | Data-quality investigation | Tested | `test_scenarios.py` |
| Analyst | Next-period forecast with interval and staffing scenarios under uncertainty (Monte Carlo), minimum headcount, named scenarios | Tested | New scenario C, `test_prescriptive.py` |
| Analyst | Event impact by interrupted time series with stated identification assumptions; DiD and linear allocation helpers | Tested | `test_prescriptive.py` |
| Analyst | Dimension member filters from the request ("in EMEA") applied to every step | Tested | `test_prescriptive.py::test_member_filters_apply_everywhere` |
| Analyst | Independent validation and evidence package (checks, queries, dataset versions with hashes, timestamps) | Tested | Scenario A |
| Analyst | Reasoning depth (fast / analytical / deep / project) chosen from the request | Tested | `test_reasoning.py` |
| Analyst | Deep investigation: quarterly horizon, change points, seven competing hypotheses decided by computation, related-metric corroboration | Tested | `test_reasoning.py::test_a_deep_investigation_tests_competing_explanations` |
| Analyst | Model-written narrative with number verification | Tested (stub model) | `test_llm.py` |
| Models | OpenRouter gateway: tiers, workload routing, admission, adaptive limits, retries, fallback, 402 handling, per-call cost | Tested against a stub OpenRouter | `test_gateway.py`, `test_llm.py::test_openrouter_end_to_end_through_the_analyst` |
| Models | Real OpenRouter call | Not yet: needs a Lyst OpenRouter key | — |
| Runtime | Leases, checkpoints, resume after crash (also mid-wave with parallel steps) | Tested | Scenario H, `test_execution.py` |
| Runtime | DAG plans: declared dependencies, independent steps run in parallel (`LYST_TASK_PARALLELISM`), optional steps skip instead of failing, failures classified (permission, input, data, model, transient) with retries only for transient ones | Tested | `test_execution.py` |
| Analyst | Verification intensity by consequence (low / standard / high); high adds an independent pandas recomputation of the headline value from raw rows | Tested | `test_execution.py::test_verification_intensity_follows_consequence` |
| Runtime | Pause, resume, cancel, retry, step budget | Tested | `test_runtime.py` |
| Runtime | Approval-gated schedules | Tested | `test_schedule_needs_approval_then_runs` |
| Security | Roles, workspace isolation, audit of denials, redaction | Tested | Scenario G, `test_security.py` |
| Security | External model blocked unless allowed, then redacted | Tested | `test_llm.py` |
| Accuracy | Adversarial suite: conflicting definitions surfaced, missing data declared (no estimate), misleading correlation detrended and hedged, unsupported causal questions declined with a path, join fan-out flagged, stale sources warned, contradictory documents disclosed with the approved definition winning, prompt injection in documents and requests inert, permission restrictions audited | Tested | New scenario F, `test_adversarial.py` |
| Accuracy | Productivity measure in evaluation runs: declared human baseline ÷ (agent time + declared supervision), passing cases only, labelled as an estimate | Built | `lyst eval` |
| Runtime | Long-running project resumes without duplicate side effects (dashboard build keyed on the task) | Tested | New scenario G, `test_studio.py` |
| UI | Executive audience gets a brief (answer + implications + first action) ahead of the detail | Tested | `test_adversarial.py::test_executive_audience_gets_a_brief` |
| Memory | Layers, trust, promotion, revocation, corrections | Built, partly tested | `memory.py`; promotion path untested |
| UI | Six-area Streamlit app with live progress, light and dark themes | Checked by hand with Playwright screenshots | `docs/images/` |
| API | Versioned FastAPI endpoints | Tested | `test_api.py` |
| Identity | OIDC sign-in (code flow + PKCE, JWKS verification, domain allow-list, expiring sessions, logout), member management | Tested against a stub provider | `test_auth.py` |
| Ops | JSON logs with request and task ids, Prometheus `/metrics`, `/health` | Tested | `test_auth.py::test_metrics_and_request_ids` |
| Ops | Dockerfile and Compose (non-root, read-only, no capabilities) | Written, not yet run (no Docker on the dev machine) | `docker-compose.yml` |
| Connectors | PostgreSQL, warehouses, SaaS; connector SDK | Planned (Phase 4) | — |
| Knowledge | Entities and relations in DuckDB, automatic lineage metric → dataset → column and analysis → artifact, lineage walks | Tested | `test_knowledge.py` |
| Knowledge | Documents (text, Markdown, PDF) split into passages, BM25 search with role gating, citations in answers | Tested | `test_knowledge.py::test_documents_are_searched_and_cited` |
| Knowledge | Consistency with prior answers on the same data; validated memory surfaced in answers | Tested | `test_knowledge.py` |
| Knowledge | Vector (embedding) search | Planned | — |
| Build | Governed skills: static check, sandboxed tests with adversarial cases, approval by another role, registration, use in plans, retire | Tested | Scenario F, `test_skills.py` |
| Build | Capability Foundry: a request for a method Lyst lacks drafts a skill (template, or a configured model against a fixed contract), runs the static check, sandbox tests, adversarial cases and a benchmark, requests registration, and runs it after approval | Tested | New scenario E, `test_foundry.py` |
| Build | Evaluation harness: question bank, independent expected values, stored scored runs, `lyst eval` | Tested | `test_evals.py` (15/15 on the demo bank) |
| Deliverables | Excel workbook and Markdown briefing from any finished analysis | Tested | `test_deliverables.py` |
| Studio | Dashboard from a sentence: governed metrics, verified SQL, Parquet extracts, generated standalone Streamlit app and README, headless render check, approval-gated refresh schedule that re-runs through the runtime, stale-on-failure with old extracts kept, bundle download | Tested | New scenario B, `test_studio.py` |
| Deliverables | PDF, slides | Planned | — |
| Ops | Tracing, backup automation, load tests | Planned (Phase 7) | — |
| Proactive | Approved monitors: robust-z shift, forecast-interval deviation, data-quality checks; automatic deep investigation; priority ranking; dedupe; inbox and https webhook notifications | Tested | Scenario D, `test_monitoring.py` |
| Models | Local model exercised end to end | Not yet: no local model is installed on the dev machine | — |
