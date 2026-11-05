# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""lyst start [--check] | status | stop | init | init-demo | serve | ui | token | backup | restore | rotate-master-key | prune | eval"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import load_settings
from .launch import cmd, via_uv
from .secrets.masterkey import MasterKeyMissing


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
        mk = masterkey.provision_into_keyring()
        print(f"Generated the master key into the {masterkey.keyring_available()} credential store (service 'lyst').\n"
              "Recovery copy, shown once; keep it offline (Lyst never stores it):", file=sys.stderr)
        print(mk.key.hex(), file=sys.stderr)


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


def init_cmd(recovery: bool, quiet: bool) -> None:
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
    print(f"Generated a master key and stored it in the {masterkey.keyring_available()} credential store (service 'lyst').", file=sys.stderr)
    if quiet:
        print("Recovery copy not shown (--quiet): export it from the credential store and keep it offline.", file=sys.stderr)
    else:
        print("Recovery copy, shown once; keep it offline (Lyst never stores it):", file=sys.stderr)
        print(mk.key.hex())


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


def _wait_http(url: str, seconds: float, proc: subprocess.Popen | None = None) -> dict | None:
    """Wait for `url` to answer; give up early if `proc` (the process that should be serving it) exits."""
    import time

    from .launch import http_ok

    deadline = time.time() + seconds
    while time.time() < deadline:
        body = http_ok(url)
        if body is not None:
            return body
        if proc is not None and proc.poll() is not None:
            return None
        time.sleep(0.4)
    return None


UI_HOST = "lyst.localhost"   # resolves to the loopback address on macOS, Linux and in every browser


def ui_address(env: dict[str, str]) -> tuple[str, bool]:
    """Where `lyst ui` listens, and whether that is loopback only. Streamlit's own default is every interface; Lyst's
    is localhost, and a deployment that wants the network says so (STREAMLIT_SERVER_ADDRESS, as Compose does)."""
    address = env.get("STREAMLIT_SERVER_ADDRESS") or "localhost"
    return address, address in ("localhost", "127.0.0.1", "::1")


def _port(var: str, default: int, avoid: set[int]) -> tuple[int, str]:
    """The port to use and a note if it moved. A port the person set explicitly is kept, and a busy one is an error."""
    from .launch import pick_port, port_free

    if os.environ.get(var):
        port = int(os.environ[var])
        if not port_free(port):
            raise SystemExit(f"Port {port} ({var}) is in use by another program. Pick a free one, or unset {var} "
                             "and Lyst will choose one.")
        return port, ""
    port = pick_port(default, avoid=avoid)
    if port is None:
        raise SystemExit(f"No free port found near {default}; set {var} to one that is free.")
    return port, "" if port == default else f"port {default} was busy, so it uses {port}"


def _already_running(home: Path, *, open_browser: bool) -> bool:
    """Handle an existing Lyst on this LYST_HOME before starting another. True means: nothing more to do."""
    import webbrowser

    from .launch import clear_record, inspect

    state = inspect(home)
    if state.status == "running":
        rec = state.record
        print(f"Lyst is already running at {rec.url}  (started {_ago(rec.started_at)}, PID {rec.launcher}).", file=sys.stderr)
        print(("Opening it. " if open_browser else "") + f"{cmd('stop')} stops it.", file=sys.stderr)
        if open_browser:
            webbrowser.open(rec.url)
        return True
    if state.status == "starting":
        print(f"{state.detail} It will be at {state.record.url} in a moment; {cmd('status')} shows when.", file=sys.stderr)
        return True
    if state.status == "stale":
        clear_record(home)
        print("Cleared a leftover record from a Lyst that is no longer running.", file=sys.stderr)
        return False
    if state.status == "unhealthy":
        raise SystemExit(f"{state.detail}\nRun {cmd('stop')} to stop what is left of it, then {cmd('start')} again.")
    if state.status == "locked":
        raise SystemExit(state.detail)
    return False


