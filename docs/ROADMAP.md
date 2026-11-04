# Roadmap

Status on 2026-11-04. Each phase ends in working, tested slices, not documents.

| Phase | State | Done | Next |
|---|---|---|---|
| 1. Audit and architecture | Done | Audit, reuse decisions, licenses, DuckDB design, runnable project | — |
| 2. Product foundation | Mostly done | App, design system via Streamlit theme, workspaces, roles, FastAPI, DuckDB persistence, workers, health | OIDC sign-in (e.g. Keycloak or Authentik, both OSS), structured JSON logging, a Dockerfile and Compose file |
| 3. Core analyst engine | Mostly done | Task intake, retrieval of definitions and memory, planning, SQL and Python tools, profiling, validation, evidence | Exercise a local model end to end (needs Ollama or llama.cpp installed); an evaluation suite that scores answers over a question bank, not only unit checks |
| 4. Enterprise data and knowledge | Started | Metrics, glossary, column policies, memory with trust | PostgreSQL connector via DuckDB's postgres scanner behind the connector SDK, lineage records, document ingestion with citations, DuckDB `vss` vector recall |
| 5. Advanced autonomous execution | Started | Durable resume, approvals, budgets, schedules | Parallel independent steps, event triggers, per-tool and per-classification autonomy policy, notifications |
| 6. Build and capability expansion | Not started | Approval rule for capability promotion | Skill proposal → sandboxed test (subprocess with no network, resource limits) → approval → registration; MCP server over the registry; Excel/PDF/deck outputs; generated Streamlit dashboards run in Acumane's `arch-runtime` pattern |
| 7. Enterprise hardening | Not started | — | Threat model, isolation and load tests, backup/restore exercise, OpenTelemetry, admin docs |

Acceptance scenarios: A (margin investigation), C (data quality), G (security) and H (resume) have automated
tests today. B (recurring dashboard), D (research briefing), E (workflow automation) and F (capability
creation) depend on Phases 5 and 6.
