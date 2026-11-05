# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What `lyst start`, `lyst start --check`, `lyst status` and `lyst stop` share: the run record, port choice, the database
lock probe and the preflight checks.

The run record (`LYST_HOME/lyst.run.json`) names the processes `lyst start` itself launched. `lyst stop` signals only
those PIDs, and only while each still runs a Lyst command line, so a reused PID or someone else's process is never
touched. Nothing here kills by name.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

RUN_FILE = "lyst.run.json"
DEFAULT_API_PORT = 8770
DEFAULT_UI_PORT = 8501
STARTING_GRACE_S = 120   # a record younger than this whose processes are alive but not answering yet is "starting"


@dataclass
class RunRecord:
    launcher: int                 # the `lyst start` process; signalling it stops the rest cleanly
    api_port: int
    ui_port: int
    url: str
    home: str
    started_at: float
    children: dict[str, int] = field(default_factory=dict)   # {"engine": pid, "interface": pid}

    def pids(self) -> list[int]:
        return [self.launcher, *self.children.values()]


def run_file(home: Path) -> Path:
    return Path(home) / RUN_FILE


def read_record(home: Path) -> RunRecord | None:
    try:
        data = json.loads(run_file(home).read_text())
        return RunRecord(**data)
    except (OSError, ValueError, TypeError):
        return None


def write_record(home: Path, rec: RunRecord) -> None:
    path = run_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(rec)))
    os.replace(tmp, path)


def clear_record(home: Path, *, launcher: int | None = None) -> None:
    """Remove the record; with `launcher`, only if it is still that launcher's (a newer start may have replaced it)."""
    if launcher is not None:
        rec = read_record(home)
        if rec is not None and rec.launcher != launcher:
            return
    try:
        run_file(home).unlink()
    except FileNotFoundError:
        pass


# -- how to name a command to this person ------------------------------------------------------------------------------

def via_uv() -> bool:
    """True when `lyst` was launched through `uv run`, or the `lyst` script isn't on PATH, so the bare command would
    give "command not found"."""
    return bool(os.environ.get("UV_RUN_RECURSION_DEPTH")) or shutil.which("lyst") is None


def cmd(args: str) -> str:
    """A Lyst command as the person should type it here, in backticks: `uv run lyst stop` or `lyst stop`."""
    return f"`{'uv run ' if via_uv() else ''}lyst {args}`"