def _ago(ts: float) -> str:
    import time

    mins = int((time.time() - ts) // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    return f"{mins // 60} h {mins % 60} min ago" if mins < 24 * 60 else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def start(*, demo: bool = False, open_browser: bool = True, hold: bool = True) -> dict:
    """One terminal: encryption, the demo if asked, the engine, the interface, and the browser at http://lyst.localhost:8501.
    Safe to run again: an already-running Lyst is reused, busy default ports move, and Ctrl-C stops everything."""
    import signal
    import time
    import webbrowser

    from .launch import RunRecord, clear_record, write_record
    from .secrets import masterkey

    settings = load_settings()
    if _already_running(settings.home, open_browser=open_browser):
        return {}
    if masterkey.resolve() is None and masterkey.keyring_available():
        _ensure_master_key()
    if demo:
        init_demo()
    api_port, api_note = _port("LYST_PORT", 8770, set())
    ui_port, ui_note = _port("LYST_UI_PORT", 8501, {api_port})
    url = f"http://{UI_HOST}:{ui_port}"
    env = {**os.environ, "LYST_HOME": str(settings.home), "LYST_PORT": str(api_port), "LYST_UI_PORT": str(ui_port),
           "LYST_API_URL": f"http://127.0.0.1:{api_port}",
           "STREAMLIT_SERVER_HEADLESS": "true", "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false"}
    root = Path(__file__).resolve().parents[2]
    rec = RunRecord(launcher=os.getpid(), api_port=api_port, ui_port=ui_port, url=url, home=str(settings.home),
                    started_at=time.time())
    procs: list[subprocess.Popen] = []
    stop = {"now": False}

    def _stop(*_):
        stop["now"] = True

    # Ctrl-C reaches only this process (the children run in their own session) and is turned into an orderly stop,
    # also while the engine and interface are still coming up.
    old = {sig: signal.signal(sig, _stop) for sig in (signal.SIGINT, signal.SIGTERM)} if hold else {}
    write_record(settings.home, rec)
    print(f"Starting Lyst (data in {settings.home})...", file=sys.stderr)
    try:
        procs.append(subprocess.Popen([sys.executable, "-c", "from lyst.cli import main; main(['serve'])"], env=env, cwd=root,
                                      start_new_session=hold))
        rec.children["engine"] = procs[-1].pid
        write_record(settings.home, rec)
        health = _wait_http(f"http://127.0.0.1:{api_port}/health", 90, procs[-1])
        if stop["now"]:
            return {}
        if health is None:
            raise SystemExit(f"The engine did not come up; the lines above say why. {cmd('start --check')} checks the usual causes.")
        app = Path(__file__).parent / "ui" / "app.py"
        procs.append(subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(ui_port),
                                       "--server.headless", "true", "--browser.serverAddress", UI_HOST,
                                       "--server.address", "localhost"],
                                      env={**env, "LYST_UI_LOCAL": "1"}, cwd=root, start_new_session=hold))
        rec.children["interface"] = procs[-1].pid
        write_record(settings.home, rec)
        if _wait_http(f"http://127.0.0.1:{ui_port}/_stcore/health", 90, procs[-1]) is None:
            if stop["now"]:
                return {}
            raise SystemExit(f"The interface did not come up; the lines above say why. {cmd('start --check')} checks the usual causes.")
        lines = [f"\nLyst is running at {url}",
                 f"  Engine     http://127.0.0.1:{api_port}" + (f"  ({api_note})" if api_note else ""),
                 f"  Interface  {url}" + (f"  ({ui_note})" if ui_note else ""),
                 f"  Data       {settings.home}"]
        if demo:
            lines.append("  Demo       the Northwind workspace is ready; try \"Why did operating margin decline last quarter?\"")
        if health.get("setup_required"):
            lines.append("  First run  the page asks you to initialise encryption; keep the recovery key it shows once.")
        lines.append(f"Ctrl-C stops it. From another terminal in this folder: {cmd('status')}, {cmd('stop')}.")
        if via_uv():
            lines.append("(To type plain `lyst ...`, run `source .venv/bin/activate` in that terminal first.)")
        lines.append("")
        print("\n".join(lines), file=sys.stderr)
        if open_browser:
            webbrowser.open(url)
        info = {"url": url, "api": f"http://127.0.0.1:{api_port}", "setup_required": bool(health.get("setup_required")),
                "pids": [p.pid for p in procs]}
        if not hold:
            return info
        while not stop["now"] and all(p.poll() is None for p in procs):
            signal.pause() if hasattr(signal, "pause") else time.sleep(0.5)
        if not stop["now"]:
            gone = "engine" if procs[0].poll() is not None else "interface"
            print(f"The {gone} stopped unexpectedly (the lines above say why); stopping the rest.", file=sys.stderr)
        return info
    finally:
        if hold:
            if procs:
                print("Stopping Lyst...", file=sys.stderr)
            for p in procs:
                if p.poll() is None:
                    p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            clear_record(settings.home, launcher=rec.launcher)
            for sig, handler in old.items():
                signal.signal(sig, handler)
            if procs:
                print("Lyst stopped.", file=sys.stderr)


