# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workspace export, import and migration between installations.

A **workspace pack** (``.lystpack``) holds everything a workspace is: every table of its database (datasets and their
versions, definitions, contracts, knowledge, memory, assets, collaboration, onboarding, classification and policy
tables), the control rows that belong to it (settings, members by email, schedules, connections, quotas, and with
``include_history`` its tasks, steps, model calls and event history), and the files Lyst wrote for it (deliverables,
dashboards). A manifest lists each part with its row count and SHA-256, the schema versions it was written with and
the environment that wrote it.

The pack is sealed with a passphrase by default: scrypt derives an AES-256-GCM key and the pack is encrypted in
authenticated chunks, so a truncated or altered pack is refused, not half-read. An unencrypted pack needs an owner
and an explicit ``encrypt=False``. Credentials and webhook secrets are never exported: connections arrive needing
their credentials again, webhooks arrive paused with a new secret.

Import always creates a new workspace (never overwrites one), checks every checksum first, and refuses a pack
written by a newer schema than this installation knows. Members are mapped by email to existing users; the
importer is the owner. Schedules and monitors arrive paused unless ``activate=True`` (a migrated workspace, not a
copy), so a copy never runs the same work twice. ``inspect_pack`` does all the checks without changing anything.

Export runs every registered check first (``export_checks``, and the governance service's export policy when it is
installed): a workspace whose data may not leave it does not leave it this way either.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import logging
import re
import secrets
import shutil
import struct
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import environment
from .security import Guard, Principal
from .store import Stores
from .store.schema import CONTROL, WORKSPACE

log = logging.getLogger("lyst.portability")
MAGIC = b"LYSTPK1\n"
PLAIN = b"PK\x03\x04"
CHUNK = 4 * 1024 * 1024
FORMAT_VERSION = 1
# control tables never exported: identities, secrets, installation-wide state, and the target's own audit chain
CONTROL_NEVER = {"_migrations", "api_tokens", "credentials", "credential_events", "secret_blobs", "users", "workspaces",
                 "memberships", "installation_admins", "installation_settings", "model_catalog", "event_cursors",
                 "webhook_deliveries"}
# exported only with include_history (and then imported as history)
CONTROL_HISTORY = {"tasks", "task_steps", "task_events", "llm_calls", "audit_log", "platform_events", "eval_runs",
                   "approvals", "connection_events", "connection_syncs", "artifacts"}
# kept in the pack as a record but not written into the target (its own audit and event sequences stay its own)
RECORD_ONLY = {"audit_log", "platform_events"}
MIN_PASSPHRASE = 12
SECRET_COLUMN = re.compile(r"secret|token|password|api_key|private_key", re.I)
# control history tables whose ids come from a sequence of the installation: renumbered on import
RENUMBER = {"task_events": ("id", "task_event_seq"), "llm_calls": ("id", "llm_call_seq")}


class PackError(ValueError):
    pass


# -- sealing -------------------------------------------------------------------------------

def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def seal(src: Path, dest: Path, passphrase: str) -> None:
    salt = secrets.token_bytes(16)
    header = {"kdf": "scrypt", "n": 2 ** 15, "r": 8, "p": 1, "salt": base64.b64encode(salt).decode(), "chunk": CHUNK}
    hbytes = json.dumps(header).encode() + b"\n"
    aes = AESGCM(_key(passphrase, salt, header["n"], header["r"], header["p"]))
    size = src.stat().st_size
    with src.open("rb") as f, dest.open("wb") as out:
        out.write(MAGIC + hbytes)
        i, done = 0, 0
        while True:
            block = f.read(CHUNK)
            done += len(block)
            final = done >= size
            nonce = secrets.token_bytes(12)
            ct = aes.encrypt(nonce, block, hbytes + struct.pack(">Q?", i, final))
            out.write(struct.pack(">I", len(ct)) + nonce + ct)
            i += 1
            if final:
                break


