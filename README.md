<p align="center">
  <img src="site/brand/lyst-logo.svg" alt="Lyst" width="160">
</p>

<p align="center">
  <b>An AI analyst that does the work, checks its own numbers, and shows you the evidence.</b><br>
  Self-hosted · Python 3.12+ · DuckDB · Streamlit · FastAPI · AGPL-3.0-only
</p>

<p align="center">
  <a href="https://github.com/MoRohn/lyst/actions/workflows/ci.yml"><img src="https://github.com/MoRohn/lyst/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://morohn.github.io/lyst/">Website</a> ·
  <a href="docs/CAPABILITY_MATRIX.md">What works today</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="docs/SECURITY.md">Security</a>
</p>

---

You describe a question or a piece of work. Lyst plans it, runs it on approved data definitions with SQL and
Python, verifies the result independently, and returns a decision-ready answer with every query, check and
assumption behind it. It is judged by completed, verifiable work, not by conversation: every number comes from
a query, whether or not a model is involved.

![Lyst answering why operating margin declined: a verified answer with evidence, a trend chart, what it means and what to do](docs/images/analyst-light.png)

## Quick start

You need [uv](https://docs.astral.sh/uv/) on macOS or Linux.

```bash
git clone https://github.com/MoRohn/lyst.git && cd lyst
uv sync --all-extras
uv run lyst start --demo
```

One command initialises encryption (the master key goes into the OS credential store), seeds the Northwind
demo workspace, starts the engine on `http://127.0.0.1:8770` and opens the interface at
**http://lyst.localhost:8501**. Ctrl-C stops everything.

Then ask: **"Why did operating margin decline last quarter?"** Lyst finds that operating margin fell from 36.0%
in Q2 2026 to 32.5% in Q3 2026, that EMEA's own margin drop (36.8% to 24.0%) explains it, reconciles the
bridge exactly, and saves the analysis so it can be rerun for any period. That story is planted in the demo data
(`src/lyst/demo.py`) so the tests can check that Lyst recovers it.

No model is needed to try it. With none configured, Lyst runs on rules and templates; adding one improves
understanding and wording, never the numbers.

## A tour of the interface

One navigation with five areas, light and dark themes, and in the sidebar a search box across data, metrics,
saved work and documents, plus a "What these words mean" glossary.

| Area | What you do there |
|---|---|
| **Home** | Ask in plain words, follow live progress, and read the answer. A Get started card guides the first run, and "This month at a glance" shows a tile per approved metric with a one-click **Why?** |
| **Work** | What needs you (approvals), what Lyst is doing and finished, and what runs on its own: watched metrics, scheduled questions and what Lyst noticed |
| **Data** | Datasets with a trust chip each, their sources, dashboards, saved analyses, and guarded SQL. **Add data** uploads a file or connects a source |
| **Brain** | What Lyst knows about the business: approved metrics and business terms with their health and lineage, documents it can cite, and what it remembers and how far to trust it |
| **Settings** | Models (level and costs), Connections (keys and sources), Workspace (members, autonomy, audit trail) and Advanced (skills, capabilities, evaluation runs) |

<p align="center">
  <img src="docs/images/home.png" alt="Home: the ask box, the Get started card and this month's metric tiles" width="80%">
</p>

### Every answer: Summary, Detail, Method, and a Brief

Answers open at **Summary**: the answer, a one-line trust statement ("All 9 checks passed"), the evidence, a
chart, what it means and what to do. **Detail** adds tables, hypotheses, assumptions and sources. **Method** adds
the plan, every check and every query. **Brief** opens the two-line version with one chart, what to do and the PDF
briefing, ready to forward; executive requests lead with it. Limitations stay visible at every depth.

Under each answer you can act on it: **Watch this metric** sets up a monitor, **Repeat it** reruns the saved
analysis on a schedule, **Download** gives a PDF briefing, a deck, an Excel workbook or Markdown, and **How this
ran** shows each step, query and model call with its timing.

<p align="center">
  <img src="docs/images/brief.png" alt="The Brief: the answer, the trust line, one chart, what to do and the PDF briefing" width="49%">
  <img src="docs/images/analyst-dark.png" alt="The same answer in the dark theme, at the Summary depth" width="49%">
</p>

<table>
  <tr>
    <td width="50%"><img src="docs/images/work.png" alt="Work: needs you, activity and recurring runs"><br><b>Work</b>: needs you, activity and recurring runs</td>
    <td width="50%"><img src="docs/images/data.png" alt="Data: datasets with descriptions, rows, freshness and classification"><br><b>Data</b>: datasets, dashboards, saved analyses and SQL</td>
  </tr>
  <tr>
    <td><img src="docs/images/brain.png" alt="Brain: approved metrics and business terms"><br><b>Brain</b>: definitions, documents and memory</td>
    <td><img src="docs/images/saved-analyses.png" alt="Data, Saved analyses: a verified analysis ready to rerun"><br><b>Saved analyses</b>: verified work Lyst reruns for any period</td>
  </tr>
</table>

### Dashboards from a sentence

Data › Dashboards › **New dashboard** turns a request into a Plotly Dash app on Lyst's own kit
(`src/lyst/dashkit`, vendored into each app so it runs anywhere with PyPI packages only): governed metrics,
verified SQL, KPI cards, a trend with a forecast band, click-to-filter breakdowns, and a Data and method page.
Every generated app is checked in a separate interpreter before it is offered.

![A generated dashboard: KPI cards, trend with forecast and breakdowns](docs/images/dash-overview-light.png)

### Lyst knows your data

Every dataset version is profiled on arrival. Lyst learns its grain, keys and column meaning, finds duplicates at
the grain, outliers, concentrated missing values, time-series gaps and stale data, and ranks what to check
before relying on it. Each dataset in Data carries a chip (**Trusted**, **N to fix**, **Contract broken**), and
its Overview shows those risks, what depends on it, and how it connects to other data: key candidates, shared
dimensions, and overlapping sources reconciled by their totals.

* **Data contracts.** Lyst drafts a contract from the data (columns, types, allowed values, what identifies a
  row). Once adopted, it is versioned and checked on every refresh. A breaking change upstream is flagged with
  the metrics, dashboards and saved analyses it affects, and reaches the owners' inbox once.
* **Metric lineage.** Each definition shows where its number comes from (source, freshness, contract), its
  versions with field-level changes, and what uses it. Retiring a definition can name a replacement that
  requests and terms then follow. A definitions health line in Brain lists duplicates, conflicting sources,
  ambiguous synonyms and stale definitions, each with its fix.
* **Search** across datasets, columns, metrics, terms, saved analyses, dashboards, documents and earlier
  answers, filtered by what you may see. Each result opens where it lives.

<table>
  <tr>
    <td width="50%"><img src="docs/images/dataset-trust.png" alt="A dataset's Overview: three risks to check before relying on it, ranked Fix first and Worth a look"><br><b>Before you rely on it</b>: ranked risks found in the data</td>
    <td width="50%"><img src="docs/images/dataset-contract.png" alt="The contract Lyst drafted for a dataset: columns, types, required flags and allowed values"><br><b>Contract</b>: drafted from the data, adopted in one click</td>
  </tr>
  <tr>
    <td colspan="2" align="center"><img src="docs/images/metric-lineage.png" alt="A metric definition: how it is calculated, where the number comes from, versions and retirement" width="60%"><br><b>Definition</b>: where the number comes from, versions and retirement</td>
  </tr>
</table>

### Sources that tell you what is wrong

Data › Datasets › **Sources** lists each connection with a health chip (**Healthy**, **Degraded**, **Failing**,
**Needs action**, **Not tested**). When something is wrong, the row says the one thing to do instead of the
usual details. A source's dialog shows the cause in plain words (sign-in, permissions, rate limit, time-out, the
source itself), the last 24 hours of operations, errors and retries, every table it feeds with its sync mode,
freshness, schedule and contract, and the credential's status (never the secret), with Test and Sync at hand.

