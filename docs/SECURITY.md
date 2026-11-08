# Security, governance and the threat model

Lyst is designed for defence in depth: permissions are checked in code at every service boundary, never
left to a prompt; storage is encrypted at rest; SQL is parsed and allow-listed; generated code runs in a
separate locked-down process; every consequential action is audited. This document lists the assets, the
trust boundaries, the threats considered, the controls in place with the tests that exercise them, and the
risks that remain. No certification is claimed.

## Assets

Workspace data (datasets, extracts, artifacts, dashboards), metric and knowledge definitions, connection
secrets, session tokens, the master encryption key, model API keys, audit history.

## Trust boundaries

| Boundary | Trusted side | Untrusted side |
|---|---|---|
| Browser ↔ API | the API after `Guard.authenticate` | every request |
| Browser ↔ chat and app routes (live answer, Lyst Apps) | the API after a chat ticket is verified (`lyst.tickets`) | the page, which holds only a 15-minute ticket for one workspace's chat routes and generating or opening an app, never the session |
| Page ↔ a Lyst App's renderer | the page: it sends the frame only the checked spec and the values Lyst resolved | the renderer, in an iframe with `sandbox="allow-scripts"` (opaque origin) under `default-src 'none'`: no network, storage, cookies or page access; it draws with textContent and SVG only |
| API ↔ engine services | services, which re-check permission per call | arguments from any client, including MCP |
| Engine ↔ source systems | Lyst's read-only connector session | the source's content (rows, documents) |
| Engine ↔ model providers | nothing: model output is advisory | all model output; all prompts are data |
| Engine ↔ skill sandbox | the engine | skill source and its output |
| Engine ↔ filesystem | `$LYST_HOME` | everything else (DuckDB external access is off) |

## Secrets and credentials

**Users manage connections. Lyst manages secrets. A dedicated security boundary protects the keys.**

The boundary is `src/lyst/secrets/`: a Secret Management Service that every consumer goes through and no
agent capability, generated skill or MCP tool can reach.

### Where a secret lives

* **Embedded (local and single-machine installations):** the value is sealed with AES-256-GCM under a random
  per-record data key; the data key is wrapped with a key-encryption key derived (HKDF-SHA256) from the
  installation's master key; the record's reference and key version are authenticated data, so a blob moved
  to another record or another key version does not open. Blobs live in the control database, which is itself
  encrypted; the master key is never in that database, its backups or the repository.
* **OpenBao (enterprise):** `LYST_OPENBAO_URL` keeps values in OpenBao's KV v2 engine under an opaque path per
  record. Lyst authenticates with an AppRole (`LYST_OPENBAO_ROLE_ID` + `LYST_OPENBAO_SECRET_ID`) or a token,
  each of which is itself a bootstrap anchor below, and holds only the policy it needs. The master key can be
  served from OpenBao too (`<prefix>/master-key`).

### The master key

The one secret Lyst cannot hold in itself. It comes from a **trust anchor outside the application's data**,
in this order: a mounted secret file `$LYST_SECRETS_DIR/LYST_MASTER_KEY` (Docker and Kubernetes secrets,
Vault Agent, External Secrets), OpenBao, the operating system's credential store (macOS Keychain, Windows
Credential Manager, Secret Service), then an environment variable injected by an orchestrator. Lyst **never
silently generates a key beside the database**: with no anchor the engine starts in *setup mode*, where the
first person, from the engine host (loopback only), initialises encryption in the browser. Lyst generates the
key into the OS credential store and shows a recovery copy exactly once; it never stores that copy. `lyst init`
is the same step from a terminal; `lyst init --recovery` puts a saved key back on a rebuilt machine. Unattended
deployments must provide a mounted file, OpenBao or the environment. The development keyfile exists only
behind `LYST_DEV_INSECURE_KEYFILE=1`.

Loss of the master key makes the databases and every stored secret unrecoverable, by design. Keep the
recovery copy offline and separate from backups.

**Rotation:** `lyst rotate-master-key` (engine stopped) copies every database under keys derived from a new
master key, re-wraps every data key inside the copy, and only then swaps the files and updates the anchor.
Anything that fails before the swap leaves the installation untouched; the previous files stay as
`*.pre-rotation` until the operator removes them. Credential replacement never needs a master-key rotation.

### Scopes, policy and administration

A credential is scoped to the **installation** (shared by every workspace: the OpenRouter key, the sign-in
provider), a **workspace** (database passwords, API tokens, OAuth clients, webhooks) or a **user**. Who may
connect, test, replace, revoke and read history is decided server-side: installation credentials by
installation administrators (the first user, who may add others), workspace credentials by that workspace's
owners, personal credentials by their owner. Connect, replace, revoke and policy changes also require recent
authentication (a session issued within 15 minutes; operator-issued CLI tokens are exempt).