def unseal(src: Path, dest: Path, passphrase: str | None) -> bool:
    """Write the plain pack to ``dest``. Returns whether it was encrypted."""
    with src.open("rb") as f:
        head = f.read(len(MAGIC))
        if head[:4] == PLAIN:
            shutil.copyfile(src, dest)
            return False
        if head != MAGIC:
            raise PackError("this is not a Lyst workspace pack")
        if not passphrase:
            raise PackError("this pack is encrypted; give its passphrase")
        hbytes = f.readline()
        header = json.loads(hbytes)
        aes = AESGCM(_key(passphrase, base64.b64decode(header["salt"]), header["n"], header["r"], header["p"]))
        i, final = 0, False
        with dest.open("wb") as out:
            while not final:
                lenb = f.read(4)
                if len(lenb) < 4:
                    raise PackError("the pack is truncated: it ends before its last part")
                n = struct.unpack(">I", lenb)[0]
                nonce, ct = f.read(12), f.read(n)
                for flag in (False, True):
                    try:
                        out.write(aes.decrypt(nonce, ct, hbytes + struct.pack(">Q?", i, flag)))
                        final = flag
                        break
                    except InvalidTag:
                        continue
                else:
                    raise PackError("the passphrase is wrong or the pack was altered")
                i += 1
            if f.read(1):
                raise PackError("the pack has data after its last part; it was altered")
    return True


# -- tables --------------------------------------------------------------------------------

def _write_parquet(cur, sql: str, params: list, path: Path) -> int:
    reader = cur.sql(sql, params=params or None).to_arrow_reader(50_000)
    rows = 0
    writer = None
    try:
        for batch in reader:
            if writer is None:
                writer = pq.ParquetWriter(path, batch.schema)
            writer.write_batch(batch)
            rows += batch.num_rows
        if writer is None:  # an empty table still carries its columns
            pq.write_table(reader.schema.empty_table(), path)
    finally:
        if writer is not None:
            writer.close()
    return rows


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _insert(c, target: str, cols: str, select: str, keyed: bool) -> None:
    """INSERT OR IGNORE where the table has a key (history already here stays as it is); plain INSERT where none."""
    verb = "INSERT OR IGNORE" if keyed else "INSERT"
    c.execute(f"{verb} INTO {target} ({cols}) SELECT {select} FROM _imp").fetchall()


def _keyed(c, alias: str, schema: str, table: str) -> bool:
    return bool(c.execute("SELECT count(*) FROM duckdb_constraints() WHERE database_name = ? AND schema_name = ? AND "
                          "table_name = ? AND constraint_type IN ('PRIMARY KEY', 'UNIQUE')", [alias, schema, table]).fetchall()[0][0])


