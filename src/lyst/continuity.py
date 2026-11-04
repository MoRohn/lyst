# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Operational continuity: atomic and scheduled backups, restore drills, retention, and measured recovery objectives.

* **Atomic backups.** A backup is written to ``<home>/backups/.partial-<stamp>`` and renamed to ``<stamp>`` only when
  every file and the manifest are written. A failure part way (a full disk, a lost volume) removes the partial copy
  and leaves earlier backups untouched, so a half-written backup is never mistaken for a good one.
* **Scheduled backups.** With ``LYST_BACKUP_EVERY_HOURS`` set, the worker takes one when the last good backup is older
  than the interval, runs a restore drill on it, and keeps the newest ``LYST_BACKUP_KEEP`` (default 7).
* **Restore drill.** Checks every checksum in the manifest, copies the databases to a scratch folder, opens each with
  the key derived from the current master key, and reads its schema version and table count, timing the whole run.
  That time is the measured recovery time for this installation's size; the age of the newest drilled backup is the
  recovery point. Both are reported against targets (``LYST_RPO_HOURS``, ``LYST_RTO_MINUTES``) instead of promised.
* **Key separation.** The master key is never written into a backup; the drill fails a backup that contains a key
  file, and the report says where the key must come from (its trust anchor) to read the backup.

High availability is not claimed: Lyst runs one engine per installation (decision 0001); continuity here means a
verified, recent backup and a measured time to restore it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("lyst.continuity")
STAMP = re.compile(r"^\d{8}T\d{6}Z$")
KEY_FILES = (".lyst-key", "master.key", "lyst-master-key")


def _home_backups(settings) -> Path:
    return Path(settings.home) / "backups"


def _status_path(settings) -> Path:
    return _home_backups(settings) / "continuity.json"


def status(settings) -> dict[str, Any]:
    p = _status_path(settings)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def _save_status(settings, **changes) -> dict[str, Any]:
    s = status(settings) | changes
    p = _status_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str))
    tmp.replace(p)
    return s


def backups(settings) -> list[dict[str, Any]]:
    """Complete backups (a manifest and a stamp-named folder), newest first."""
    root = _home_backups(settings)
    out = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        if d.is_dir() and STAMP.match(d.name) and (d / "manifest.json").exists():
            m = json.loads((d / "manifest.json").read_text())
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            out.append({"name": d.name, "path": str(d), "taken_at": m.get("taken_at"), "files": len(m.get("files", {})),
                        "bytes": size})
    return out


def take(lyst, *, actor=None) -> dict[str, Any]:
    """An atomic backup under ``<home>/backups/<stamp>``. Raises on failure after removing the partial copy."""
    settings = lyst.settings
    root = _home_backups(settings)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final, partial = root / stamp, root / f".partial-{stamp}"
    if final.exists():  # two in the same second: keep the first
        return {"path": str(final), **json.loads((final / "manifest.json").read_text())}
    started = time.perf_counter()
    try:
        manifest = lyst.stores.backup(partial)
        partial.rename(final)
    except Exception as exc:
        shutil.rmtree(partial, ignore_errors=True)
        _save_status(settings, last_failure=datetime.now(UTC).isoformat(timespec="seconds"),
                     last_error=f"{type(exc).__name__}: {exc}"[:300])
        log.error("backup failed and was discarded: %s", exc)
        raise
    seconds = round(time.perf_counter() - started, 3)
    _save_status(settings, last_success=manifest["taken_at"], last_backup=str(final), last_backup_seconds=seconds)
    if actor is not None:
        lyst.guard.audit(actor, None, "installation.backup", str(final), "ok", {"files": len(manifest["files"])})
    return {"path": str(final), "seconds": seconds, **manifest}