Resolution is an explicit policy per provider, never a precedence rule users have to guess: an installation
credential is authoritative; a workspace or personal credential is used only where the installation policy
allows that provider to be overridden at that level (Settings › Secrets & Connections › Installation). One
user's credential is never used for another user's work.

### Entry, testing, replacement, revocation

Values are entered once in Settings (masked, `autocomplete=off`), validated with the provider's
least-privileged call (OpenRouter `GET /key`; PostgreSQL `SELECT 1` on a read-only connection; a REST `GET`;
an OIDC discovery document; an OAuth client-credentials grant) and sealed. A failed test stores nothing. After
that the value is never displayed, never returned by an API, never in Streamlit session state, never logged:
the broker registers every plaintext it releases with a redaction filter on the root logger, and audit details
are scrubbed too. Replacement validates the new value first, then deletes the old blob (only metadata and
history remain) and invalidates caches and leases. Revocation deletes the blob and says plainly what Lyst
could not do at the provider (OpenRouter keys are revoked in OpenRouter's dashboard). OAuth client credentials
are exchanged for short-lived access tokens that are cached only until shortly before expiry and never stored.

### The broker

Consumers do not read the store; they ask the service to *use* a credential for a purpose. Trusted in-process
consumers only: the model gateway (`inference`), connector workers (bound to the workspace that owns the
record), the OIDC client. Each distinct grant is an access decision in the audit chain, at most hourly per
credential and purpose. Connections made on the Data page store their secret fields through the service and
keep only a `credential_id`; the connector worker receives the plaintext for the duration of the sync.

### Audit

`credential_events` is a hash chain: every event carries the SHA-256 of the previous one over actor, time,
scope, action, outcome and detail, so a deleted or edited line breaks `verify_audit_chain()` (shown as
"Audit chain: intact/broken" in Settings). Creation, replacement, revocation, tests, access decisions, denied
attempts and policy changes are all events, with the actor and scope.

### Threat scenarios and their tests (`tests/test_credentials.py`)

| Threat | Control | Test |
|---|---|---|
| Cross-tenant access | Scope checks on every operation; connector secrets bound to the owning workspace | `test_cross_tenant_and_privilege_boundaries`, `test_connections_keep_their_secrets_…` |
| Privilege escalation | Server-side RBAC: owners for workspaces, installation admins for the installation; users cannot act in another's scope | `test_cross_tenant_and_privilege_boundaries` |
| Prompt injection, malicious generated code | No MCP or agent tool touches credentials; skills run in an isolated interpreter that cannot import `lyst.secrets` or read the environment | `test_agents_generated_code_and_mcp_have_no_route_to_the_store` |
| Leaked logs, traces, audit | Root-logger redaction of every released plaintext; scrubbed audit details | `test_logs_traces_and_audit_never_carry_plaintext` |
| Session-state and API exposure | The API never returns a value; the UI holds none | `test_connect_tests_first_and_the_value_never_comes_back` |
| Database compromise, backup leakage | Encrypted files plus per-record envelopes; nothing opens without the master key | `test_embedded_backend_envelope_…`, `test_master_key_loss_…` |
| Master-key loss | No silent keyfile; setup mode; recovery copy shown once | `test_master_key_loss_…`, `test_setup_mode_…` |
| Failed key rotation | Copy, re-wrap, then swap; rollback on failure | `test_master_key_rotation_…` |
| Concurrent replacement | Version check inside the write transaction; orphan blob removed | `test_concurrent_replacement_…` |
| Provider revocation failures | Reported honestly, never claimed | `test_revoke_invalidates_use_…` |
| Compromised connectors | A connector receives only its own workspace's secrets, at use time | `test_connections_keep_their_secrets_…` |
| Expired OAuth tokens | Short-lived tokens refreshed before expiry, never stored | `test_oauth_client_credentials_…` |
| Unauthorised secret testing | Testing requires scope administration | `test_cross_tenant_and_privilege_boundaries` |
| Workspace ownership changes | Rights follow the current membership; demotion or removal ends them | `test_ownership_changes_end_management_rights` |

Bootstrap anchors (`security/vault.py`) also cover `LYST_OPENROUTER_API_KEY` and `LYST_OIDC_CLIENT_SECRET` as
*seeds* for unattended deployments: a mounted value is imported once as an installation credential (audited
as such) so that afterwards there is one path for every consumer. `.env` files carry non-secret settings only;
a secret-shaped name there is ignored with a warning.

## Row policies

A workspace owner can restrict the rows a member sees: `POST /v1/workspaces/{ws}/datasets/{name}/row-policies`
with a column, a principal (a user id, or a role: viewer, editor, admin, owner) and the allowed values. The guard
rewrites every reference to that dataset into a filtered subquery before the SQL runs, so the Analyst, ad-hoc
SQL, the MCP `query` tool, monitors and dashboard extracts all obey it, whatever shape the query takes (aliases,
joins, CTEs, functions over the column). A policy naming the user wins over one naming their role; an empty
allowed list means no rows. Owners are unrestricted unless a policy names them. Every policy change is audited.

## Autonomy levels

| Class of work | Who may start it | Gate |
|---|---|---|
| Read-only analysis (ask, query, deliverables) | viewer and above | none beyond role |
| Loading data, building dashboards, proposing skills, monitors, schedules | editor and above | schedules and monitors need an admin's approval before they run |
| Defining metrics, glossary, column policies, members | admin and above | audited |
| Registering a skill (new executable capability) | owner, never the proposer | approval record |
| External model use | workspace setting by an admin | text redacted before it leaves; no dataset rows sent |
| Consequential external actions (writes to source systems, messages) | not implemented: connectors read only, notifications go to the workspace's own webhook | — |

No agent step can change a role, a policy or a setting: those are API operations with their own checks,
and no capability in the registry calls them.

## Threats and controls

| Threat | Control | Tested in |
|---|---|---|
| Cross-workspace access | One encrypted DuckDB file per workspace; dataset names resolve only inside the workspace; roles per workspace | `test_security.py` |
| Privilege escalation through a request or a model | Permissions checked in code per capability and service call; unknown actions denied; model output never selects permissions | `test_security.py`, `test_adversarial.py` |
| SQL injection / data exfiltration through user SQL | SQLGlot parse, single read statement, allow-listed shape, deny-listed functions, no schema-qualified names, column policies, row limit; DuckDB `enable_external_access=false` with the configuration locked | `test_sqlguard.py`, `test_store.py` |
| A browser script (an XSS) taking the live answer's credential | The page holds only a chat ticket: four routes (ask, resume, generate an app, open an app) in one workspace, 15 minutes, acting as its holder under every permission check, invalid after an engine restart; other routes refuse it; cross-origin only from `LYST_UI_ORIGINS` | `test_chat_tickets.py` |
| A model's wording seen before it is checked (streamed answers) | Streamed text passes a figure check as it is written (only backed figures are shown); the draft is kept only if it is exactly the stored answer, else removed | `test_live_drafts.py` |
| Prompt injection via documents, rows or requests | Retrieved text is fenced as data; numbers in model text must match computed values or the model text is discarded; approved definitions are the only source of metric logic | `test_adversarial.py` |
| Malicious or buggy skills | Static check (imports, builtins, attributes), separate `python -I` process, empty environment, CPU and file limits, wall-clock timeout, adversarial cases, approval by a non-proposer | `test_skills.py`, `test_foundry.py` |
| Connector misuse | Read-only transactions, identifier validation, secrets in their own column and never returned, files confined to the import root | `test_connectors.py` |
| Token theft | Tokens stored as SHA-256; sessions expire; logout revokes; OIDC with PKCE, JWKS verification, nonce, domain allow-list | `test_auth.py` |
| Data at rest | DuckDB AES-GCM encryption, one derived key per file; backups keep the encryption | `test_store.py`, `test_hardening.py` |
| Loss of work | Checkpointed steps, leases, resume without duplicate side effects | `test_scenarios.py`, `test_execution.py`, `test_studio.py` |
| Resource exhaustion | Task step and time budgets, model admission limits, row limits, upload size limit, sandbox limits | `test_runtime.py`, `test_gateway.py` |
| Silent wrong answers | Validation checks, independent recomputation, bridge closure, hypothesis verdicts, declared missing data, detrended correlation, join fan-out and stale-source warnings | `test_reasoning.py`, `test_adversarial.py` |
| Audit gaps | Every denial, mutation, approval, sync, build and model call is a row; redaction before storage; retention keeps audit longer than events | `test_security.py`, `test_hardening.py` |

## Residual risks and what to do about them

* The skill sandbox is process isolation with an allow-list, not a kernel sandbox. Run the engine in the
  provided container (read-only filesystem, no capabilities) and keep `skill.propose` to trusted roles.
* The master key is a single secret: keep it in the credential store or a secret manager, with its recovery
  copy apart from the database backups. `lyst rotate-master-key` re-keys everything with the engine stopped.
* Lyst serves plain HTTP; terminate TLS in a reverse proxy.
* OIDC has been exercised against a stub provider only; run a real login against your IdP before go-live.
* Model providers see redacted metric names, definitions and computed findings. If even that is too much,
  keep external providers off and run a local model.
* Row policies restrict rows by user or role inside the SQL guard; a dashboard built by a restricted user holds
  only that user's rows, and the manifest says who built it.

## Alignment

Controls follow OWASP ASVS themes (authentication, session management, access control, input validation,
cryptography at rest, logging) and the NIST AI RMF functions (govern: approvals and autonomy levels; map:
this document; measure: the evaluation suite and adversarial tests; manage: monitors, audit, retention).