<table>
  <tr>
    <td width="45%"><img src="docs/images/sources.png" alt="Sources: a degraded source with the action to take, and a healthy one"><br><b>Sources</b>: one chip and one action per connection</td>
    <td width="55%"><img src="docs/images/source-health.png" alt="A degraded source's dialog: the action, the cause, failures and retries, and the table it feeds"><br><b>A degraded source</b>: the action, the cause and what it feeds</td>
  </tr>
</table>

### What Lyst makes stays current

Reports (PDF briefings, workbooks, decks), dashboards and saved analyses share one registry with an owner,
versions, validation status, and the question, dataset versions and metric versions each was built from. Every
new data load and definition change re-assesses them as **current**, **stale** (newer data), **invalidated** (a
metric was redefined or retired) or **broken** (a dataset is gone, a contract breached, or checks failed), and the
owner hears once. Each asset comes with a repair proposal: dashboards refresh with the previous version kept,
reports rerun their question, saved analyses rerun and recompile, and an upstream cause says a person is needed.
Only a verified result becomes a new version; a failed repair restores the earlier state with the reason.

## What Lyst does

* **Answers and investigations.** Values with prior-period and year-ago comparisons, breakdowns, trends,
  anomalies, forecasts with backtests, "why" questions with exact rate and mix attribution, deep investigations
  that decide between competing hypotheses by computation, staffing scenarios under uncertainty, and event
  impact with stated assumptions.
