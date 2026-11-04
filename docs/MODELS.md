# Models and the gateway

Lyst calls models the way Acumane does (its AcuRout gateway, `docs/ACUROUT.md` in Acumane), ported to
Python in `src/lyst/llm/`. OpenRouter is the main provider; a local OpenAI-compatible server is the
alternative; with neither, Lyst runs on rules and templates and every number still comes from SQL.

## Configuring

| Variable | Meaning |
|---|---|
| OpenRouter key | Connected in **Settings › Secrets & Connections › Installation** (tested with `GET /key`, sealed, never shown again). Unattended deployments may mount `LYST_OPENROUTER_API_KEY` under `/run/secrets` as a one-time seed; never a `.env` file. Use a key of its own for Lyst |
| `LYST_LLM_TIER` | `economy`, `balanced` (default) or `frontier`: the installation's default level (also set in Settings › Installation) |
| `LYST_LLM_MAX_TIER` | The highest level a workspace may choose (default `frontier`); it also caps a workspace's specific model at that level's dearest model |
| `LYST_LLM_MODEL` | Pin one slug for every workspace; it is used alone |
| `LYST_CATALOG_AUTO` | `0` stops the daily background refresh of OpenRouter's model list (air-gapped installations) |
| `LYST_LLM_BASE_URL`, `LYST_LLM_MODEL` | A local server instead (Ollama `http://localhost:11434/v1`, llama.cpp, vLLM) |
| `LYST_EMBED_MODEL` | An embedding model at the same provider (OpenRouter slug or a local model name). Turns on semantic search over documents and memory; the same external-provider policy and redaction apply |
| `LYST_EXTERNAL_LLM_DEFAULT` | Whether new workspaces may use an external provider (on by default with OpenRouter) |
| `LYST_AI_*` | Admission limits, same names and defaults as Acumane's `AI_GATEWAY_*` |

An owner or admin can turn external providers off for a workspace in Settings.

## Levels, models and prices

Lyst builds its three levels the way Acumane does (`catalog-policy.ts`, policy `latest-flagship-2026-10`),
in `src/lyst/llm/catalog.py`, from OpenRouter's live `GET /api/v1/models` (public; no key is sent):

* **Economy**: each provider's fast line (Claude Haiku, GPT mini, Gemini Flash Lite, DeepSeek Flash,
  Mistral Small, Llama Maverick). **Balanced**: the mid lines (Claude Sonnet, the previous GPT, Gemini
  Flash, Grok's value line, DeepSeek Pro, Mistral Large). **Frontier**: the flagships (Claude Opus, the
  newest GPT, Gemini Pro, Grok).
* Only models that call tools, are not deprecated or expiring, have a sane published price and at least
  8,000 tokens of context are placed; the newest model of each line wins; one model per provider.
* A level is ordered by the cost of a typical call (800 input and 400 output tokens), cheapest first.
  Auto uses the first and falls back down the list; a specific model is used alone.