def status_cmd(as_json: bool) -> int:
    from .launch import inspect

    settings = load_settings()
    state = inspect(settings.home)
    if as_json:
        print(json.dumps(state.as_dict()))
    elif state.status == "running":
        rec = state.record
        print(f"Lyst is running at {rec.url}\n  Engine     http://127.0.0.1:{rec.api_port}\n  Data       {rec.home}\n"
              f"  Started    {_ago(rec.started_at)}\n  PIDs       {rec.launcher} (lyst start), "
              + ", ".join(f"{pid} ({name})" for name, pid in rec.children.items()))
    elif state.status == "stale":
        print(f"Lyst isn't running. {state.detail} {cmd('start')} clears it.")
    elif state.status == "unhealthy":
        print(f"{state.detail}\n{cmd('stop')} stops what is left of it.")
    else:
        print(state.detail)
    return 0 if state.status in ("running", "starting") else 1


def stop_cmd() -> int:
    from .launch import inspect
    from .launch import stop as stop_recorded

    settings = load_settings()
    state = inspect(settings.home)
    if state.status == "locked":
        print(f"Lyst has no record of starting anything here, so it won't stop it. {state.detail}", file=sys.stderr)
        return 1
    if state.status in ("stopped", "stale"):
        stop_recorded(settings.home)   # clears a leftover record; signals nothing
        print("Lyst isn't running.")
        return 0
    print(f"Stopping Lyst (PID {state.record.launcher})...")
    stop_recorded(settings.home, say=print)
    print("Lyst stopped.")
    return 0


def start_checks(as_json: bool) -> int:
    """`lyst start --check`: the preflight alone. It opens no database, so it also works while another process holds one."""
    from .launch import preflight

    settings = load_settings()
    checks = preflight(settings.home, api_port=int(os.environ.get("LYST_PORT", "8770")),
                       ui_port=int(os.environ.get("LYST_UI_PORT", "8501")))
    if as_json:
        print(json.dumps([c.__dict__ for c in checks]))
    else:
        for c in checks:
            print(f"  {'ok ' if c.ok else 'FIX'}  {c.name:<16} {c.detail}" + (f"\n       {'':<16} {c.fix}" if not c.ok and c.fix else ""))
        print("Everything looks ready." if all(c.ok for c in checks) else f"Fix the items marked FIX, then {cmd('start')}.")
    return 0 if all(c.ok for c in checks) else 1


def backup(dest: Path) -> None:
    """Back up every database. With the engine stopped this opens the files itself; while the engine runs (it holds
    DuckDB's lock on every file) the engine takes the backup and this copies it to dest (B3)."""
    import duckdb

    from .app import Lyst

    try:
        lyst = Lyst.build()
    except duckdb.IOException as exc:
        if "lock" not in str(exc).lower():
            raise
        m = _backup_through_engine(dest)
        print(f"The engine is running, so it took the backup: {len(m['files'])} database(s) copied to {dest} at "
              f"{m['taken_at']}. {m['note']}")
        return
    try:
        m = lyst.stores.backup(dest)
        print(f"Backed up {len(m['files'])} database(s) to {dest} at {m['taken_at']}. {m['note']}")
    finally:
        lyst.close()


