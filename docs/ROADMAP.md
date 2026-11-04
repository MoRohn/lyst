# Roadmap

Status on 2026-11-04. Each phase ends in working, tested slices, not documents.

| Phase | State | Done | Next |
|---|---|---|---|
| 1. Audit and architecture | Done | Audit, reuse decisions, licenses, DuckDB design, runnable project | — |
| 2. Product foundation | Done | App, Streamlit theme, workspaces, roles, members, OIDC sign-in, FastAPI, DuckDB persistence, workers, health, JSON logs, metrics, Dockerfile and Compose | Run the Compose stack on a Docker host |
| 3. Core analyst engine | Done except live models | Task intake, retrieval, planning, SQL and Python tools, profiling, validation, evidence, OpenRouter gateway, evaluation suite (`lyst eval`, 15 cases) | A real OpenRouter call (needs a Lyst key); a local model end to end |
| 4. Enterprise data and knowledge | Mostly done | Metrics, glossary, column policies, memory with trust, entities and lineage, documents with citations, consistency checks | PostgreSQL connector behind a connector SDK, DuckDB `vss` vector recall |
| 5. Advanced autonomous execution | Mostly done | Durable resume, approvals, budgets, schedules, DAG plans with parallel steps, failure classes, monitors that investigate and notify, idempotent projects | Event triggers from webhooks, per-tool and per-classification autonomy policy |
| 6. Build and capability expansion | Mostly done | Governed skills, the Foundry's own drafts, generated dashboards with refresh, Excel and Markdown deliverables | MCP server over the registry; PDF and deck outputs; SQL model authoring |
| 7. Enterprise hardening | Mostly done | Adversarial suite, isolation tests, threat model, encrypted online backup and verified restore, retention, OpenTelemetry tracing, load test with a measured baseline, deployment docs | A Compose run on a Docker host; a real IdP login; key rotation |
| 8. Product excellence | In progress | Register (docs/PRODUCT_EXCELLENCE.md); brand and accent; Analyst responsiveness, exports, resumable links; workflow compiler with reuse and invalidation; false contradiction and duplicate memory fixes | Open register O1 to O10: result-aware task states, schedules that run compiled workflows, Work page fragments |

Acceptance scenarios: all eight of the first prompt's scenarios except D (external research) and all seven of the
second prompt's (A root cause, B analytics engineering, C forecasting with scenarios, D autonomous discovery,
E missing capability, F adversarial accuracy, G long-running reliability) have automated tests.
