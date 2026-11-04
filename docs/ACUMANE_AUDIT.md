# Acumane audit and reuse report

Read on 2026-11-04 against Acumane `main` at `363c934` (`~/acumane`) and ai-analytics `main` at `d86ee66`
(`~/ai-analytics`). Acumane was not modified.

## What Acumane is, technically

A TypeScript monorepo (pnpm + Turborepo): a Next.js app (`apps/web`), shared packages (`agent-core`, `arch`,
`arsi`, `config`, `db`, `emails`, `mcp-server`, `ui`), Prisma on Postgres with pgvector, Inngest for jobs,
OpenRouter for every model call, Nango as a connections broker, and two Python services
(`services/arch-runtime` runs generated Dash and Streamlit apps in isolation; `services/voice`). There is no
`LICENSE` file; it is Rohn's own proprietary code, so Lyst may reuse it, but anything ported carries Lyst's
license only because the same owner relicenses it.

The headline constraint: Lyst must be Python, Streamlit, FastAPI and DuckDB, with no Neon (Acumane's
workspace databases now live on one Neon project, commit `a7fb5bd`) and no mandatory proprietary service
(Acumane requires OpenRouter for managed inference). So almost nothing can be imported as-is. What transfers is
design: rules, contracts, and tested behaviours, which Lyst reimplements in Python. Where ai-analytics already
had tested Python, Lyst vendors it.

## Decision per component