def _backup_through_engine(dest: Path) -> dict:
    import httpx

    settings = load_settings()
    url = os.environ.get("LYST_API_URL", f"http://127.0.0.1:{os.environ.get('LYST_PORT', '8770')}")
    token = os.environ.get("LYST_TOKEN") or ((settings.home / "dev-token").read_text().strip()
                                             if (settings.home / "dev-token").exists() else None)
    if not token:
        sys.exit("The engine is running and holds the database files. Set LYST_TOKEN to an installation administrator's "
                 "token (or stop the engine) and run the backup again.")
    r = httpx.post(f"{url}/v1/installation/backup", headers={"Authorization": f"Bearer {token}"}, timeout=600)
    if r.status_code >= 400:
        sys.exit(f"The running engine refused the backup ({r.status_code}): {r.json().get('detail', r.text)}")
    m = r.json()
    src = Path(m.pop("path"))
    if src.resolve() != Path(dest).resolve():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    return m


def restore(src: Path, yes: bool) -> None:
    """Copy a backup into LYST_HOME after checking every database opens with the current master key."""
    import duckdb

    from .store.db import derive_key

    settings = load_settings()
    if settings.setup_required:
        sys.exit("No master key is available: restore needs the key the backup was taken with (lyst init --recovery).")
    manifest = json.loads((src / "manifest.json").read_text())
    import hashlib

    # B1: the manifest's checksums are checked before anything is opened or copied; a flipped byte, a truncated
    # file or a swapped file stops the restore while the installation is untouched
    for rel, digest in manifest["files"].items():
        path = src / rel
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            sys.exit(f"{rel} does not match the checksum in the backup's manifest (damaged or altered); nothing was restored.")
    # B2: never replace files an engine has open. DuckDB holds a lock on an open database file; probing it here
    # fails while `lyst serve` (or any other process, or this one) has it attached.
    for rel in manifest["files"]:
        target = settings.home / rel
        if target.exists():
            probe = duckdb.connect()
            try:
                key = derive_key(settings.master_key, "control" if rel == "control.duckdb" else "ws:" + Path(rel).stem)
                quoted = str(target).replace("'", "''")
                probe.execute(f"ATTACH '{quoted}' AS live (ENCRYPTION_KEY '{key}')" if settings.encrypt
                              else f"ATTACH '{quoted}' AS live")
                probe.execute("DETACH live")
            except duckdb.Error as exc:
                msg = str(exc).lower()
                if "lock" in msg or "already" in msg or "conflict" in msg:
                    sys.exit(f"{target} is in use by a running Lyst engine; stop it (Ctrl-C on `lyst start` or `lyst serve`) "
                             "and run the restore again. Nothing was restored.")
                # any other failure (for example a damaged live file) does not block replacing it
            finally:
                probe.close()
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