class PortabilityService:
    def __init__(self, stores: Stores, guard: Guard, credentials, home: Path) -> None:
        self.stores = stores
        self.guard = guard
        self.credentials = credentials
        self.home = Path(home)
        self.export_checks: list[Callable[[Principal, str, dict[str, Any]], None]] = []
        self.governance: Any = None  # set when the governance service is installed (evaluate(purpose="export"))

    @property
    def folder(self) -> Path:
        return self.home / "exports"

    # -- export -----------------------------------------------------------------------------

    def export(self, actor: Principal, ws: str, *, passphrase: str | None, encrypt: bool = True,
               include_history: bool = False, include_files: bool = True) -> dict[str, Any]:
        role = self.guard.require(actor, ws, "workspace.admin", ws)
        if encrypt and (not passphrase or len(passphrase) < MIN_PASSPHRASE):
            raise ValueError(f"give a passphrase of at least {MIN_PASSPHRASE} characters; the pack is encrypted with it")
        if not encrypt and role != "owner":
            from .security import AccessDenied  # noqa: PLC0415

            raise AccessDenied("only an owner may export a workspace unencrypted")
        db = self.stores.workspace(ws)
        datasets = [r["name"] for r in db.query("SELECT name FROM datasets")]
        scope = {"datasets": datasets, "purpose": "export", "include_history": include_history}
        self._check_export(actor, ws, scope)
        eid = "exp_" + dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
        work = self.folder / ".tmp" / eid
        (work / "workspace").mkdir(parents=True)
        (work / "control").mkdir()
        tables: dict[str, dict[str, Any]] = {}
        try:
            with db.read() as cur:
                for r in cur.execute("SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = ? "
                                     "ORDER BY 1, 2", [db.ALIAS]).fetchall():
                    schema, table = r
                    if table == "_migrations":
                        continue
                    path = work / "workspace" / f"{schema}.{table}.parquet"
                    n = _write_parquet(cur, f"SELECT * FROM {_q(schema)}.{_q(table)}", [], path)
                    tables[f"workspace/{path.name}"] = {"rows": n, "sha256": _sha(path)}
                ws_versions = sorted(v for (v,) in cur.execute("SELECT version FROM _migrations").fetchall())
                sequences = {n: lv for n, lv in cur.execute("SELECT sequence_name, last_value FROM duckdb_sequences() "
                                                             "WHERE database_name = ?", [db.ALIAS]).fetchall() if lv is not None}
            ctl = self.stores.control
            with ctl.read() as cur:
                cols: dict[str, list[str]] = {}
                secret_cols: dict[str, list[tuple[str, str]]] = {}
                for t, c, typ in cur.execute("SELECT table_name, column_name, data_type FROM duckdb_columns() "
                                             "WHERE database_name = ?", [ctl.ALIAS]).fetchall():
                    cols.setdefault(t, []).append(c)
                    if SECRET_COLUMN.search(c):
                        secret_cols.setdefault(t, []).append((c, typ))
                for t in sorted(cols):
                    if t in CONTROL_NEVER or (t in CONTROL_HISTORY and not include_history):
                        continue
                    if "workspace_id" in cols[t]:
                        sql, params = f"SELECT * FROM {_q(t)} WHERE workspace_id = ?", [ws]
                    elif t in ("task_steps", "task_events") and include_history:
                        sql, params = f"SELECT * FROM {_q(t)} WHERE task_id IN (SELECT id FROM tasks WHERE workspace_id = ?)", [ws]
                    else:
                        continue
                    if secret_cols.get(t):  # secrets never leave: blanked whatever the table (connections, webhooks ...)
                        blank = ", ".join(f"{'CAST(' + repr('{}') + ' AS JSON)' if typ == 'JSON' else repr('')} AS {_q(c)}"
                                          for c, typ in secret_cols[t])
                        sql = sql.replace("SELECT *", f"SELECT * REPLACE ({blank})", 1)
                    path = work / "control" / f"{t}.parquet"
                    n = _write_parquet(cur, sql, params, path)
                    tables[f"control/{path.name}"] = {"rows": n, "sha256": _sha(path)}
                workspace_row = cur.sql("SELECT * FROM workspaces WHERE id = ?", params=[ws]).to_arrow_table().to_pylist()[0]
                members = [dict(zip(("email", "name", "role"), m, strict=True)) for m in cur.execute(
                    "SELECT u.email, u.name, m.role FROM memberships m JOIN users u ON u.id = m.user_id "
                    "WHERE m.workspace_id = ? ORDER BY u.email", [ws]).fetchall()]
                ctl_versions = sorted(v for (v,) in cur.execute("SELECT version FROM _migrations").fetchall())
            files: dict[str, str] = {}
            if include_files:
                for kind in ("artifacts", "dashboards"):
                    root = self.home / kind / ws
                    if root.is_dir():
                        for f in sorted(p for p in root.rglob("*") if p.is_file()):
                            rel = f"files/{kind}/{f.relative_to(root).as_posix()}"
                            dest = work / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(f, dest)
                            files[rel] = _sha(dest)
            manifest = {"format": "lyst-workspace", "format_version": FORMAT_VERSION, "exported_at": dt.datetime.now(dt.UTC).isoformat(),
                        "exported_by": actor.email, "workspace": {"id": ws, "name": workspace_row["name"],
                                                                  "settings": json.loads(json.dumps(workspace_row, default=str))},
                        "schema": {"workspace": ws_versions, "control": ctl_versions}, "members": members,
                        "tables": tables, "files": files, "sequences": sequences, "include_history": include_history, "encrypted": encrypt,
                        "environment": environment.fingerprint(), "datasets": datasets}
            (work / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
            plain = self.folder / ".tmp" / f"{eid}.zip"
            with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(work / "manifest.json", "manifest.json")
                for rel in [*tables, *files]:
                    z.write(work / rel, rel)
            out_dir = self.folder / ws
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"{eid}.lystpack"
            if encrypt:
                seal(plain, dest, passphrase or "")
            else:
                shutil.move(plain, dest)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            (self.folder / ".tmp" / f"{eid}.zip").unlink(missing_ok=True)
        size = dest.stat().st_size
        rows = sum(t["rows"] for t in tables.values())
        self.guard.audit(actor, ws, "workspace.export", eid, "ok", {"tables": len(tables), "rows": rows, "files": len(files),
                                                                   "encrypted": encrypt, "history": include_history, "bytes": size})
        return {"id": eid, "workspace": ws, "bytes": size, "sha256": _sha(dest), "encrypted": encrypt, "tables": len(tables),
                "rows": rows, "files": len(files), "include_history": include_history,
                "download": f"/v1/workspaces/{ws}/exports/{eid}/download", "expires_in_hours": 24}

    def _check_export(self, actor: Principal, ws: str, scope: dict[str, Any]) -> None:
        for check in self.export_checks:
            check(actor, ws, scope)
        gov = self.governance
        if gov is not None and hasattr(gov, "evaluate"):
            # the workspace's data policy (governance.evaluate, purpose export): the same ceiling as exported files
            verdict = gov.evaluate(actor, ws, datasets=scope["datasets"], purpose="export")
            refused = verdict.get("outcome") == "refused" or verdict.get("allowed") is False if isinstance(verdict, dict) else not verdict
            if refused:
                from .security import AccessDenied  # noqa: PLC0415

                why = "; ".join(r.get("why", "") for r in verdict.get("reasons", []) if r.get("outcome") == "refused") \
                    if isinstance(verdict, dict) else ""
                reason = why or (verdict.get("reason") if isinstance(verdict, dict) else None)
                self.guard.audit(actor, ws, "workspace.export", ws, "denied", {"reason": reason})
                raise AccessDenied(reason or "the workspace's data policy does not allow exporting it")

    def exports(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.admin")
        self.prune()
        folder = self.folder / ws
        if not folder.is_dir():
            return []
        return [{"id": p.stem, "bytes": p.stat().st_size, "created_at": dt.datetime.fromtimestamp(p.stat().st_mtime, dt.UTC).isoformat(),
                 "download": f"/v1/workspaces/{ws}/exports/{p.stem}/download"} for p in sorted(folder.glob("*.lystpack"), reverse=True)]

    def export_path(self, actor: Principal, ws: str, eid: str) -> Path:
        self.guard.require(actor, ws, "workspace.admin", eid)
        if not eid.replace("_", "").isalnum():
            raise KeyError(eid)
        p = self.folder / ws / f"{eid}.lystpack"
        if not p.is_file():
            raise KeyError(eid)
        self.guard.audit(actor, ws, "workspace.export_download", eid, "ok", {"bytes": p.stat().st_size})
        return p

    def delete_export(self, actor: Principal, ws: str, eid: str) -> bool:
        p = self.export_path(actor, ws, eid)
        p.unlink()
        return True

    def prune(self, hours: int = 24) -> int:
        cutoff = dt.datetime.now().timestamp() - hours * 3600
        n = 0
        if self.folder.is_dir():
            for p in self.folder.glob("*/*.lystpack"):
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    n += 1
        return n

    # -- import -----------------------------------------------------------------------------

    def _open(self, content: bytes, passphrase: str | None, work: Path) -> tuple[zipfile.ZipFile, dict[str, Any], bool]:
        work.mkdir(parents=True, exist_ok=True)
        src, plain = work / "in.lystpack", work / "plain.zip"
        src.write_bytes(content)
        encrypted = unseal(src, plain, passphrase)
        src.unlink()
        try:
            z = zipfile.ZipFile(plain)
            manifest = json.loads(z.read("manifest.json"))
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            raise PackError("the pack's contents are damaged (no readable manifest)") from exc
        if manifest.get("format") != "lyst-workspace":
            raise PackError("this is not a Lyst workspace pack")
        return z, manifest, encrypted

    def _verify(self, z: zipfile.ZipFile, manifest: dict[str, Any]) -> list[str]:
        problems = []
        names = set(z.namelist())
        for rel, info in {**manifest["tables"], **{k: {"sha256": v} for k, v in manifest["files"].items()}}.items():
            if rel not in names:
                problems.append(f"{rel} is listed in the manifest but missing")
                continue
            h = hashlib.sha256()
            with z.open(rel) as f:
                for block in iter(lambda f=f: f.read(1 << 20), b""):
                    h.update(block)
            if h.hexdigest() != info["sha256"]:
                problems.append(f"{rel} does not match its checksum")
        extra = names - set(manifest["tables"]) - set(manifest["files"]) - {"manifest.json"}
        problems += [f"{e} is in the pack but not in its manifest" for e in sorted(extra)]
        return problems

    def _compatibility(self, manifest: dict[str, Any]) -> dict[str, Any]:
        known_ws, known_ctl = {v for v, _ in WORKSPACE}, {v for v, _ in CONTROL}
        newer_ws = sorted(set(manifest["schema"]["workspace"]) - known_ws)
        newer_ctl = sorted(set(manifest["schema"]["control"]) - known_ctl)
        older = sorted(known_ws - set(manifest["schema"]["workspace"]))
        ok = not newer_ws and not newer_ctl and manifest.get("format_version", 0) <= FORMAT_VERSION
        return {"compatible": ok, "pack_newer_workspace_migrations": newer_ws, "pack_newer_control_migrations": newer_ctl,
                "upgraded_by_import": older,
                "detail": "compatible" if ok else "the pack was written by a newer Lyst; upgrade this installation first",
                "environment_changes": environment.diff(manifest.get("environment"), environment.fingerprint())}

    def _plan_members(self, manifest: dict[str, Any], actor: Principal) -> dict[str, Any]:
        mapped, missing = [], []
        for m in manifest["members"]:
            if m["email"] == actor.email:
                continue
            found = self.stores.control.query("SELECT id FROM users WHERE email = ?", [m["email"]])
            (mapped if found else missing).append({**m, **({"user_id": found[0]["id"]} if found else {})})
        return {"mapped": mapped, "not_found": missing}

    def inspect_pack(self, actor: Principal, content: bytes, *, passphrase: str | None) -> dict[str, Any]:
        """Every check an import makes, with nothing changed."""
        self._require_admin(actor)
        work = self.folder / ".tmp" / ("in_" + uuid.uuid4().hex[:8])
        try:
            z, manifest, encrypted = self._open(content, passphrase, work)
            with z:
                problems = self._verify(z, manifest)
            return {"workspace": manifest["workspace"]["id"], "name": manifest["workspace"]["name"], "encrypted": encrypted,
                    "exported_at": manifest["exported_at"], "exported_by": manifest["exported_by"],
                    "datasets": manifest.get("datasets", []), "tables": len(manifest["tables"]),
                    "rows": sum(t["rows"] for t in manifest["tables"].values()), "files": len(manifest["files"]),
                    "include_history": manifest["include_history"], "intact": not problems, "problems": problems,
                    "compatibility": self._compatibility(manifest), "members": self._plan_members(manifest, actor),
                    "workspace_exists": self._exists(manifest["workspace"]["id"])}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _exists(self, ws: str) -> bool:
        return bool(self.stores.control.scalar("SELECT count(*) FROM workspaces WHERE id = ?", [ws]))

    def _require_admin(self, actor: Principal) -> None:
        if not self.credentials.is_installation_admin(actor):
            from .security import AccessDenied  # noqa: PLC0415

            raise AccessDenied("workspaces are imported by installation administrators")

    def import_pack(self, actor: Principal, content: bytes, *, passphrase: str | None, workspace_id: str | None = None,
                    name: str | None = None, activate: bool = False, add_members: bool = True) -> dict[str, Any]:
        self._require_admin(actor)
        work = self.folder / ".tmp" / ("in_" + uuid.uuid4().hex[:8])
        try:
            z, manifest, encrypted = self._open(content, passphrase, work)
            with z:
                problems = self._verify(z, manifest)
                if problems:
                    raise PackError("the pack failed its checks, nothing was imported: " + "; ".join(problems[:5]))
                compat = self._compatibility(manifest)
                if not compat["compatible"]:
                    raise PackError(compat["detail"])
                target = workspace_id or manifest["workspace"]["id"]
                if self._exists(target):
                    raise PackError(f"a workspace {target!r} already exists here; import under another id")
                self.guard.create_workspace(actor, target, name or manifest["workspace"]["name"])
                try:
                    report = self._load(actor, z, manifest, target, activate=activate, add_members=add_members)
                except Exception:
                    self._remove(target)
                    raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        report.update({"workspace": target, "from": manifest["workspace"]["id"], "encrypted": encrypted,
                       "upgraded_by_import": compat["upgraded_by_import"], "environment_changes": compat["environment_changes"]})
        self.guard.audit(actor, target, "workspace.import", target, "ok",
                         {"from": manifest["workspace"]["id"], "rows": report["rows"], "activated": activate})
        return report

    def _remove(self, ws: str) -> None:
        """Undo a half-done import: the new workspace's rows and file go; nothing else was touched."""
        ctl = self.stores.control
        cols = ctl.query("SELECT DISTINCT table_name FROM duckdb_columns() WHERE database_name = ? AND column_name = 'workspace_id'",
                         [ctl.ALIAS])
        for r in cols:
            ctl.execute(f"DELETE FROM {_q(r['table_name'])} WHERE workspace_id = ?", [ws])
        ctl.execute("DELETE FROM workspaces WHERE id = ?", [ws])
        with self.stores._lock:  # noqa: SLF001
            db = self.stores._workspaces.pop(ws, None)  # noqa: SLF001
        if db is not None:
            db.close()
        for p in (self.stores.settings.workspace_db(ws), Path(str(self.stores.settings.workspace_db(ws)) + ".wal")):
            p.unlink(missing_ok=True)

    def _load(self, actor: Principal, z: zipfile.ZipFile, manifest: dict[str, Any], ws: str, *, activate: bool,
              add_members: bool) -> dict[str, Any]:
        db = self.stores.workspace(ws)
        rows, skipped, notes = 0, {}, []

        def read(rel: str) -> pa.Table:
            with z.open(rel) as f:
                return pq.read_table(io.BytesIO(f.read()))

        with db.read() as cur:
            existing = {(s, t) for s, t in cur.execute(
                "SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = ?", [db.ALIAS]).fetchall()}
        for rel in sorted(r for r in manifest["tables"] if r.startswith("workspace/")):
            schema, table = rel[len("workspace/"):-len(".parquet")].split(".", 1)
            data = read(rel)

            def tx(c, schema=schema, table=table, data=data):
                c.register("_imp", data)
                try:
                    if (schema, table) not in existing:
                        if schema != "data":
                            raise PackError(f"{schema}.{table} is not a table this installation knows")
                        c.execute(f"CREATE TABLE data.{_q(table)} AS SELECT * FROM _imp").fetchall()
                        return data.num_rows, 0
                    have = {r[0] for r in c.execute("SELECT column_name FROM duckdb_columns() WHERE database_name = ? "
                                                    "AND schema_name = ? AND table_name = ?", [db.ALIAS, schema, table]).fetchall()}
                    cols = [n for n in data.column_names if n in have]
                    if not cols:
                        return 0, data.num_rows
                    sel = ", ".join(_q(n) for n in cols)
                    before = c.execute(f"SELECT count(*) FROM {_q(schema)}.{_q(table)}").fetchall()[0][0]
                    _insert(c, f"{_q(schema)}.{_q(table)}", sel, sel, _keyed(c, db.ALIAS, schema, table))
                    added = c.execute(f"SELECT count(*) FROM {_q(schema)}.{_q(table)}").fetchall()[0][0] - before
                    return added, data.num_rows - added
                finally:
                    c.unregister("_imp")

            added, ignored = db.write(tx)
            rows += added
            if ignored:
                skipped[rel] = ignored
        # ids drawn from sequences (query history) continue after the imported ones, never collide with them
        for seq, last in (manifest.get("sequences") or {}).items():
            def restart(c, seq=seq, last=int(last)):
                if c.execute("SELECT count(*) FROM duckdb_sequences() WHERE database_name = ? AND sequence_name = ?",
                             [db.ALIAS, seq]).fetchall()[0][0]:
                    c.execute(f"DROP SEQUENCE {_q(seq)}").fetchall()
                    c.execute(f"CREATE SEQUENCE {_q(seq)} START {last + 1}").fetchall()
            db.write(restart)
        if not activate:
            db.execute("UPDATE monitors SET enabled = false WHERE enabled")
        # control rows: settings, then everything scoped to the workspace
        settings = manifest["workspace"]["settings"]
        ctl = self.stores.control
        wcols = {r["column_name"] for r in ctl.query("SELECT column_name FROM duckdb_columns() WHERE database_name = ? "
                                                     "AND table_name = 'workspaces'", [ctl.ALIAS])}
        for k, v in settings.items():
            if k in wcols and k not in ("id", "name", "created_at") and v is not None:
                ctl.execute(f"UPDATE workspaces SET {_q(k)} = ? WHERE id = ?", [v, ws])
        for rel in sorted(r for r in manifest["tables"] if r.startswith("control/")):
            table = rel[len("control/"):-len(".parquet")]
            if table in RECORD_ONLY:
                notes.append(f"{table} is kept in the pack as a record; this installation keeps its own")
                continue
            data = read(rel)
            if "workspace_id" in data.column_names:
                data = data.set_column(data.column_names.index("workspace_id"), "workspace_id",
                                       pa.array([ws] * data.num_rows, pa.string()))
            if table == "webhook_subscriptions" and data.num_rows:
                data = data.set_column(data.column_names.index("secret"), "secret",
                                       pa.array(["whsec_" + secrets.token_urlsafe(32) for _ in range(data.num_rows)]))
                for col, val in (("active", False), ("disabled_reason", "imported: rotate the secret and re-enable")):
                    if col in data.column_names:
                        data = data.set_column(data.column_names.index(col), col, pa.array([val] * data.num_rows))
                notes.append(f"{data.num_rows} webhook(s) arrived paused with a new secret")
            if table == "tasks" and data.num_rows:
                status = data.column("status").to_pylist()
                fixed = ["failed" if s in ("queued", "pending", "running", "approved") else s for s in status]
                data = data.set_column(data.column_names.index("status"), "status", pa.array(fixed))
                for col in ("lease_owner", "lease_until"):
                    if col in data.column_names:
                        data = data.set_column(data.column_names.index(col), col, pa.nulls(data.num_rows, data.schema.field(col).type))
            if table == "schedules" and not activate and "enabled" in data.column_names:
                data = data.set_column(data.column_names.index("enabled"), "enabled", pa.array([False] * data.num_rows))
            if table == "connections" and data.num_rows:
                notes.append(f"{data.num_rows} connection(s) need their credentials connected again (secrets are never exported)")
            have = {r["column_name"] for r in ctl.query("SELECT column_name FROM duckdb_columns() WHERE database_name = ? "
                                                        "AND table_name = ?", [ctl.ALIAS, table])}
            if not have:
                notes.append(f"{table} is not a table this installation has; skipped")
                continue
            cols = [n for n in data.column_names if n in have]
            renumber = RENUMBER.get(table)
            if renumber:  # ids from this installation's own sequence: drawn afresh, so nothing collides later
                cols = [n for n in cols if n != renumber[0]]
            sel = ", ".join(_q(n) for n in cols)
            ins = f"{_q(renumber[0])}, {sel}" if renumber else sel
            src = f"nextval('{renumber[1]}'), {sel}" if renumber else sel

            def ctx(c, table=table, data=data, ins=ins, src=src):
                c.register("_imp", data)
                try:
                    before = c.execute(f"SELECT count(*) FROM {_q(table)}").fetchall()[0][0]
                    _insert(c, _q(table), ins, src, _keyed(c, ctl.ALIAS, "main", table))
                    return c.execute(f"SELECT count(*) FROM {_q(table)}").fetchall()[0][0] - before
                finally:
                    c.unregister("_imp")

            added = ctl.write(ctx)
            rows += added
            if added < data.num_rows:
                skipped[rel] = data.num_rows - added
        if skipped:
            notes.append("some history rows already exist on this installation (an earlier copy) and were kept as they are")
        members = self._plan_members(manifest, actor)
        if add_members:
            for m in members["mapped"]:
                ctl.execute("INSERT OR IGNORE INTO memberships (workspace_id, user_id, role) VALUES (?, ?, ?)",
                            [ws, m["user_id"], m["role"]])
        for kind in ("artifacts", "dashboards"):
            prefix = f"files/{kind}/"
            for rel in [r for r in manifest["files"] if r.startswith(prefix)]:
                dest = self.home / kind / ws / rel[len(prefix):]
                if ".." in Path(rel).parts:
                    raise PackError(f"{rel} points outside the workspace folder")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(rel) as f, dest.open("wb") as out:
                    shutil.copyfileobj(f, out)
        paused = [] if activate else ["schedules", "monitors"]
        return {"rows": rows, "tables": len(manifest["tables"]), "files": len(manifest["files"]), "skipped": skipped,
                "members": {"added": [m["email"] for m in members["mapped"]] if add_members else [],
                            "not_found": [m["email"] for m in members["not_found"]]},
                "paused": paused, "notes": notes}


__all__ = ["PackError", "PortabilityService", "seal", "unseal"]