# -- processes ---------------------------------------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def command_line(pid: int) -> str:
    """The process's command line, or '' if it is gone or cannot be read."""
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.parent.parent.is_dir() and Path("/proc/self").exists():   # Linux
        try:
            return proc.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            return ""
    ps = shutil.which("ps") or "/bin/ps"   # macOS and the BSDs
    try:
        out = subprocess.run([ps, "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


# The command lines `lyst start` launches: itself (`.../bin/lyst start` or `lyst.cli ... start`), the engine
# (`lyst.cli ... serve`) and the interface (`streamlit run .../lyst/ui/app.py`). A bare "lyst" substring is not enough:
# any Python in a checkout called lyst would match.
_LYST_CMD = re.compile(r"lyst\.cli|(?:^|[/\s])lyst\s+(?:start|serve|ui)\b|lyst/ui/app\.py")


def is_lyst_process(pid: int) -> bool:
    """Alive and running one of the Lyst command lines above."""
    return pid_alive(pid) and bool(_LYST_CMD.search(command_line(pid)))


# -- ports ------------------------------------------------------------------------------------------------------------

def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(preferred: int, *, avoid: set[int] = frozenset(), tries: int = 50) -> int | None:
    """`preferred` if it is free, otherwise the next free port above it."""
    for port in range(preferred, preferred + tries):
        if port not in avoid and port_free(port):
            return port
    return None


def http_ok(url: str, timeout: float = 2.0) -> dict | None:
    """The JSON body (or {}) of a 2xx answer at `url`, or None."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - loopback only
            body = r.read()
    except Exception:  # noqa: BLE001 - not answering
        return None
    try:
        return json.loads(body) if body.strip().startswith(b"{") else {}
    except ValueError:
        return {}


# -- the database lock ------------------------------------------------------------------------------------------------

_LOCK_PID = re.compile(r"Conflicting lock is held in (?P<exe>.*?) \(PID (?P<pid>\d+)\)")


def lock_holder_from_error(message: str) -> int | None:
    m = _LOCK_PID.search(message)
    return int(m.group("pid")) if m else None


def database_lock_holder(path: Path) -> int | None:
    """The PID holding DuckDB's lock on `path`, or None if it is free (or missing). A read-only attach without the key
    fails on the lock before it needs the key, so this works on encrypted files and never changes them."""
    if not Path(path).exists():
        return None
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        quoted = str(path).replace("'", "''")
        con.execute(f"ATTACH '{quoted}' AS probe (READ_ONLY)")
    except duckdb.Error as exc:
        return lock_holder_from_error(str(exc))
    finally:
        con.close()
    return None


def describe_pid(pid: int) -> str:
    """`PID n (command)` on one line: a `python -c` script's newlines and indentation collapse to single spaces."""
    cmd = " ".join(command_line(pid).split())
    return f"PID {pid} ({cmd[:120]})" if cmd else f"PID {pid}"


def lock_message(path: Path, pid: int) -> str:
    """The one line a person sees when another process has Lyst's database open."""
    who = describe_pid(pid)
    if is_lyst_process(pid):
        return (f"Lyst's data in {Path(path).parent} is already open in another Lyst process, {who}. "
                f"Stop that one first (Ctrl+C in its terminal, or `kill {pid}`), or point this one elsewhere with LYST_HOME.")
    return (f"Another program, {who}, has {path} open, so Lyst can't use it. "
            f"Close that program (or `kill {pid}` if you're sure), or use a different LYST_HOME.")


# -- the state of this home -------------------------------------------------------------------------------------------

@dataclass
class State:
    status: str                    # running | starting | unhealthy | stale | stopped | locked
    record: RunRecord | None = None
    lock_pid: int | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail, "lock_pid": self.lock_pid,
                "record": asdict(self.record) if self.record else None}


def inspect(home: Path) -> State:
    """What is running against this LYST_HOME, from the run record, the processes, the health checks and the lock."""
    home = Path(home)
    rec = read_record(home)
    if rec is not None:
        alive = {pid: is_lyst_process(pid) for pid in rec.pids()}
        if not any(alive.values()):
            return State("stale", rec, detail=f"A record from an earlier Lyst (PID {rec.launcher}) is left over; "
                                                  "none of its processes are running.")
        api = http_ok(f"http://127.0.0.1:{rec.api_port}/health")
        ui = http_ok(f"http://127.0.0.1:{rec.ui_port}/_stcore/health")
        if api is not None and ui is not None and alive.get(rec.launcher):
            return State("running", rec, detail=f"Lyst is running at {rec.url}")
        if time.time() - rec.started_at < STARTING_GRACE_S and alive.get(rec.launcher):
            return State("starting", rec, detail=f"Lyst is still starting (PID {rec.launcher}).")
        down = [name for name, ok in (("engine", api is not None), ("interface", ui is not None)) if not ok]
        if not alive.get(rec.launcher):
            down.insert(0, "the `lyst start` process")
        return State("unhealthy", rec, detail=f"Lyst (PID {rec.launcher}) is only partly running: {', '.join(down)} "
                                                 "not answering.")
    holder = database_lock_holder(home / "control.duckdb")
    if holder is not None:
        return State("locked", lock_pid=holder, detail=lock_message(home / "control.duckdb", holder))
    return State("stopped", detail="Lyst isn't running.")


def stop(home: Path, *, timeout: float = 20.0, say=print) -> bool:
    """Stop the processes this home's run record names. Returns True if anything was stopped. Only recorded PIDs that
    still run a Lyst command line are signalled: the launcher first (it shuts its children down), then any child left."""
    rec = read_record(home)
    if rec is None:
        return False
    targets = [pid for pid in rec.pids() if is_lyst_process(pid)]
    if not targets:
        clear_record(home)
        return False
    order = [rec.launcher] if rec.launcher in targets else []
    order += [pid for pid in targets if pid not in order]
    for i, pid in enumerate(order):
        if not pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        if i == 0 and pid == rec.launcher:   # give the launcher the chance to stop its children itself
            _wait_gone([pid, *rec.children.values()], timeout)
    _wait_gone(order, timeout)
    for pid in order:
        if pid_alive(pid) and is_lyst_process(pid):
            say(f"PID {pid} did not stop in {timeout:.0f}s; forcing it.")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    _wait_gone(order, 5)
    clear_record(home)
    return True


def _wait_gone(pids: list[int], timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.2)


# -- preflight (`lyst start --check`; it opens no database, so a fuller diagnostics command can run it first) ----------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def preflight(home: Path, *, api_port: int = DEFAULT_API_PORT, ui_port: int = DEFAULT_UI_PORT) -> list[Check]:
    """Quick checks that explain a failed start before it happens. Each is cheap and has no side effects."""
    import sys

    from .secrets import masterkey

    home = Path(home)
    checks = [Check("python", sys.version_info >= (3, 12), f"Python {sys.version.split()[0]}", "Lyst needs Python 3.12 or newer.")]
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write-probe"
        probe.write_text("")
        probe.unlink()
        checks.append(Check("data folder", True, str(home)))
    except OSError as exc:
        checks.append(Check("data folder", False, f"{home}: {exc.strerror or exc}", "Set LYST_HOME to a folder you can write to."))
    try:
        mk = masterkey.resolve(home)
    except Exception as exc:  # noqa: BLE001 - a malformed key is still a finding, not a crash
        mk, err = None, str(exc)
    else:
        err = ""
    if mk is not None:
        checks.append(Check("encryption key", True, "found"))
    elif masterkey.keyring_available():
        checks.append(Check("encryption key", True, err or "not set yet; " + cmd("start") + f" creates it in the {masterkey.keyring_available()} credential store"))
    else:
        checks.append(Check("encryption key", False, err or "none found and no OS credential store", f"Run {cmd('init')}, or mount the key (docs/SECURITY.md)."))
    state = inspect(home)
    if state.status in ("running", "starting"):
        checks.append(Check("already running", True, state.detail))
    elif state.status == "locked":
        checks.append(Check("database", False, state.detail))
    elif state.status == "unhealthy":
        checks.append(Check("already running", False, state.detail, f"Run {cmd('stop')}, then start again."))
    else:
        checks.append(Check("database", True, "not in use"))
    ours = {state.record.api_port, state.record.ui_port} if state.record and state.status != "stale" else set()
    for label, port in (("engine port", api_port), ("interface port", ui_port)):
        if port in ours or port_free(port):
            checks.append(Check(label, True, f"{port} {'used by this Lyst' if port in ours else 'free'}"))
        else:
            alt = pick_port(port, avoid={api_port, ui_port})
            checks.append(Check(label, True, f"{port} is busy; {cmd('start')} will use {alt}" if alt else f"{port} is busy"))
    return checks
