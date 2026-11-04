# Roadmap

Status on 2026-11-04. Each phase ends in working, tested slices, not documents.

| Phase | State | Done | Next |
|---|---|---|---|
| 1. Audit and architecture | Done | Audit, reuse decisions, licenses, DuckDB design, runnable project | — |
| 2. Product foundation | Done | App, Streamlit theme, workspaces, roles, members, OIDC sign-in, FastAPI, DuckDB persistence, workers, health, JSON logs, metrics, Dockerfile and Compose | Run the Compose stack on a Docker host |
| 3. Core analyst engine | Done except live models | Task intake, retrieval, planning, SQL and Python tools, profiling, validation, evidence, OpenRouter gateway, evaluation suite (`lyst eval`, 15 cases) | A real OpenRouter call (needs a Lyst key); a local model end to end |
| 4. Enterprise data and knowledge | Started | Metrics, glossary, column policies, memory with trust | PostgreSQL connector via DuckDB's postgres scanner behind the connector SDK, lineage records, document ingestion with citations, DuckDB `vss` vector recall |
| 5. Advanced autonomous execution | Started | Durable resume, approvals, budgets, schedules | Parallel independent steps, event triggers, per-tool and per-classification autonomy policy, notifications |
| 6. Build and capability expansion | Started | Governed skills end to end (scenario F), Excel and Markdown deliverables, Build page | MCP server over the registry; PDF and deck outputs; generated Streamlit dashboards in Acumane's `arch-runtime` pattern; SQL model and workflow authoring |
| 7. Enterprise hardening | Not started | — | Threat model, isolation and load tests, backup/restore exercise, OpenTelemetry, admin docs |

Acceptance scenarios: A (margin investigation), C (data quality), F (capability creation), G (security) and H
(resume) have automated tests today. B (recurring dashboard), D (research briefing) and E (workflow automation)
depend on the rest of Phases 5 and 6.