def drill(settings, path: str | Path) -> dict[str, Any]:
    """Restore drill: checksums, key separation, then copy and open every database with the current master key."""
    import duckdb

    from .store.db import derive_key

    src = Path(path)
    started = time.perf_counter()
    problems: list[str] = []
    try:
        manifest = json.loads((src / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        return {"path": str(src), "ok": False, "problems": [f"manifest unreadable: {exc}"], "seconds": 0.0}
    for rel, digest in manifest.get("files", {}).items():
        f = src / rel
        if not f.exists() or hashlib.sha256(f.read_bytes()).hexdigest() != digest:
            problems.append(f"{rel} does not match its checksum (damaged or altered)")
    for f in src.rglob("*"):
        if f.is_file() and f.name in KEY_FILES:
            problems.append(f"{f.relative_to(src)} looks like key material; a backup must not contain the master key")
    tables: dict[str, int] = {}
    if not problems:
        if getattr(settings, "master_key", None) is None and manifest.get("encrypted"):
            problems.append("no master key is available to open the backup (restore needs the key's trust anchor)")
        else:
            with tempfile.TemporaryDirectory(prefix="lyst-drill-") as tmp:
                for rel in manifest["files"]:
                    target = Path(tmp) / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src / rel, target)
                    name = "control" if rel == "control.duckdb" else "ws:" + Path(rel).stem
                    con = duckdb.connect()
                    quoted = str(target).replace("'", "''")
                    try:
                        if manifest.get("encrypted"):
                            con.execute(f"ATTACH '{quoted}' AS d (ENCRYPTION_KEY '{derive_key(settings.master_key, name)}', READ_ONLY)")
                        else:
                            con.execute(f"ATTACH '{quoted}' AS d (READ_ONLY)")
                        tables[rel] = con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = 'd'").fetchone()[0]
                        con.execute("SELECT max(version) FROM d._migrations").fetchone()
                    except duckdb.Error as exc:
                        problems.append(f"{rel} does not open with the current master key: {str(exc)[:160]}")
                    finally:
                        con.close()
    result = {"path": str(src), "taken_at": manifest.get("taken_at"), "ok": not problems, "problems": problems,
              "tables": tables, "seconds": round(time.perf_counter() - started, 3),
              "drilled_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if Path(src).parent == _home_backups(settings):
        _save_status(settings, last_drill=result)
    return result


def prune_backups(settings, keep: int | None = None) -> list[str]:
    keep = keep if keep is not None else int(os.environ.get("LYST_BACKUP_KEEP", "7"))
    removed = []
    for b in backups(settings)[max(1, keep):]:
        shutil.rmtree(b["path"], ignore_errors=True)
        removed.append(b["name"])
    for d in _home_backups(settings).glob(".partial-*"):  # leftovers of a crash during a backup
        shutil.rmtree(d, ignore_errors=True)
    return removed


def scheduled(lyst, now: datetime | None = None) -> dict[str, Any] | None:
    """The worker's periodic job: back up when the last good backup is older than the interval, drill it, prune."""
    hours = float(os.environ.get("LYST_BACKUP_EVERY_HOURS", "0") or 0)
    if hours <= 0:
        return None
    now = now or datetime.now(UTC)
    last = status(lyst.settings).get("last_success")
    if last and (now - datetime.fromisoformat(last)).total_seconds() < hours * 3600:
        return None
    try:
        b = take(lyst)
    except Exception:  # noqa: BLE001 - recorded in the status; the next tick tries again
        return {"ok": False}
    d = drill(lyst.settings, b["path"])
    removed = prune_backups(lyst.settings)
    return {"ok": d["ok"], "backup": b["path"], "drill": d, "removed": removed}


def report(lyst, now: datetime | None = None) -> dict[str, Any]:
    """Recovery objectives as measured: recovery point = age of the newest backup that passed a drill; recovery time
    = how long that drill took (copy, open, read) for this installation's size."""
    now = now or datetime.now(UTC)
    s = status(lyst.settings)
    drill_ = s.get("last_drill") or {}
    every = float(os.environ.get("LYST_BACKUP_EVERY_HOURS", "0") or 0)
    rpo_target = float(os.environ.get("LYST_RPO_HOURS", str(every * 1.5 if every else 24)))
    rto_target = float(os.environ.get("LYST_RTO_MINUTES", "30"))
    rpo = None
    if drill_.get("ok") and drill_.get("taken_at"):
        rpo = round((now - datetime.fromisoformat(drill_["taken_at"])).total_seconds() / 3600, 2)
    rto = drill_.get("seconds") if drill_.get("ok") else None
    return {
        "schedule": {"every_hours": every or None, "keep": int(os.environ.get("LYST_BACKUP_KEEP", "7"))},
        "backups": backups(lyst.settings),
        "last_success": s.get("last_success"), "last_failure": s.get("last_failure"), "last_error": s.get("last_error"),
        "last_drill": drill_ or None,
        "objectives": {"rpo_hours": rpo, "rpo_target_hours": rpo_target, "rpo_met": rpo is not None and rpo <= rpo_target,
                       "rto_seconds": rto, "rto_target_minutes": rto_target,
                       "rto_met": rto is not None and rto <= rto_target * 60},
        "key": "Backups never contain the master key. Restoring needs the key from its trust anchor (key file, OpenBao, "
               "OS keyring or LYST_MASTER_KEY); keep that anchor's own backup separately from these files.",
        "availability": "Single engine per installation: no high-availability claim. Continuity is a verified, recent backup "
                        "and a measured restore time.",
    }


__all__ = ["backups", "drill", "prune_backups", "report", "scheduled", "status", "take"]
