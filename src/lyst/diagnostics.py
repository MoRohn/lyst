# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Diagnostics: is this installation healthy, and does it keep its profile's promises?

Each check returns ``{id, area, status: ok|warn|fail|skip, detail, fix}``; ``run`` adds an overall status (the worst
one) and a support summary safe to share: versions, settings that matter and the checks, never a secret, a token,
a file path outside Lyst's home or a row of data. ``lyst doctor`` prints it; ``GET /v1/diagnostics`` returns it to
installation administrators.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import environment, profiles, sandbox
from .launch import cmd
from .store.schema import CONTROL, WORKSPACE

ORDER = {"ok": 0, "skip": 0, "warn": 1, "fail": 2}


def _c(cid: str, area: str, status: str, detail: str, fix: str | None = None) -> dict[str, Any]:
    return {"id": cid, "area": area, "status": status, "detail": detail, "fix": fix}


def _probe(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    import httpx  # noqa: PLC0415

    try:
        r = httpx.get(url, timeout=timeout)
        return r.status_code < 500, f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        return False, type(exc).__name__


def checks(lyst, *, network: bool = True) -> list[dict[str, Any]]:
    s = lyst.settings
    out: list[dict[str, Any]] = []
    prof = profiles.name()
    rules = profiles.rules()
    # -- start: the same preflight as `lyst start --check` (Python, data folder, key, ports), reused, not repeated.
    # Its "database"/"already running" checks are about another process holding the files; here this process holds
    # them, so they are left out. Local paths stay out of the report.
    from .launch import preflight  # noqa: PLC0415

    for c in preflight(Path(s.home), api_port=int(os.environ.get("LYST_PORT", "8770")),
                       ui_port=int(os.environ.get("LYST_UI_PORT", "8501"))):
        if c.name in ("database", "already running"):
            continue
        detail = "writable" if c.name == "data folder" and c.ok else c.detail.replace(str(s.home), "LYST_HOME")
        out.append(_c(c.name.replace(" ", "_"), "start", "ok" if c.ok else "fail", detail, c.fix or None))
    env = environment.fingerprint()
    missing = [p for p, ver in env["packages"].items() if ver is None and p != "lyst"]
    out.append(_c("packages", "platform", "ok" if not missing else "fail",
                  ", ".join(f"{p} {ver}" for p, ver in env["packages"].items() if ver) or "none",
                  f"install {', '.join(missing)}" if missing else None))
    # -- storage
    home = Path(s.home)
    usage = shutil.disk_usage(home)
    free_gb = usage.free / 1e9
    out.append(_c("disk", "storage", "ok" if free_gb > 5 else "warn" if free_gb > 1 else "fail",
                  f"{free_gb:.1f} GB free where Lyst keeps its data", None if free_gb > 5 else "free disk space or move LYST_HOME"))
    out.append(_c("encryption", "security", "ok" if s.encrypt else "fail",
                  f"databases encrypted at rest; master key from {s.master_key_source or 'unknown'}" if s.encrypt
                  else "encryption at rest is off (LYST_ENCRYPT=0)", None if s.encrypt else "unset LYST_ENCRYPT; it is for tests only"))
    known_ctl, known_ws = max(v for v, _ in CONTROL), {v for v, _ in WORKSPACE}
    ctl = lyst.stores.control
    have_ctl = set(r["version"] for r in ctl.query("SELECT version FROM _migrations"))
    pending_ctl = sorted({v for v, _ in CONTROL} - have_ctl)
    behind = []
    sizes = 0
    for w in ctl.query("SELECT id FROM workspaces ORDER BY id"):
        have = {r["version"] for r in lyst.stores.workspace(w["id"]).query("SELECT version FROM _migrations")}
        if known_ws - have:
            behind.append(w["id"])
        f = s.workspace_db(w["id"])
        sizes += f.stat().st_size if f.exists() else 0
    out.append(_c("schema", "storage", "ok" if not pending_ctl and not behind else "fail",
                  f"control schema {known_ctl}, {len(known_ws)} workspace migrations applied everywhere" if not pending_ctl and not behind
                  else f"pending control migrations {pending_ctl}; workspaces behind: {behind}", f"restart Lyst ({cmd('stop')}, then {cmd('start')}) to migrate"))
    wal = sum(p.stat().st_size for p in home.rglob("*.wal")) if home.exists() else 0
    out.append(_c("database_size", "storage", "ok" if wal < 512e6 else "warn",
                  f"workspaces {sizes / 1e6:.1f} MB, write-ahead logs {wal / 1e6:.1f} MB",
                  None if wal < 512e6 else "large WAL files: let the engine checkpoint (restart) or check for a stuck writer"))
    backups = sorted((home / "backups").glob("*/manifest.json"), key=lambda p: p.stat().st_mtime) if (home / "backups").exists() else []
    if backups:
        age = (time.time() - backups[-1].stat().st_mtime) / 86400
        out.append(_c("backup", "storage", "ok" if age < 2 else "warn", f"last backup {age:.1f} days ago",
                      None if age < 2 else f"Settings › Advanced › Back up now, and schedule {cmd('backup <folder>')} daily"))
    else:
        out.append(_c("backup", "storage", "warn", "no backup found in the data folder",
                      f"Settings › Advanced › Back up now (or {cmd('backup <folder>')}), then schedule it daily"))
    # -- security
    chain = lyst.credentials.verify_audit_chain()
    out.append(_c("credential_audit", "security", "ok" if chain["ok"] else "fail",
                  "the credential history chain is intact" if chain["ok"] else f"the credential history was altered at {chain.get('broken_at')}",
                  None if chain["ok"] else "investigate: restore the control database from a backup taken before the change"))
    host = os.environ.get("LYST_HOST", "127.0.0.1")
    exposed = host not in ("127.0.0.1", "localhost", "::1")
    oidc = bool(os.environ.get("LYST_OIDC_ISSUER")) or bool(ctl.scalar(
        "SELECT count(*) FROM credentials WHERE provider = 'oidc' AND status = 'active'") or 0)
    if exposed and not oidc:
        out.append(_c("exposure", "security", "fail", f"the API binds {host} without an identity provider",
                      "configure OIDC (Settings › Connections) or bind 127.0.0.1"))
    elif rules.get("loopback") and exposed:
        out.append(_c("exposure", "security", "warn", f"profile {prof} expects loopback, but the API binds {host}",
                      "set LYST_HOST=127.0.0.1 or choose the server profile"))
    else:
        out.append(_c("exposure", "security", "ok", f"API binds {host}" + (" with OIDC" if oidc else "")))
    if rules.get("oidc") and not oidc:
        out.append(_c("identity", "security", "fail", "the server profile needs an identity provider", "configure OIDC"))
    kind, why = sandbox.backend()
    out.append(_c("sandbox", "security", "ok" if kind else "warn", why, None if kind else "install bubblewrap (Linux) to enable the Python workbench"))
    # -- models
    llm = s.llm
    if profiles.offline():
        bad = llm.provider == "openrouter" or (llm.base_url or "").startswith("https://openrouter.ai")
        out.append(_c("offline_model", "models", "fail" if bad else "ok",
                      "offline profile, but the model provider is OpenRouter" if bad else f"local model at {llm.base_url or 'unset'}",
                      "set LYST_LLM_BASE_URL to the local model server (e.g. http://localhost:11434/v1) and LYST_LLM_MODEL" if bad else None))
    if llm.provider == "local":
        if not (llm.base_url and llm.model):
            out.append(_c("model", "models", "fail", "the local model is not configured", "set LYST_LLM_BASE_URL and LYST_LLM_MODEL"))
        elif network:
            ok, how = _probe(llm.base_url.rstrip("/") + "/models")
            out.append(_c("model", "models", "ok" if ok else "fail", f"local model server {llm.base_url}: {how}",
                          None if ok else "start the model server (for Ollama: `ollama serve`) or fix LYST_LLM_BASE_URL"))
        else:
            out.append(_c("model", "models", "skip", "network checks skipped"))
    else:
        c = lyst.credentials.resolve("openrouter")
        out.append(_c("model", "models", "ok" if c else "warn", "OpenRouter key connected" if c else "no OpenRouter key yet",
                      None if c else "connect it in Settings › Connections; analyses that need a model wait until then"))
    # -- runtime
    stuck = ctl.scalar("SELECT count(*) FROM tasks WHERE status = 'running' AND lease_until < now() - INTERVAL 10 MINUTE") or 0
    out.append(_c("runtime", "runtime", "ok" if not stuck else "warn", f"{stuck} task(s) running past their lease" if stuck else "no stalled tasks",
                  f"restart Lyst ({cmd('stop')}, then {cmd('start')}); it recovers orphaned tasks on start" if stuck else None))
    dead = ctl.scalar("SELECT count(*) FROM webhook_deliveries WHERE status = 'dead'") or 0
    paused = ctl.scalar("SELECT count(*) FROM webhook_subscriptions WHERE NOT active AND disabled_reason IS NOT NULL") or 0
    out.append(_c("webhooks", "runtime", "ok" if not dead and not paused else "warn",
                  f"{dead} dead deliveries, {paused} paused subscription(s)" if dead or paused else "deliveries healthy",
                  "check the receivers, then redeliver or re-enable" if dead or paused else None))
    last_audit = ctl.scalar("SELECT max(id) FROM audit_log") or 0
    cursor = int(ctl.scalar("SELECT position FROM event_cursors WHERE name = 'audit'") or 0)
    lag = last_audit - cursor
    out.append(_c("events", "runtime", "ok" if lag < 5000 else "warn", f"event collector {lag} audit lines behind",
                  None if lag < 5000 else "the worker tick is not running; check the engine's workers"))
    # -- profile promises
    if rules.get("no_internet"):
        internet = [r["kind"] for r in ctl.query("SELECT DISTINCT kind FROM connections")
                    if r["kind"] in ("http_file", "salesforce", "hubspot", "stripe", "bigquery", "snowflake", "google_sheets")]
        out.append(_c("offline_connectors", "profile", "ok" if not internet else "warn",
                      "no internet connectors" if not internet else f"internet connectors configured: {', '.join(internet)}",
                      "remove them or point them at mirrors inside the network" if internet else None))
        if os.environ.get("LYST_CATALOG_AUTO", "1") != "0":
            out.append(_c("offline_catalog", "profile", "fail", "the model catalog refresh calls openrouter.ai", "set LYST_CATALOG_AUTO=0"))
    if rules.get("self_contained"):
        inside = Path(os.path.realpath(sys.executable)).is_relative_to(home.parent) if hasattr(Path, "is_relative_to") else False
        out.append(_c("portable", "profile", "ok" if inside else "warn",
                      "Python and data live in one folder" if inside else "the data folder is self-contained; Python is installed elsewhere",
                      None if inside else "for a fully portable folder, create the virtual environment beside LYST_HOME"))
    return out


def run(lyst, *, network: bool = True) -> dict[str, Any]:
    results = checks(lyst, network=network)
    worst = max(results, key=lambda c: ORDER[c["status"]])["status"] if results else "ok"
    status = {"ok": "healthy", "skip": "healthy", "warn": "attention", "fail": "unhealthy"}[worst]
    env = environment.fingerprint()
    return {"status": status, "profile": profiles.name(), "checked_at": dt.datetime.now(dt.UTC).isoformat(),
            "summary": {"fail": sum(c["status"] == "fail" for c in results), "warn": sum(c["status"] == "warn" for c in results),
                        "ok": sum(c["status"] == "ok" for c in results)},
            "environment": {k: env[k] for k in ("python", "platform", "packages", "lock", "digest")},
            "settings": {"encrypt": lyst.settings.encrypt, "provider": lyst.settings.llm.provider, "workers": lyst.settings.workers,
                         "offline": profiles.offline(), "host": os.environ.get("LYST_HOST", "127.0.0.1")},
            "checks": results}


def render(report: dict[str, Any]) -> str:
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}
    lines = [f"Lyst doctor: {report['status']} (profile {report['profile']}, Python {report['environment']['python']})", ""]
    for c in report["checks"]:
        lines.append(f"  [{mark[c['status']]}] {c['area']:<8} {c['detail']}")
        if c["fix"] and c["status"] in ("warn", "fail"):
            lines.append(f"           fix: {c['fix']}")
    s = report["summary"]
    lines += ["", f"{s['fail']} failing, {s['warn']} to look at, {s['ok']} fine."]
    return "\n".join(lines)
