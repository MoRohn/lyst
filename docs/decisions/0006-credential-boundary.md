# 6. Users manage connections, Lyst manages secrets

Date: 2026-11-04. Status: accepted.

## Context

Early builds read secrets from the OS credential store, a mounted file or the environment, set with a CLI
command. That was secure but not a product: every person had to reach a terminal on the engine host, nothing
was tested before use, nothing could be replaced or revoked from the interface, and a `.env` file was the path
of least resistance. The owner asked for a solution that is more user-friendly and still highly secure, works
for every kind of user and deployment, and never uses `.env` for keys.

## Decision

A Secret Management Service (`src/lyst/secrets/`) is the only holder of credential values:

* embedded AES-256-GCM envelope encryption in the control database for local installations, OpenBao KV v2 for
  enterprises, behind one interface;
* the master key from a trust anchor outside the data (mounted file, OpenBao, OS credential store, injected
  variable), never generated silently beside the database; setup mode from the browser on first run;
* installation, workspace and user scopes with explicit per-provider override policy; server-side RBAC and
  recent-authentication checks for sensitive actions;
* entry once, masked, validated by a least-privileged provider call, never displayed or returned again;
* a broker that releases plaintext only to trusted in-process consumers by purpose, with hourly access
  decisions in a hash-chained audit;
* replacement that validates first and deletes the old blob; revocation that reports what the provider
  did not do; OAuth client credentials exchanged for short-lived tokens;
* root-key rotation as copy, re-wrap, swap.

## Consequences

Settings gains a Secrets & Connections page; `lyst secret` is gone, replaced by `lyst init` and
`lyst rotate-master-key`. Connections keep a `credential_id` instead of a secrets column. A developer who wants
the old keyfile convenience must say so (`LYST_DEV_INSECURE_KEYFILE=1`). A set of security tests
(`tests/test_credentials.py`) encodes the threat scenarios the design must keep holding against.
