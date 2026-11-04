# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""lyst init | init-demo | serve | ui | token | backup | restore | rotate-master-key | prune | eval"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import load_settings


def _write_token(home: Path, token: str, name: str = "dev-token") -> Path:
    path = home / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    return path


def _ensure_master_key() -> None:
    from .secrets import masterkey

    if masterkey.resolve() is None:
        if masterkey.keyring_available() is None:
            sys.exit("No master encryption key: run the engine and open the interface on this machine to initialise it, or mount "
                     "the key for an unattended deployment (docs/SECURITY.md).")
        masterkey.provision_into_keyring()
        print(f"Generated the master key into the {masterkey.keyring_available()} credential store (service 'lyst'). "
              "Keep a recovery copy: `lyst init --show` prints it only on first generation, so export it from the store.", file=sys.stderr)


def init_demo() -> None:
    from . import demo
    from .app import Lyst

    _ensure_master_key()
    lyst = Lyst.build()
    try:
        if lyst.stores.control.scalar("SELECT count(*) FROM workspaces WHERE id = 'northwind'"):
            print("The demo workspace already exists.")
            return
        owner = lyst.guard.create_user("avery@northwind.test", "Avery Lin")
        demo.seed(lyst, owner)
        viewer = lyst.guard.create_user("sam@northwind.test", "Sam Ortiz")
        lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
        path = _write_token(lyst.settings.home, lyst.guard.issue_token(owner, "dev"))
        vpath = _write_token(lyst.settings.home, lyst.guard.issue_token(viewer, "dev"), "viewer-token")
        print(f"Seeded workspace 'northwind'. Owner token: {path}; viewer token: {vpath}")
    finally:
        lyst.close()


def issue_token(email: str, name: str | None, hours: int | None, owner_of: str | None) -> None:
    from .app import Lyst

    lyst = Lyst.build()
    try:
        user = lyst.guard.user_by_email(email) or lyst.guard.create_user(email.lower(), name or email.split("@")[0])
        if owner_of:
            lyst.guard.create_workspace(user, owner_of, owner_of.replace("_", " ").title())
        token = lyst.guard.issue_token(user, "cli", hours=hours)
        print(token)
    finally:
        lyst.close()


def init_cmd(recovery: bool, show: bool) -> None:
    """Initialise encryption on this machine: generate the master key into the OS credential store (the browser's
    setup page does the same). ``--recovery`` restores a saved key instead; ``--show`` prints the recovery copy."""
    import getpass

    from .secrets import masterkey

    existing = masterkey.resolve()
    if existing and not recovery:
        print(f"A master key is already available (source: {existing.source}). Nothing to do.")
        return
    if recovery:
        value = getpass.getpass("Paste the recovery copy of the master key (hex): ")
        mk = masterkey.store_into_keyring(value)
        print(f"Stored the master key in the {masterkey.keyring_available()} credential store (service 'lyst').")
        return
    if masterkey.keyring_available() is None:
        sys.exit("This machine has no usable OS credential store. For an unattended deployment mount the key at "
                 "$LYST_SECRETS_DIR/LYST_MASTER_KEY, serve it from OpenBao (LYST_OPENBAO_URL) or inject LYST_MASTER_KEY.")
    mk = masterkey.provision_into_keyring()
    print(f"Generated a master key and stored it in the {masterkey.keyring_available()} credential store (service 'lyst').")
    if show:
        print("Recovery copy (keep it offline; Lyst never stores it):", file=sys.stderr)
        print(mk.key.hex())
    else:
        print("Run `lyst init --show` was not used: to keep a recovery copy, export it from the credential store now.", file=sys.stderr)