def run_eval(label: str, *, baseline: str | None = None, write_baseline: str | None = None,
             json_out: str | None = None) -> None:
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
            print("dimensions: " + ", ".join(f"{k} {v['passed']}/{v['checks']}" for k, v in s["dimensions"].items()))
            print("categories: " + ", ".join(f"{k} {v['passed']}/{v['cases']}" for k, v in s["categories"].items()))
            ok = s["passed"] == s["cases"]
            if baseline:
                cmp_ = evals.compare(s, json.loads(Path(baseline).read_text()))
                for r in cmp_["regressions"]:
                    print(f"REGRESSION  {r['kind']:<10} {r['case']}: {r['check']} ({r['detail']})")
                for m in cmp_["missing"]:
                    print(f"REGRESSION  missing    {m}: in the baseline but not run")
                if cmp_["improvements"]:
                    print(f"improved: {len(cmp_['improvements'])} check(s) now pass that failed in the baseline")
                if cmp_["new"]:
                    print(f"new cases (not in the baseline): {', '.join(cmp_['new'])}")
                print("baseline: " + ("no regressions" if cmp_["ok"] else f"{len(cmp_['regressions']) + len(cmp_['missing'])} regression(s)"))
                ok = ok and cmp_["ok"]
            if write_baseline:
                Path(write_baseline).write_text(json.dumps(evals.baseline_of(s), indent=1, sort_keys=True) + "\n")
                print(f"baseline written to {write_baseline}")
            if json_out:
                Path(json_out).write_text(json.dumps(s, indent=1, default=str))
            sys.exit(0 if ok else 1)
        finally:
            lyst.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="lyst")
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start", help="one terminal: encryption, the engine, the interface and the browser at http://lyst.localhost:8501")
    st.add_argument("--demo", action="store_true", help="also seed the Northwind demo workspace on first run")
    st.add_argument("--no-browser", action="store_true")
    st.add_argument("--check", action="store_true", help="only run the start checks (Python, data folder, key, database, ports)")
    st.add_argument("--json", action="store_true", help="with --check: the checks as JSON")
    stt = sub.add_parser("status", help="is Lyst running here, where, and under which PIDs")
    stt.add_argument("--json", action="store_true")
    sub.add_parser("stop", help="stop the Lyst that `lyst start` launched here (only the PIDs it recorded)")
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
    ini.add_argument("--quiet", action="store_true", help="do not print the recovery copy")
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
    ev.add_argument("--baseline", help="compare with this baseline file; any regression fails the run")
    ev.add_argument("--write-baseline", help="write this run as the new baseline (after reviewing it)")
    ev.add_argument("--json", help="write the full scored run as JSON to this path")
    # extensibility lane: diagnostics and moving workspaces between installations
    dr = sub.add_parser("doctor", help="check this installation's health and its profile (LYST_PROFILE) promises")
    dr.add_argument("--json", action="store_true")
    dr.add_argument("--no-network", action="store_true", help="skip reaching the model server")
    ex = sub.add_parser("export", help="seal a workspace into a pack (passphrase from LYST_PACK_PASSPHRASE or a prompt)")
    ex.add_argument("workspace")
    ex.add_argument("out")
    ex.add_argument("--history", action="store_true", help="include tasks, model calls and event history")
    im = sub.add_parser("import", help="create a workspace from a pack (inspect with --check first)")
    im.add_argument("pack")
    im.add_argument("--as", dest="target", default=None, help="workspace id to create (default: the pack's)")
    im.add_argument("--activate", action="store_true", help="run its schedules and monitors here (a migration, not a copy)")
    im.add_argument("--check", action="store_true", help="only verify the pack; change nothing")
    args = p.parse_args(argv)
    try:
        _dispatch(args)
    except MasterKeyMissing as exc:
        sys.exit(f"{exc}\nRun {cmd('init')} on this machine (or open the interface here) to initialise encryption first.")
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # a DuckDB lock held by another process: one plain line instead of a traceback
        from .launch import lock_holder_from_error, lock_message

        holder = lock_holder_from_error(str(exc))
        if holder is None:
            raise
        sys.exit(lock_message(load_settings().home / "control.duckdb", holder))


def _engine_or_local(path: str, method: str = "GET", **kw):
    """Ask the running engine (it holds the database files); without one, open the installation here."""
    import httpx

    settings = load_settings()
    url = os.environ.get("LYST_API_URL", f"http://127.0.0.1:{os.environ.get('LYST_PORT', '8770')}")
    token = os.environ.get("LYST_TOKEN") or ((settings.home / "dev-token").read_text().strip()
                                             if (settings.home / "dev-token").exists() else None)
    try:
        r = httpx.request(method, url + path, headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=900, **kw)
    except httpx.TransportError:
        return None
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        sys.exit(f"The engine refused ({r.status_code}): {detail}")
    return r


def _passphrase(confirm: bool) -> str:
    import getpass

    pw = os.environ.get("LYST_PACK_PASSPHRASE")
    if pw:
        return pw
    pw = getpass.getpass("Pack passphrase: ")
    if confirm and getpass.getpass("Again: ") != pw:
        sys.exit("The passphrases differ.")
    return pw