* **Checks its own work.** Independent validation scaled to consequence (high-stakes answers are recomputed from
  raw rows in pandas), join fan-out and stale-source detection, ambiguous terms surfaced instead of guessed, and
  model wording verified against the computed numbers.
* **Gets faster with use.** A verified analysis compiles into a typed, versioned saved analysis. Matching
  requests reuse it, and a changed data layout or metric definition marks it out of date until it is verified again.
* **Maintains its own output.** One registry for reports, dashboards and saved analyses, re-assessed on every
  data and definition change, with verified repair and every version kept.
* **Runs work durably.** Leases, checkpoints and resume after a crash; parallel steps; pause, cancel, retry and
  budgets; approval-gated schedules and monitors with an inbox and webhooks.
* **Keeps governed knowledge.** Versioned metrics and a glossary, lineage from metric to column, documents with
  citations, semantic recall when an embedding model is configured, and layered memory with trust levels.
* **Extends itself, under control.** When a method is missing, the Capability Foundry drafts a skill, tests it in
  a sandbox with adversarial cases, and registers it only after another role approves.
* **Connects to your data.** CSV, TSV and Parquet uploads, PostgreSQL, SQLite, REST JSON and files at a URL,
  with incremental syncs. Every connector failure is classified (auth, permission, rate limit, timeout, schema,
  and so on) with the action that fixes it, transient failures retry with backoff, each connection reports its
  health, and added, removed or retyped columns are caught on every sync (a retyped column triggers a full
  reload). A versioned API and an MCP server at `/mcp` expose the same capabilities.

The [capability matrix](docs/CAPABILITY_MATRIX.md) lists each capability with its state and the test that proves
it, and [known gaps](docs/GAPS.md) lists what is missing.

## Models

Lyst calls models through OpenRouter on a gateway ported from Acumane's: workload routing, admission, adaptive
limits, retries with fallback, and the cost of every call. Each workspace picks a level, built daily from
OpenRouter's live catalog:

| Level | Models | Use it for |
|---|---|---|
| **Economy** | Each provider's fast line (Claude Haiku, GPT mini, Gemini Flash Lite, …) | High volume, simple questions |
| **Balanced** (default) | The mid lines (Claude Sonnet, Gemini Flash, Mistral Large, …) | Most work |
| **Frontier** | The flagships (Claude Opus, the newest GPT, Gemini Pro, Grok) | Hard investigations |

An administrator sets the default level and a ceiling; a workspace admin can also choose one specific model.
Settings › Models shows each level's models, OpenRouter's prices, the cost per call and per 1,000 calls, and
30-day spend. Quick steps such as classification run with thinking off, and an empty reply fails over to the
next model, so the computed answer always stands.

A local OpenAI-compatible server (Ollama, llama.cpp, vLLM) works instead: set `LYST_LLM_BASE_URL` and
`LYST_LLM_MODEL`. Details are in [docs/MODELS.md](docs/MODELS.md).

## Security and secrets

* **Credentials are never configuration.** The OpenRouter key, database passwords and API tokens are entered
  once in **Settings › Connections**, tested, sealed with AES-256-GCM envelope encryption, and never
  shown again. `.env` files refuse secret-shaped names. OpenBao is supported for enterprises.