def rotate_master_key(*, new_key: bytes | None = None, update_anchor: bool = True, yes: bool = False) -> str:
    """Root-key rotation with the engine stopped. Every database is copied under keys derived from the new master
    key and every secret's data key is re-wrapped in the copy; only when all of that succeeded are the files
    swapped and the anchor updated. A failure anywhere before the swap leaves the installation untouched."""
    import secrets as rnd
    import types

    from .app import Lyst
    from .secrets.backends import EmbeddedBackend
    from .store.db import Database

    settings = load_settings()
    if settings.setup_required:
        sys.exit("No master key is available; nothing to rotate.")
    if settings.master_key_source not in ("keyring", "env", "file", "dev-keyfile") and update_anchor and not yes:
        sys.exit(f"The master key comes from {settings.master_key_source}; rotate it there and rerun with --yes to re-key the files.")
    new_key = new_key or rnd.token_bytes(32)
    staging = settings.home / "backups" / ".rekey"
    if staging.exists():
        shutil.rmtree(staging)
    lyst = Lyst.build(settings)
    try:
        old_version = lyst.credentials.backend.key_version if isinstance(lyst.credentials.backend, EmbeddedBackend) else None
        pairs = lyst.stores.rekey_copies(new_key, staging)
    finally:
        lyst.close()
    if old_version is not None:  # re-wrap the data keys inside the staged control copy, old files untouched
        ctl = Database(staging / "control.duckdb", name="control", master_key=new_key, encrypt=settings.encrypt, allowed_dir=staging)
        try:
            EmbeddedBackend(types.SimpleNamespace(control=ctl), settings.master_key, old_version).rewrap_all(new_key, old_version + 1)
        finally:
            ctl.close()
    # swap: old files stay beside the new ones as *.pre-rotation until the operator removes them
    swapped = []
    try:
        for live, copy in pairs:
            keep = live.with_name(live.name + ".pre-rotation")
            if keep.exists():
                keep.unlink()
            wal = live.with_name(live.name + ".wal")
            if wal.exists():
                wal.unlink()
            if live.exists():
                live.rename(keep)
            swapped.append((live, keep))
            shutil.move(copy, live)
        if update_anchor and settings.master_key_source == "keyring":
            from .secrets import masterkey

            masterkey.store_into_keyring(new_key.hex())
    except Exception:
        for live, keep in swapped:  # put the old files back
            if live.exists():
                live.unlink()
            if keep.exists():
                keep.rename(live)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if update_anchor and settings.master_key_source == "keyring":
        print("Rotated the master key; the credential store holds the new one. Previous database files are kept as *.pre-rotation.")
    elif update_anchor:
        print(f"Rotated the files. Place this new key at the anchor ({settings.master_key_source}) before the next start:", file=sys.stderr)
        print(new_key.hex())
    return new_key.hex()


def backup(dest: Path) -> None:
    from .app import Lyst

    lyst = Lyst.build()
    try:
        m = lyst.stores.backup(dest)
        print(f"Backed up {len(m['files'])} database(s) to {dest} at {m['taken_at']}. {m['note']}")
    finally:
        lyst.close()


def restore(src: Path, yes: bool) -> None:
    """Copy a backup into LYST_HOME after checking every database opens with the current master key."""
    import duckdb

    from .store.db import derive_key

    settings = load_settings()
    if settings.setup_required:
        sys.exit("No master key is available: restore needs the key the backup was taken with (lyst init --recovery).")
    manifest = json.loads((src / "manifest.json").read_text())
    for rel in manifest["files"]:
        name = "control" if rel == "control.duckdb" else "ws:" + Path(rel).stem
        con = duckdb.connect()
        quoted = str(src / rel).replace("'", "''")
        try:
            if manifest["encrypted"]:
                con.execute(f"ATTACH '{quoted}' AS chk (ENCRYPTION_KEY '{derive_key(settings.master_key, name)}', READ_ONLY)")
            else:
                con.execute(f"ATTACH '{quoted}' AS chk (READ_ONLY)")
            con.execute("DETACH chk")
        except duckdb.Error as exc:
            sys.exit(f"{rel} does not open with the current master key: {exc}")
        finally:
            con.close()
    if (settings.home / "control.duckdb").exists() and not yes:
        answer = input(f"{settings.home} already holds a Lyst installation. Replace it? [y/N] ")
        if answer.strip().lower() != "y":
            sys.exit("Restore cancelled.")
    for rel in manifest["files"]:
        target = settings.home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", ".wal"):
            if (target.parent / (target.name + suffix)).exists():
                (target.parent / (target.name + suffix)).unlink()
        shutil.copy2(src / rel, target)
    for folder in ("artifacts", "dashboards"):
        if (src / folder).exists():
            shutil.copytree(src / folder, settings.home / folder, dirs_exist_ok=True)
    print(f"Restored {len(manifest['files'])} database(s) from {src} (taken {manifest['taken_at']}) into {settings.home}.")


