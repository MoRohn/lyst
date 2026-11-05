# Deployment and operations

## Single machine with Docker Compose

```bash
cp .env.example .env            # non-secret settings only
python -c 'import secrets;print(secrets.token_hex(32))' > secrets/LYST_MASTER_KEY   # the trust anchor; keep a copy offline
docker compose up -d --build    # engine (API + workers) on 127.0.0.1:8770, UI on :8501
docker compose exec engine lyst token you@example.com --owner-of acme   # first owner token, until OIDC is on
```

Then open the UI, sign in with that token and connect the OpenRouter key under **Settings › Secrets &
Connections › Installation**: it is tested, sealed and never shown again. (An unattended deployment may instead
mount `secrets/LYST_OPENROUTER_API_KEY`; it is imported once as the installation credential.)

The master key is mounted at `/run/secrets` (the Compose file uses Docker secrets from `./secrets/`, which is
gitignored); with Kubernetes mount a Secret there, or point `LYST_SECRETS_DIR` elsewhere. Never put it in the
data volume, the image or a backup. See `docs/SECURITY.md`, *Secrets and credentials*.

Both containers run as a non-root user with a read-only root filesystem, no Linux capabilities and
`no-new-privileges`. Only the engine mounts the data volume; the UI holds no data and reaches the engine over
HTTP. Put a TLS-terminating reverse proxy (Caddy, nginx, Traefik) in front of both ports; Lyst itself serves
plain HTTP on the internal network.

The Compose file has not yet been run on this machine (Docker was not available while it was written); the
image build and the services are checked in CI once it exists.

## A workstation or a single server without containers

`uv run lyst start` runs the engine and the interface from one terminal and opens http://lyst.localhost:8501
(`--demo` seeds the demo workspace; Ctrl-C stops both). With no master key anywhere the engine is in **setup mode** and the UI's
first page initialises encryption from the engine host (loopback only), generating the key into the OS
credential store and showing a recovery copy once. `lyst init` does the same from a terminal; `lyst init
--recovery` restores a saved key on a rebuilt machine. Headless Linux without Secret Service: mount the key
file or inject `LYST_MASTER_KEY` from your service manager (systemd `LoadCredential=` works well).

## Enterprise: OpenBao

Set `LYST_OPENBAO_URL` (and `LYST_OPENBAO_MOUNT`, `LYST_OPENBAO_PREFIX`, `LYST_OPENBAO_NAMESPACE` as needed) and
give Lyst an AppRole (`LYST_OPENBAO_ROLE_ID` plus `LYST_OPENBAO_SECRET_ID` as a mounted secret) or a token
(`LYST_OPENBAO_TOKEN`, mounted). Credentials are then kept in OpenBao's KV v2 engine; the master key may be
served from `<prefix>/master-key`, or still mounted. Policy needed: read and write under
`<mount>/data/<prefix>/*`, delete under `<mount>/metadata/<prefix>/*`, read `sys/health`. The same Settings
page works unchanged.

## Master-key rotation and recovery

`lyst rotate-master-key` with the engine stopped: every database is copied under the new key and every stored
secret re-wrapped, then the files are swapped and the credential store updated (for a mounted or injected key
the new value is printed for the operator to place). Previous files stay as `*.pre-rotation` until removed.
Backups taken before a rotation need the key they were taken with. Without the master key an installation is
unrecoverable: that is the design, so keep the recovery copy where a compromised server cannot reach it.

## Sign-in

Connect the sign-in provider in Settings › Secrets & Connections › Installation (issuer, client id, client
secret), or set the `LYST_OIDC_*` variables (`.env.example`) with the client secret mounted as
`LYST_OIDC_CLIENT_SECRET`. Any OIDC provider works: Keycloak, Authentik, Dex, Zitadel or a
commercial IdP. Lyst uses the authorization code flow with PKCE, verifies the ID token against the provider's
JWKS (issuer, audience, expiry, nonce), requires a verified email, optionally restricts email domains, and
issues a session token that expires after `LYST_SESSION_HOURS` (12). Signing in creates the user but grants
no workspace: an admin adds members by email in Settings or `POST /v1/workspaces/{ws}/members`.

Without OIDC, `lyst token <email>` issues a bearer token (optionally `--hours` and `--owner-of <workspace>`),
and the UI asks for it.

## Logs and metrics

Logs are JSON lines on stderr (`LYST_LOG_FORMAT=text` for a terminal) with `request_id` and `task_id`. Every
API response carries `X-Request-Id`; pass one in to correlate with a caller's logs.

`GET /metrics` is Prometheus text: `lyst_http_requests_total`, `lyst_http_request_seconds`, `lyst_tasks_total`
by final status, `lyst_step_seconds` by capability, `lyst_model_calls_total` by outcome and pool,
`lyst_model_cost_usd_total`, `lyst_tasks_queued`, `lyst_model_inflight`, `lyst_workers`. `GET /health` returns
200 while the control database answers. It answers in setup mode too. Set `LYST_METRICS_TOKEN` to require scrapes to send it as `Authorization: Bearer <token>` (Prometheus: `authorization: {credentials: ...}`); without it the endpoint is open, so keep the API port private.

Suggested alerts (thresholds to set from your own baseline): task failure rate over 10% in 15 minutes,
`lyst_tasks_queued` growing for 10 minutes, p95 `lyst_http_request_seconds` over 2 s, any `credits` outcome
in `lyst_model_calls_total`.

## Backup and restore

```bash
lyst backup /backups/lyst-$(date +%F)      # online, consistent, still encrypted
lyst restore /backups/lyst-2026-11-04      # with the engine stopped; checks the key opens every file first
lyst prune                                  # retention: events 90 days, audit 400 (LYST_RETENTION_DAYS_*)
```

`lyst backup` copies every database on its writer thread in one snapshot (`COPY FROM DATABASE` into a file
encrypted with the same derived key), plus `artifacts/` and `dashboards/`, and writes `manifest.json` with
hashes. The engine keeps serving during a backup. A backup is unreadable without the master key: keep
`LYST_MASTER_KEY` (or the keyfile) separately. `lyst restore` refuses a backup whose files do not open with
the current key. The worker loop applies retention once a day; `lyst prune` runs it on demand.

## Tracing

Set `LYST_OTEL_ENDPOINT` (an OTLP/HTTP collector, for example `http://otel-collector:4318`) and Lyst exports
spans: `http` per request (route, status, request id), `task` per task, `step:<capability>` per step with its
outcome, `model.call` per model call. Without the variable, spans stay in process at no cost.

## Upgrades

Schema migrations run automatically at start (`src/lyst/store/schema.py`); they only add. Back up before an
upgrade, then `docker compose pull && docker compose up -d`.
