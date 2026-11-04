# Lyst waitlist service (optional)

A small FastAPI service that receives update sign-ups and design-partner applications from the
static Lyst website and stores them in DuckDB. GitHub Pages cannot receive form posts, so the site
talks to this service through one public URL set at build time (`PUBLIC_WAITLIST_ENDPOINT`). If that
variable is empty, the site shows a truthful "sign-up isn’t open" state instead of a form.

## What it does

| Concern | How |
| --- | --- |
| Endpoint | `POST /v1/registrations` (JSON) and `GET /healthz`. No read, list, docs or OpenAPI routes. |
| Validation | Server-side: email format and length, required consent, allow-listed role/interest/team size, field length caps, control-character stripping. Errors come back as `422 {"errors": {"field": "code"}}`. |
| Duplicates | Idempotent per (kind, email). New and existing addresses get the identical `200` response, so the API cannot be used to test membership. |
| Abuse | Origin allow-list (CORS and a server-side check), 8 KB body cap, JSON-only, hidden honeypot field, per-client sliding-window rate limit (default 5 per 10 minutes). |
| Single writer | One DuckDB connection per process, every statement serialized through a lock; run exactly one worker. DuckDB's file lock stops a second process from opening the database for writing. |
| Encryption at rest | Email, organization and message are encrypted with Fernet (AES-128-CBC + HMAC) using `LYST_WAITLIST_ENCRYPTION_KEY`. Duplicate detection uses a keyed HMAC of the email. Also put the database on an encrypted volume. |
| Consent | Each record stores `consent_scope` (`early_access_updates` or `design_partner_contact`), the consent text version and a UTC timestamp. |
| Removal and retention | `lyst-waitlist remove EMAIL` deletes every record for an address. `lyst-waitlist purge` deletes records older than `LYST_WAITLIST_RETENTION_DAYS` (default 730); run it from cron. |
| Logging | JSON lines on stdout with event, request id and a hashed client id. Emails and IPs are never logged. |
| DuckDB file | Never served. The service has no route that reads the database; only the operator CLI does. |

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `LYST_WAITLIST_ALLOWED_ORIGINS` | yes | Comma-separated site origins, e.g. `https://morohn.github.io`. |
| `LYST_WAITLIST_HASH_KEY` | yes | Random secret, 32+ characters (`openssl rand -hex 32`). Keys the email and client hashes. |
| `LYST_WAITLIST_ENCRYPTION_KEY` | yes | Fernet key from `lyst-waitlist genkey`. Keep a backup: without it, stored data cannot be read. |
| `LYST_WAITLIST_DB` | no | Database path. Default `data/waitlist.duckdb`. |
| `LYST_WAITLIST_RATE_LIMIT` | no | `count/seconds`, default `5/600`. |
| `LYST_WAITLIST_RETENTION_DAYS` | no | Default `730`. |
| `LYST_WAITLIST_TRUST_PROXY` | no | `1` behind a reverse proxy, to rate-limit on the first `X-Forwarded-For` hop. |
| `LYST_WAITLIST_ENV` | no | `development` generates throwaway keys and allows localhost origins. Never use it in production. |

Secrets belong in your host's secret store or environment, never in the repository or the website build.

## Run locally

```bash
cd site/waitlist
uv sync
LYST_WAITLIST_ENV=development uv run lyst-waitlist serve --port 8787
```

Then build the site against it, from `site/`:

```bash
PUBLIC_WAITLIST_ENDPOINT=http://localhost:8787 npm run build && npm run preview
```

Submit the form at http://localhost:4321/#learn-more and confirm the record:

```bash
LYST_WAITLIST_ENV=development uv run lyst-waitlist count
```

(In development mode the keys are regenerated on each start, so use real keys if you want to export
what you stored.)

## Deploy

Any host that runs one long-lived Python process with a persistent, encrypted disk works (a small VM,
Fly.io with a volume, Railway, a container on your own server). Serve it over HTTPS behind a reverse
proxy, set the variables above, and start:

```bash
uv run lyst-waitlist serve --host 0.0.0.0 --port 8787
```

Keep it to a single instance and a single worker. Back up the database file and the encryption key
separately.

## Tests

```bash
uv run pytest
```
