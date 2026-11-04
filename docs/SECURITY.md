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
| API ↔ engine services | services, which re-check permission per call | arguments from any client, including MCP |
| Engine ↔ source systems | Lyst's read-only connector session | the source's content (rows, documents) |
| Engine ↔ model providers | nothing: model output is advisory | all model output; all prompts are data |
| Engine ↔ skill sandbox | the engine | skill source and its output |
| Engine ↔ filesystem | `$LYST_HOME` | everything else (DuckDB external access is off) |

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
* The master key is a single secret. Keep it in a secret manager, not in `.env` on disk, and store it apart
  from backups. Key rotation is manual (restore under a new key is not yet automated).
* Lyst serves plain HTTP; terminate TLS in a reverse proxy.
* OIDC has been exercised against a stub provider only; run a real login against your IdP before go-live.
* Model providers see redacted metric names, definitions and computed findings. If even that is too much,
  keep external providers off and run a local model.
* There is no row-level security yet: column policies are the finest grain.

## Alignment

Controls follow OWASP ASVS themes (authentication, session management, access control, input validation,
cryptography at rest, logging) and the NIST AI RMF functions (govern: approvals and autonomy levels; map:
this document; measure: the evaluation suite and adversarial tests; manage: monitors, audit, retention).