def _extend_dispatch(args) -> bool:
    if args.cmd == "doctor":
        from . import diagnostics

        r = _engine_or_local("/v1/diagnostics" + ("?network=false" if args.no_network else ""))
        if r is not None:
            report = r.json()
        else:
            from .app import Lyst

            lyst = Lyst.build()
            try:
                report = diagnostics.run(lyst, network=not args.no_network)
            finally:
                lyst.close()
        print(json.dumps(report, indent=1, default=str) if args.json else diagnostics.render(report))
        sys.exit(1 if report["status"] == "unhealthy" else 0)
    if args.cmd == "export":
        pw = _passphrase(confirm=True)
        r = _engine_or_local(f"/v1/workspaces/{args.workspace}/exports", "POST",
                             json={"passphrase": pw, "include_history": args.history})
        if r is not None:
            info = r.json()
            data = _engine_or_local(info["download"]).content
            Path(args.out).write_bytes(data)
        else:
            from .app import Lyst
            from .security import SYSTEM

            lyst = Lyst.build()
            try:
                owner = _local_owner(lyst, args.workspace)
                info = lyst.portability.export(owner or SYSTEM, args.workspace, passphrase=pw, include_history=args.history)
                shutil.copyfile(lyst.portability.export_path(owner or SYSTEM, args.workspace, info["id"]), args.out)
            finally:
                lyst.close()
        print(f"Sealed {args.workspace} into {args.out}: {info['tables']} tables, {info['rows']:,} rows, {info['files']} files, "
              f"{info['bytes'] / 1e6:.1f} MB. Keep the passphrase apart from the pack.")
        return True
    if args.cmd == "import":
        pw = _passphrase(confirm=False)
        content = Path(args.pack).read_bytes()
        path = "/v1/imports/inspect" if args.check else "/v1/imports"
        form = {"passphrase": pw, "activate": str(args.activate).lower()}
        if args.target:
            form["workspace_id"] = args.target
        r = _engine_or_local(path, "POST", files={"file": (Path(args.pack).name, content)}, data=form)
        if r is None:
            sys.exit("Start the engine (`lyst serve`) and set LYST_TOKEN to an installation administrator's token, then "
                     "import again; imports go through the engine so the new workspace is checked and audited.")
        print(json.dumps(r.json(), indent=1, default=str))
        return True
    return False


def _local_owner(lyst, ws: str):
    from .security import Principal

    rows = lyst.stores.control.query("SELECT u.id, u.email FROM memberships m JOIN users u ON u.id = m.user_id "
                                     "WHERE m.workspace_id = ? AND m.role = 'owner' LIMIT 1", [ws])
    return Principal(rows[0]["id"], rows[0]["email"]) if rows else None


def _dispatch(args) -> None:
    if _extend_dispatch(args):
        return
    if args.cmd == "start" and args.check:
        sys.exit(start_checks(args.json))
    elif args.cmd == "start":
        start(demo=args.demo, open_browser=not args.no_browser)
    elif args.cmd == "status":
        sys.exit(status_cmd(args.json))
    elif args.cmd == "stop":
        sys.exit(stop_cmd())
    elif args.cmd == "init-demo":
        init_demo()
    elif args.cmd == "serve":
        from .api import main as serve

        serve()
    elif args.cmd == "token":
        issue_token(args.email, args.name, args.hours, args.owner_of)
    elif args.cmd == "init":
        init_cmd(args.recovery, args.quiet)
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
        run_eval(args.label, baseline=args.baseline, write_baseline=args.write_baseline, json_out=args.json)
    elif args.cmd == "ui":
        app = Path(__file__).parent / "ui" / "app.py"
        settings = load_settings()
        address, local = ui_address(os.environ)
        env = {**os.environ, "LYST_HOME": str(settings.home), "LYST_UI_LOCAL": "1" if local else "0"}
        sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--browser.serverAddress", UI_HOST,
                                  "--server.address", address,
                                  "--server.port", os.environ.get("LYST_UI_PORT", "8501")], env=env,
                                 cwd=Path(__file__).resolve().parents[2]))