Legend: **Reuse** (code carried over), **Port** (design reimplemented in Python), **Rebuild** (new for Lyst),
**Replace** (conflicts with Lyst's stack or policy), **Drop** (not relevant to an analyst).

| Acumane component | Where it lives | What it does well | Decision for Lyst | Lyst location |
|---|---|---|---|---|
| **AcuHub** | `agent-core/src/acuhub-*.ts` | Picks the answer's form (text, table, chart, calculator), number checks on answers, recipes, follow-ups, save to memory | **Port** the answer contract and the number check; **Drop** the generated interactive explainers | `analyst/engine.py` `deliver` (Answer → Evidence → Implications → Actions → Supporting work), `unbacked_numbers` |
| **Hub intelligence** | `agent-core/src/intelligence/` (`router`, `policy`, `verify`, `registry`) | Rules first, model only for what rules cannot read; intents with tool families; `unbackedClaims` rejects model text whose numbers no tool produced; shadow mode before switching | **Port** all four ideas | `analyst/engine.py` `rule_intent`, `understand` (model may only pick approved metric names), `unbacked_numbers`; capability permissions in `analyst/registry.py` |
| **AcuBin** | `agent-core/src/bin-*.ts`, `db/src/bin*` | Versioned files, cited answers, exact decimal sums from cells, OCR, sharing | **Port later** (Phase 4: documents as knowledge with citations). The "exact numbers from cells, never from a model" rule is already Lyst's rule | not yet built |
| **AcuDB (memory)** | `db/src/acudb/` | Scoped memory per org/workspace/agent; layered long-term memory; recall that never pastes raw vectors; facts replaced but kept with dates (`asOf`) | **Port** scopes, trust and revocation; **Replace** pgvector with DuckDB (vector search via DuckDB's `vss` extension is a Phase 4 item) | `memory.py` (layers, provenance, trust `observed → user_validated → approved`, revoke, promote) |
| **Data / AcuArch** | `packages/arch/src/` | `sql-guard.ts` (parse with the real parser, walk the tree, allow-list), metrics defined once with versions and checks, profiling, relationship inference, changes previewed and approved, drift repair, dashboards checked in an isolated runtime | **Port** the guard, the metric model and profiling; **Port later** relationship inference, change approvals, drift; **Reuse later** `services/arch-runtime` (already Python) for Build-generated dashboards | `data/sqlguard.py` (SQLGlot), `semantic/__init__.py`, `data/__init__.py` |
| **AcuRout** | `agent-core/src/ai-gateway/`, `db/src/ai-gateway.ts` | Admission control by requests and tokens, interactive vs background pools, Postgres leases across replicas, one span per model call | **Port** the pools idea (interactive work is claimed before scheduled work) and per-call accounting; **Replace** OpenRouter-only routing with a provider-neutral OpenAI-compatible client | `runtime/__init__.py` (claim order, leases in DuckDB), `llm/__init__.py` (`llm_calls`) |
| **Jev / Clef decisions** | `intelligence/registry.ts` | Constrained decision models on OpenRouter | **Replace**: proprietary hosted dependency. Lyst's rules-first planner works with no model at all | `analyst/engine.py` |
| **Automations / schedules** | `agent-core/src/automations/`, Inngest | Recurrence, templates, checks before running | **Port**: schedules need approval before they run, and run as the owner's identity with autonomy `autonomous` | `runtime/__init__.py` `create_schedule`, `tick_schedules` |
| **AcuCal** | `agent-core/src/acucal/` | Calendar proposals that a person confirms | **Drop** from the core; the "a model's yes never authorizes a write" rule is kept as approval gates | `runtime` approvals |
| **AcuCom** | `services/voice` | In this repo AcuCom is voice, not messaging (confirmed in `docs/ACUBIN.md`) | **Drop** for now; notifications are a Phase 5 item | not built |
| **Spaces** | Prisma `Organization`, `Space`, `Membership`, `MembershipRole` | Client spaces, roles OWNER/ADMIN/EDITOR/VIEWER, a client never sees another client's files | **Port** as workspaces: one encrypted DuckDB file per workspace, the same four roles, enforced in code | `store/`, `security/` |
| **CASSCA** | `docs/ADAPTIVE_INTELLIGENCE.md`, `docs/ACUARCH.md` | Every decision traded off on Context, Acumen, Speed, Security, Cost, Access | **Reuse** as the design discipline | `docs/ARCHITECTURE.md` |
| **ARSI** | `packages/arsi/` | Propose → Predict → Select → Evaluate → Learn → Retain with modes off/suggest/auto-apply | **Port** as governed skill promotion (propose, test in a sandbox, approve by someone other than the proposer, register). `auto_apply` is not carried over | `runtime.decide` blocks self-approval of `skill.register`; skill authoring is Phase 6 |
| **MCP server** | `packages/mcp-server/` | `ask_agent`, `search_files`, `read_memory`, `write_memory` over JSON-RPC | **Port later** (Phase 6): expose Lyst's capability registry as MCP tools | not built |
| **Connections (Nango)** | `agent-core/src/connections/` | OAuth broker for SaaS | **Replace**: Nango cloud is not a required dependency. Lyst's connector SDK (Phase 4) is declarative and runs in-process | not built |
| **Billing, Stripe, referrals, marketing site, emails** | various | — | **Drop** | — |
| **Prisma / Postgres / Neon** | `packages/db` | — | **Replace** with DuckDB | `store/` |

## From ai-analytics (Python, reused directly)

`streamlit/kit/src/stkit` is Rohn's tested Python analytics core. Lyst vendors five pure modules unchanged,
with a provenance header, into `src/lyst/analytics/`: `profile.py` (column roles, missingness, PII patterns),
`analytics/drivers.py` (contribution, exact rate-and-mix decomposition), `analytics/decomp.py`,
`analytics/timeseries.py` (ETS forecast with backtest, STL anomaly scores) and `analytics/stats.py`. Its
`ai.py` is not used because it calls Anthropic's API directly.

## From the reference projects

* **ai-analyst-lab/ai-analyst** (MIT, checked on GitHub 2026-11-04). Worth adopting: an agent registry with
  contracts, a `.knowledge/` store with `corrections/` and `query-archaeology/`, connection templates per
  warehouse, and evaluation docs. It runs inside Claude Code, which Lyst must not require, so Lyst copies no
  code: the registry (`analyst/registry.py`), corrections as user-validated memory, and query history
  (`query_history`) are independent implementations of the same ideas.
* **Actian, Atlan, Zamp, Workforce Institute** articles: they reinforce governed definitions, metadata and
  lineage, bounded autonomy and measurable workflow outcomes. They shaped the semantic layer's "approved
  definition or ask" rule and the autonomy modes; nothing is copied.

## Technical debt found in Acumane that Lyst avoids

* Jobs run inside the web process (`docs/ACUROUT.md`: "Inngest jobs, which today run inside the web
  process"). Lyst's runtime is owned by the API process, independent of any UI session, and resumes from
  checkpoints.
* Managed inference depends on one vendor (OpenRouter). Lyst runs with no model, or any OpenAI-compatible one.
* Two languages for one product (TypeScript plus Python services). Lyst is Python end to end.