The release is validated (every level filled, one model per provider, economy < balanced < frontier
on the first model's price) before it is stored in `model_catalog` and used; a failed refresh keeps the
last good release, and `tiers.json` is the snapshot shipped with Lyst until the first refresh. Lyst
refreshes when the stored release is over a day old (in the background, when Settings is opened) and
on **Refresh models and prices from OpenRouter** in Settings › Installation, which writes the changes
("Balanced added …", "… price changed …") to the credential history.

**Quick calls.** Mapping a question to a metric (`classify`), extraction, JSON layouts (`structured`) and
short chat calls (an output limit of 1,024 tokens or less, such as rewording computed findings) run with
the model's thinking off, or at its lowest effort when it always thinks, using the reasoning details in
the catalog. Walking a level, models that cannot think less go last. If a provider refuses the lower
setting the catalog listed, Lyst remembers that for the process and moves to the next model, or retries
a pinned model without the setting. Interactive calls whose workload sets no routing prefer fast
endpoints, as chat does. Long answers and reasoning keep each model's default.

**Who chooses.** An installation administrator sets the default level, an optional pinned model, and
the highest level a workspace may choose. A workspace admin picks a level and optionally one specific
model from the ~250 OpenRouter models Lyst can use (any provider), within that ceiling. A workspace
whose choice is above a lowered ceiling falls back to the default.

**Cost information.** Settings › Inference shows each level's first model, its fallbacks, OpenRouter's
per-1M-token prices, the cost per call and per 1,000 calls, the spread across the level, and every
model's price. Estimates use the typical call until the workspace has 20 calls in 30 days, then its own
average tokens per call (and calls per question). Model usage shows spend for the last 30 days and,
per call, the level it ran under and the cost OpenRouter reported (`usage.cost`), or the catalog price
when a provider did not report one.

## What every call goes through

1. **Workload and pool.** The caller names the workload (`chat`, `classify`, `long_form`, …); it sets the
   pool (interactive or background), the OpenRouter routing and the default output limit, as in Acumane's
   `WORKLOAD_ROUTING`: chat and agents prefer p50 ≤ 2 s and p90 ≤ 5 s latency, long answers prefer 40
   tokens/s; extraction, classification and enrichment keep OpenRouter's default price-weighted balancing.
   Lyst never sends `sort`, which would turn OpenRouter's load balancing off.
2. **Admission.** Total in flight ≤ 48, interactive pool ≤ 32, background ≤ 12 and never inside the
   16-call interactive reserve, 8 interactive and 3 background calls per workspace, 1M estimated tokens in
   flight (250K per workspace). Waiters queue (64 interactive, 32 background) up to 8 s or 45 s; then a
   person gets "busy, try again" and a background step is deferred.
3. **Adaptive limit per model and pool.** Starts at half the pool ceiling, +1 after a run of healthy
   calls, halves on 429/5xx/timeout/network failure (at most every 5 s); `Retry-After` pauses that model
   for up to 60 s while others keep running.
4. **Retries and fallback.** Jittered backoff (250 ms doubling to 4 s) or `Retry-After`; 429 and 5xx retry
   3 times, then the next model in the tier; `Retry-After` over 10 s goes to the next model at once. 402
   by `limit_source`: in-flight budget waits and retries once; a call too big for the budget stops; no
   credit stops without trying other models.
5. **Request shape.** `usage.include` for exact cost, response healing for JSON, Lyst's referer and title.
6. **Accounting.** One `llm_calls` row per upstream request: workspace, task, workload, pool, model,
   OpenRouter's chosen provider, status, outcome, attempt, queue wait, latency, tokens and cost. Settings
   shows usage by workload and model, and the gateway's live state.

## What models see

The analyst asks a model for two things: choosing among approved metric names when rules found none, and
rewording computed findings. It sends metric names, definitions and findings, never dataset rows, and
redacts text first. A model's narrative that contains a number no query produced is discarded for the
template text (`unbacked_numbers`).

## Differences from Acumane

* Limits are counted in the engine process, not in Postgres leases: Lyst has one engine process per
  deployment. Several engine processes would need a shared lease store.
* Waiters are served first-come, not round-robin by customer; the per-workspace limit still stops one
  workspace from taking the pool.
* Not ported yet: the key-balance watch and admin alert emails (the `key_status` call exists, nothing
  schedules it), streaming, prompt caching hints.

## Verified

`tests/test_gateway.py` runs the rules against a stub OpenRouter (routing fields, retries, fallbacks, 402
cases, busy/deferred, reserve, per-workspace limit, adaptive limit) and `tests/test_llm.py` runs the
analyst end to end on an OpenRouter-configured workspace against the stub. Live calls to the real OpenRouter
are recorded below.

`tests/test_models.py` covers the level rules on a synthetic catalog, refusal of an invalid release,
stored releases and change reports, the ceiling, the workspace choice through the API, and the level and
catalog-priced cost on a recorded call. Live, 2026-11-04, against a copy of the local installation with
its OpenRouter key: the catalog refreshed (453 models, 251 usable); an Economy call went to Claude Haiku
5.5 and OpenRouter reported $0.00025 for 204 in / 459 out, exactly the catalog price; a pinned Claude
Opus 5.5 call reported $0.002864 for 136 in / 116 out, again exactly the catalog price.

Quick calls, live, 2026-11-04, on a fresh copy of the local installation (Balanced level, which starts with
Mistral Large 4, a model that thinks by default). This is one cold run of each version, so it shows the
size of the change, not a benchmark. Before: the first question took 23.6 s. The 600-token wording call
spent its budget thinking, was cut off, and was retried at 1,800 tokens (5.2 s + 17.1 s). After: 5.4 s.
Mistral's provider refused `effort: none` (1.0 s), and DeepSeek V4 Pro answered with thinking off
(2.9 s). Later questions took 1.3 s and 2.9 s.
