# Models and the gateway

Lyst calls models the way Acumane does (its AcuRout gateway, `docs/ACUROUT.md` in Acumane), ported to
Python in `src/lyst/llm/`. OpenRouter is the main provider; a local OpenAI-compatible server is the
alternative; with neither, Lyst runs on rules and templates and every number still comes from SQL.

## Configuring

| Variable | Meaning |
|---|---|
| OpenRouter key | Connected in **Settings › Secrets & Connections › Installation** (tested with `GET /key`, sealed, never shown again). Unattended deployments may mount `LYST_OPENROUTER_API_KEY` under `/run/secrets` as a one-time seed; never a `.env` file. Use a key of its own for Lyst |
| `LYST_LLM_TIER` | `economy`, `balanced` (default) or `frontier`: the model list a call walks, copied from Acumane's published catalog (`src/lyst/llm/tiers.json`, catalog version 4) |
| `LYST_LLM_MODEL` | Pin one slug; it is used alone |
| `LYST_LLM_BASE_URL`, `LYST_LLM_MODEL` | A local server instead (Ollama `http://localhost:11434/v1`, llama.cpp, vLLM) |
| `LYST_EMBED_MODEL` | An embedding model at the same provider (OpenRouter slug or a local model name). Turns on semantic search over documents and memory; the same external-provider policy and redaction apply |
| `LYST_EXTERNAL_LLM_DEFAULT` | Whether new workspaces may use an external provider (on by default with OpenRouter) |
| `LYST_AI_*` | Admission limits, same names and defaults as Acumane's `AI_GATEWAY_*` |

An owner or admin can turn external providers off for a workspace in Settings.

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
analyst end to end on an OpenRouter-configured workspace against the stub. No call has been made to the
real OpenRouter from Lyst yet: that needs a Lyst key.
