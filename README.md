# Lyst

Lyst is a governed, mostly autonomous AI analyst. You describe the question or the work; Lyst plans it, runs
it on approved data definitions with SQL and Python, checks its own numbers, and returns a decision-ready
answer with the evidence behind it. It is judged by completed, verifiable work, not by conversation.

* **Python 3.12+, Streamlit, FastAPI, DuckDB.** Open-source core, self-hosted, no managed database.
* **Models through OpenRouter, on Acumane's rails.** Set `LYST_OPENROUTER_API_KEY` and every call goes
  through Lyst's gateway: tiers, workload routing, admission, adaptive limits, retries with fallback, cost per
  call ([details](docs/MODELS.md)). A local OpenAI-compatible server works too, and with no model at all Lyst
  runs on rules and templates. Every number comes from a query either way.
* Derived from Acumane's design (see [the audit](docs/ACUMANE_AUDIT.md)) but a separate product with its own
  code, data, configuration and license.

![The Analyst workspace answering why operating margin declined](docs/images/analyst-light.png)

## Quick start

```bash
uv sync --all-extras
uv run lyst init-demo        # seeds the Northwind demo workspace and a dev token in ./var
uv run lyst serve            # API + workers on http://127.0.0.1:8770
uv run lyst ui               # Streamlit on http://localhost:8501 (second terminal)
uv run pytest                # 74 tests
uv run lyst eval             # scores 15 analytical questions against independent answers
```

Ask "Why did operating margin decline last quarter?" in the demo. Lyst finds that operating margin fell from
36.0% in Q2 2026 to 32.5% in Q3 2026 and that EMEA's own margin drop (36.8% to 24.0%) explains it, with the
bridge reconciled exactly and every query listed. That story is planted in the demo data
(`src/lyst/demo.py`) so the tests can check that Lyst recovers it.

Configuration is environment variables, listed at the top of `src/lyst/config.py`. Set `LYST_MASTER_KEY` (64
hex characters) in any real deployment; the generated keyfile is for development only.

## Repository layout

| Path | What |
|---|---|
| `src/lyst/` | The product: store, security, data, semantic, analyst, runtime, llm, api, ui |
| `tests/` | Unit and acceptance tests (scenarios A, C, G, H) |
| `docs/` | Architecture, DuckDB design, audit, capability matrix, SBOM, roadmap, decisions |
| `scripts/licenses.py` | Regenerates `docs/SBOM.md` and fails on disallowed licenses |
| `site/` | The marketing website (separate build; see its own README) |

## What works today

See the [capability matrix](docs/CAPABILITY_MATRIX.md) for each item and its test. In short: encrypted,
workspace-scoped DuckDB storage with one writer per file; roles, audit and redaction; a read-only SQL guard;
versioned metrics and a glossary; an analyst loop that answers value, breakdown, "why", trend, forecast,
data-quality and SQL requests with validation and an evidence package; a durable task runtime with resume
after a crash, approvals, budgets and schedules; a versioned API; and a six-area Streamlit interface
(Analyst, Work, Data, Intelligence, Build, Settings) with light and dark themes.

## What does not exist yet

OIDC sign-in, connectors beyond file upload, document knowledge and vector search, PDF and deck
deliverables, generated dashboards, observability, backup automation and load tests. The [roadmap](docs/ROADMAP.md) orders them.

## Documents

[Architecture](docs/ARCHITECTURE.md) · [Models](docs/MODELS.md) · [Skills](docs/SKILLS.md) · [DuckDB design](docs/DUCKDB.md) ·
[Acumane audit](docs/ACUMANE_AUDIT.md) · [Capability matrix](docs/CAPABILITY_MATRIX.md) ·
[SBOM](docs/SBOM.md) · [Roadmap](docs/ROADMAP.md) · [Decisions](docs/decisions/)

## License

[GNU AGPL-3.0-only](LICENSE). If you run a modified Lyst as a network service, you must offer its users the
modified source (AGPL section 13). Dependencies are all AGPL-compatible ([SBOM](docs/SBOM.md)). Vendored
analytics in `src/lyst/analytics/` come from the same author's ai-analytics project and are relicensed with Lyst.
