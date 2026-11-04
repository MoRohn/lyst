# Deployment and operations

## Single machine with Docker Compose

```bash
cp .env.example .env            # set LYST_MASTER_KEY (python -c "import secrets;print(secrets.token_hex(32))")
docker compose up -d --build    # engine (API + workers) on 127.0.0.1:8770, UI on :8501
docker compose exec engine lyst token you@example.com --owner-of acme   # first owner token, until OIDC is on
```

Both containers run as a non-root user with a read-only root filesystem, no Linux capabilities and
`no-new-privileges`. Only the engine mounts the data volume; the UI holds no data and reaches the engine over
HTTP. Put a TLS-terminating reverse proxy (Caddy, nginx, Traefik) in front of both ports; Lyst itself serves
plain HTTP on the internal network.

The Compose file has not yet been run on this machine (Docker was not available while it was written); the
image build and the services are checked in CI once it exists.

## Sign-in

Set the `LYST_OIDC_*` variables (`.env.example`) to any OIDC provider: Keycloak, Authentik, Dex, Zitadel or a
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
200 while the control database answers.

Suggested alerts (thresholds to set from your own baseline): task failure rate over 10% in 15 minutes,
`lyst_tasks_queued` growing for 10 minutes, p95 `lyst_http_request_seconds` over 2 s, any `credits` outcome
in `lyst_model_calls_total`.

## Backup and restore

Everything lives in `$LYST_HOME` (`/var/lib/lyst` in the container): `control.duckdb`, `workspaces/*.duckdb`,
`artifacts/`, and the keyfile when `LYST_MASTER_KEY` is not set. A consistent backup is: stop the engine, copy
the directory, keep the master key separately (a backup without the key is unreadable). Restore is the
reverse. Online backups through DuckDB's `EXPORT DATABASE` on the writer thread are planned.

## Upgrades

Schema migrations run automatically at start (`src/lyst/store/schema.py`); they only add. Back up before an
upgrade, then `docker compose pull && docker compose up -d`.
