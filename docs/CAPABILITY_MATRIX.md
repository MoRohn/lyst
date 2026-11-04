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
| Analyst | Data-quality investigation | Tested | Scenario C |
| Analyst | Independent validation and evidence package | Tested | Scenario A |
| Analyst | Model-written narrative with number verification | Tested (mock model) | `test_llm.py` |
| Runtime | Leases, checkpoints, resume after crash | Tested | Scenario H |
| Runtime | Pause, resume, cancel, retry, step budget | Tested | `test_runtime.py` |
| Runtime | Approval-gated schedules | Tested | `test_schedule_needs_approval_then_runs` |
| Security | Roles, workspace isolation, audit of denials, redaction | Tested | Scenario G, `test_security.py` |
| Security | External model blocked unless allowed, then redacted | Tested | `test_llm.py` |
| Memory | Layers, trust, promotion, revocation, corrections | Built, partly tested | `memory.py`; promotion path untested |
| UI | Six-area Streamlit app with live progress, light and dark themes | Checked by hand with Playwright screenshots | `docs/images/` |
| API | Versioned FastAPI endpoints | Tested | `test_api.py` |
| Identity | OIDC / SSO | Planned (Phase 2) | — |
| Connectors | PostgreSQL, warehouses, SaaS; connector SDK | Planned (Phase 4) | — |
| Knowledge | Documents with citations, vector search | Planned (Phase 4) | — |
| Build | Skill authoring, sandboxed tests, promotion | Planned (Phase 6); only the approval rule exists | `test_proposer_cannot_approve_own_capability` |
| Deliverables | Excel, PDF, slides, generated dashboards | Planned (Phase 6) | — |
| Ops | Metrics/tracing, backup automation, load tests | Planned (Phase 7) | — |
| Model | Local model actually exercised end to end | Not yet: no local model is installed on the dev machine | — |