def run_eval(label: str) -> None:
    import tempfile

    from . import demo, evals
    from .app import Lyst
    from .secrets import masterkey

    with tempfile.TemporaryDirectory() as home:
        if masterkey.resolve() is None:  # a throwaway installation gets a throwaway key; nothing here outlives the run
            import secrets as rnd

            os.environ["LYST_MASTER_KEY"] = rnd.token_hex(32)
        lyst = Lyst.build(load_settings(home))
        try:
            owner = lyst.guard.create_user("eval@lyst.local", "Evaluator")
            demo.seed(lyst, owner)
            s = evals.run(lyst, owner, "northwind", evals.demo_bank(), label=label)
            for r in s["results"]:
                bad = [c["name"] for c in r["checks"] if not c["passed"]]
                print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['seconds']:>5.2f}s  {r['case']}" + (f"  ({', '.join(bad)})" if bad else ""))
            print(f"{s['passed']}/{s['cases']} passed, p95 {s['p95_seconds']:.2f}s, model {s['model'] or 'none (rules)'}")
            pr = s["productivity"]
            if pr["median_gain"]:
                print(f"productivity: median gain {pr['median_gain']:.0f}x over {pr['cases_with_baseline']} cases with a declared "
                      f"baseline ({pr['note']})")
            sys.exit(0 if s["passed"] == s["cases"] else 1)
        finally:
            lyst.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="lyst")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-demo", help="create the demo workspace and a development token")
    sub.add_parser("serve", help="run the API and workers")
    sub.add_parser("ui", help="run the Streamlit interface")
    tk = sub.add_parser("token", help="issue an API token for a user (creates the user if new)")
    tk.add_argument("email")
    tk.add_argument("--name", default=None)
    tk.add_argument("--hours", type=int, default=None, help="expiry; default never")
    tk.add_argument("--owner-of", default=None, help="also create this workspace with the user as owner")
    ini = sub.add_parser("init", help="initialise encryption: generate the master key into the OS credential store")
    ini.add_argument("--recovery", action="store_true", help="restore a saved master key instead of generating one")
    ini.add_argument("--show", action="store_true", help="print the recovery copy once")
    rk = sub.add_parser("rotate-master-key", help="re-key every database and re-wrap every secret under a new master key (engine stopped)")
    rk.add_argument("--yes", action="store_true")
    bk = sub.add_parser("backup", help="consistent, encrypted backup of every database plus artifacts and dashboards")
    bk.add_argument("dest")
    rs = sub.add_parser("restore", help="restore a backup into LYST_HOME (engine must be stopped)")
    rs.add_argument("src")
    rs.add_argument("--yes", action="store_true", help="replace existing files without asking")
    pr = sub.add_parser("prune", help="apply retention: drop old events, calls, query history and notifications")
    pr.add_argument("--event-days", type=int, default=int(os.environ.get("LYST_RETENTION_DAYS_EVENTS", "90")))
    pr.add_argument("--audit-days", type=int, default=int(os.environ.get("LYST_RETENTION_DAYS_AUDIT", "400")))
    ev = sub.add_parser("eval", help="run the demo evaluation bank against a fresh demo workspace")
    ev.add_argument("--label", default="")
    args = p.parse_args(argv)
    if args.cmd == "init-demo":
        init_demo()
    elif args.cmd == "serve":
        from .api import main as serve

        serve()
    elif args.cmd == "token":
        issue_token(args.email, args.name, args.hours, args.owner_of)
    elif args.cmd == "init":
        init_cmd(args.recovery, args.show)
    elif args.cmd == "rotate-master-key":
        rotate_master_key(yes=args.yes)
    elif args.cmd == "backup":
        backup(Path(args.dest))
    elif args.cmd == "restore":
        restore(Path(args.src), args.yes)
    elif args.cmd == "prune":
        from .app import Lyst

        lyst = Lyst.build()
        try:
            print(json.dumps(lyst.stores.prune(event_days=args.event_days, audit_days=args.audit_days)))
        finally:
            lyst.close()
    elif args.cmd == "eval":
        run_eval(args.label)
    elif args.cmd == "ui":
        app = Path(__file__).parent / "ui" / "app.py"
        settings = load_settings()
        env = {**os.environ, "LYST_HOME": str(settings.home)}
        sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app),
                                  "--server.port", os.environ.get("LYST_UI_PORT", "8501")], env=env,
                                 cwd=Path(__file__).resolve().parents[2]))