* **The master key lives outside the data**: in the OS credential store (the Keychain on a Mac), a mounted
  secret file, or OpenBao, and never beside the database. `lyst init` sets it up and `lyst rotate-master-key`
  re-keys everything.
* **Encrypted at rest.** Every workspace is its own encrypted DuckDB file with a derived key.
* **Governed access.** Roles, workspace isolation, a read-only SQL guard with column and row policies,
  redaction before text reaches an external model (dataset rows are never sent), OIDC sign-in, and a
  hash-chained credential audit trail.

The threat model, autonomy levels and residual risks are in [docs/SECURITY.md](docs/SECURITY.md).

## Commands

| Command | What it does |
|---|---|
| `lyst start [--demo] [--no-browser]` | Everything in one terminal: encryption, the engine and the interface |
| `lyst init-demo` | Create the Northwind demo workspace and a development token |
| `lyst serve` | Run the API and workers (`http://127.0.0.1:8770`) |
| `lyst ui` | Run the Streamlit interface (`http://lyst.localhost:8501`, loopback only by default) |
| `lyst token EMAIL [--owner-of WS]` | Issue an API token, optionally creating a workspace with that user as owner |
| `lyst init [--recovery]` | Initialise encryption, or restore a saved master key |
| `lyst rotate-master-key` | Re-key every database and re-wrap every secret (engine stopped) |
| `lyst backup DEST` | Consistent, encrypted backup, also while the engine is running |
| `lyst restore SRC` | Verified restore into `LYST_HOME` (engine stopped) |
| `lyst prune` | Apply retention to events, calls, query history and notifications |
| `lyst eval` | Score the analytical question bank against independent answers |

Prefix each with `uv run` in a checkout. Ports move with `LYST_PORT` and `LYST_UI_PORT`, and data lives in
`LYST_HOME` (default `./var`). Other settings are environment variables documented at the top of
[`src/lyst/config.py`](src/lyst/config.py).

## Development

```bash
uv run pytest                        # the full suite; the PostgreSQL connector test uses a local server
uv run lyst eval                     # 19 analytical questions scored against independently computed answers
uv run ruff check src tests scripts  # lint
uv run python scripts/licenses.py    # regenerate docs/SBOM.md and fail on a disallowed license
```

CI runs all four on every pull request and every push to main.

| Path | What |
|---|---|
| `src/lyst/` | The product: store, security, secrets, data, semantic, analyst, runtime, llm, api, ui, dashkit |
| `tests/` | Unit and acceptance tests, including the adversarial scenarios |
| `docs/` | Architecture, models, security, capability matrix, excellence registers, decisions |
| `scripts/` | License gate, load test and benchmarks |
| `site/` | The marketing website (a separate build; see its README) |

To run on a server, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) (Docker Compose, non-root, read-only).

## Documents

[Architecture](docs/ARCHITECTURE.md) · [Data platform](docs/PLATFORM.md) · [Capability matrix](docs/CAPABILITY_MATRIX.md) ·
[Models](docs/MODELS.md) · [Security](docs/SECURITY.md) · [Connectors](docs/CONNECTORS.md) ·
[Skills](docs/SKILLS.md) · [DuckDB design](docs/DUCKDB.md) · [Deployment](docs/DEPLOYMENT.md) ·
[Performance](docs/PERFORMANCE.md) · [UX strategy](docs/UX_STRATEGY.md) · [Product excellence](docs/PRODUCT_EXCELLENCE.md) ·
[Engineering excellence](docs/ENGINEERING_EXCELLENCE.md) · [Known gaps](docs/GAPS.md) ·
[Roadmap](docs/ROADMAP.md) · [Acumane audit](docs/ACUMANE_AUDIT.md) · [SBOM](docs/SBOM.md) ·
[Decisions](docs/decisions/)

## License

[GNU AGPL-3.0-only](LICENSE). If you run a modified Lyst as a network service, you must offer its users the
modified source (AGPL section 13). Every dependency is AGPL-compatible ([SBOM](docs/SBOM.md)). Vendored analytics
in `src/lyst/analytics/` come from the same author's ai-analytics project and are relicensed with Lyst.
