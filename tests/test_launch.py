# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The run record, `lyst status`, `lyst stop`, `lyst start --check`, port choice and the database-lock message.

The safety rule under test: `lyst stop` signals only PIDs that `lyst start` recorded and that still run a Lyst command
line. A PID that was reused by another program, or a Lyst process nobody recorded, is never touched."""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from lyst import launch
from lyst.cli import _port


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {**os.environ, "LYST_HOME": str(home), "LYST_MASTER_KEY": "66" * 32, "LYST_KEYRING": "0",
           "LYST_SECRETS_DIR": str(home.parent / "none"), **extra}
    env.pop("LYST_OPENROUTER_API_KEY", None)
    return env


def _cli(home: Path, *args: str, timeout: float = 60, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", f"from lyst.cli import main; main({list(args)!r})"], env=_env(home, **extra),
                          capture_output=True, text=True, timeout=timeout)


def _sleeper(tmp: Path, name: str) -> subprocess.Popen:
    """A process that sleeps; its command line contains `name` (a name with `lyst.cli` in it looks like Lyst)."""
    proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep(120)  # {name}"])
    deadline = time.time() + 10
    while time.time() < deadline and name not in launch.command_line(proc.pid):
        time.sleep(0.05)
    return proc


def _record(home: Path, launcher: int, *, api_port: int = 1, ui_port: int = 2, children=None, started_at=None) -> launch.RunRecord:
    rec = launch.RunRecord(launcher=launcher, api_port=api_port, ui_port=ui_port, url=f"http://lyst.localhost:{ui_port}",
                           home=str(home), started_at=started_at or time.time() - 3600, children=children or {})
    launch.write_record(home, rec)
    return rec


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# -- the record ---------------------------------------------------------------------------------------------------------

def test_record_round_trip_and_clear_only_own(tmp_path):
    rec = _record(tmp_path, 4242, children={"engine": 4243})
    assert launch.read_record(tmp_path) == rec
    launch.clear_record(tmp_path, launcher=9999)          # a different launcher's clear leaves it alone
    assert launch.read_record(tmp_path) == rec
    launch.clear_record(tmp_path, launcher=4242)
    assert launch.read_record(tmp_path) is None
    launch.run_file(tmp_path).write_text("not json")
    assert launch.read_record(tmp_path) is None            # a damaged record reads as none, never crashes


def test_dead_pids_make_the_record_stale(tmp_path):
    _record(tmp_path, _dead_pid())
    assert launch.inspect(tmp_path).status == "stale"


def test_reused_pid_running_something_else_is_not_lyst_and_is_never_signalled(tmp_path):
    other = _sleeper(tmp_path, "unrelated_tool")
    try:
        assert not launch.is_lyst_process(other.pid)
        _record(tmp_path, other.pid, children={"engine": other.pid})
        assert launch.inspect(tmp_path).status == "stale"
        assert launch.stop(tmp_path, timeout=2) is False
        time.sleep(0.3)
        assert other.poll() is None                        # still running: Lyst didn't start it, so it didn't stop it
        assert launch.read_record(tmp_path) is None
    finally:
        other.kill()
        other.wait()


def test_stop_signals_only_recorded_lyst_pids(tmp_path):
    recorded = _sleeper(tmp_path, "lyst.cli serve (fake)")
    bystander = _sleeper(tmp_path, "lyst.cli serve (unrecorded)")      # a Lyst-looking process nobody recorded
    try:
        _record(tmp_path, recorded.pid)
        assert launch.inspect(tmp_path).status == "unhealthy"   # alive, but nothing answers on its ports
        assert launch.stop(tmp_path, timeout=5) is True
        assert recorded.wait(timeout=10) is not None
        assert bystander.poll() is None
        assert launch.read_record(tmp_path) is None
    finally:
        for p in (recorded, bystander):
            p.kill()
            p.wait()


# -- ports --------------------------------------------------------------------------------------------------------------

def test_pick_port_moves_past_a_busy_one():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        assert not launch.port_free(busy)
        got = launch.pick_port(busy)
        assert got is not None and got != busy and launch.port_free(got)
        assert launch.pick_port(busy, avoid={busy + 1}) not in (busy, busy + 1)


def test_explicit_busy_port_is_a_clear_error_and_a_default_one_moves(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        monkeypatch.setenv("LYST_PORT", str(busy))
        with pytest.raises(SystemExit, match=f"Port {busy} .LYST_PORT. is in use"):
            _port("LYST_PORT", 8770, set())
        monkeypatch.delenv("LYST_PORT")
        port, note = _port("LYST_PORT", busy, set())
        assert port != busy and f"port {busy} was busy" in note


# -- the database lock --------------------------------------------------------------------------------------------------

@pytest.fixture
def held_database(tmp_path):
    """Another process holding DuckDB's lock on home/control.duckdb, the way a second engine would."""
    home = tmp_path / "home"
    home.mkdir()
    db = home / "control.duckdb"
    ready = tmp_path / "ready"
    code = textwrap.dedent(f"""
        import duckdb, pathlib, time
        c = duckdb.connect(':memory:')
        c.execute("ATTACH '{db}' AS d (ENCRYPTION_KEY 'k')")
        c.execute("CREATE TABLE d.t(x INT)")
        pathlib.Path('{ready}').write_text('1')
        time.sleep(120)
    """)
    holder = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.time() + 20
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    yield home, holder
    holder.kill()
    holder.wait()


def test_lock_holder_is_found_and_named(held_database):
    home, holder = held_database
    assert launch.database_lock_holder(home / "control.duckdb") == holder.pid
    state = launch.inspect(home)
    assert state.status == "locked" and state.lock_pid == holder.pid
    assert f"PID {holder.pid}" in state.detail


def test_lock_holder_none_when_free_or_missing(tmp_path):
    assert launch.database_lock_holder(tmp_path / "missing.duckdb") is None
    import duckdb

    c = duckdb.connect(str(tmp_path / "free.duckdb"))
    c.execute("CREATE TABLE t(x INT)")
    c.close()
    assert launch.database_lock_holder(tmp_path / "free.duckdb") is None


def test_lyst_command_lines_are_recognised_and_others_are_not():
    import re

    pat = launch._LYST_CMD
    for cmd in ("/u/lyst/.venv/bin/python /u/lyst/.venv/bin/lyst start --demo",
                "/u/lyst/.venv/bin/python -c from lyst.cli import main; main(['serve'])",
                "/u/lyst/.venv/bin/python -m streamlit run /u/lyst/src/lyst/ui/app.py --server.port 8501"):
        assert pat.search(cmd), cmd
    for cmd in ("/u/lyst/.venv/bin/python /u/lyst/.venv/bin/pytest -q", "/u/lyst/.venv/bin/python other.py", "vim lyst.md"):
        assert not pat.search(cmd), cmd
    assert isinstance(pat, re.Pattern)


def test_describe_pid_is_one_line_for_a_multi_line_command(monkeypatch):
    monkeypatch.setattr(launch, "command_line", lambda pid: "python -c \n    import duckdb\n    time.sleep(1)\n")
    assert launch.describe_pid(7) == "PID 7 (python -c import duckdb time.sleep(1))"
    assert "\n" not in launch.lock_message(Path("/x/control.duckdb"), 7)


def test_lock_message_parses_duckdb_wording():
    msg = ('IO Error: Could not set lock on file "/x/var/control.duckdb": Conflicting lock is held in '
           '/usr/bin/python3.13 (PID 2974) by user someone. See also https://duckdb.org/docs/current/connect/concurrency')
    assert launch.lock_holder_from_error(msg) == 2974
    assert launch.lock_holder_from_error("some other error") is None


def test_start_with_a_held_database_says_one_line_and_no_traceback(held_database):
    home, holder = held_database
    out = _cli(home, "start", "--demo", "--no-browser")
    assert out.returncode == 1
    assert "Traceback" not in out.stderr
    assert f"PID {holder.pid}" in out.stderr and "LYST_HOME" in out.stderr
    assert len(out.stderr.strip().splitlines()) == 1
    assert holder.poll() is None                           # never stopped by Lyst


def test_status_and_stop_with_a_held_database_leave_it_alone(held_database):
    home, holder = held_database
    st = _cli(home, "status")
    assert st.returncode == 1 and f"PID {holder.pid}" in st.stdout
    sp = _cli(home, "stop")
    assert sp.returncode == 1 and "won't stop it" in sp.stderr
    assert holder.poll() is None


# -- commands -----------------------------------------------------------------------------------------------------------

def test_status_and_stop_when_nothing_runs(tmp_path):
    home = tmp_path / "home"
    st = _cli(home, "status")
    assert st.returncode == 1 and "isn't running" in st.stdout
    js = json.loads(_cli(home, "status", "--json").stdout)
    assert js["status"] == "stopped"
    sp = _cli(home, "stop")
    assert sp.returncode == 0 and "isn't running" in sp.stdout


def test_stale_record_is_cleared_by_stop(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _record(home, _dead_pid())
    assert "left over" in _cli(home, "status").stdout
    assert _cli(home, "stop").returncode == 0
    assert launch.read_record(home) is None


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        body = b'{"status":"ok"}' if self.path == "/health" else b"ok"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def test_start_reuses_a_healthy_running_lyst(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Health) for _ in range(2)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    launcher = _sleeper(tmp_path, "lyst.cli start (fake)")
    try:
        api, ui = (s.server_address[1] for s in servers)
        _record(home, launcher.pid, api_port=api, ui_port=ui)
        assert launch.inspect(home).status == "running"
        st = _cli(home, "status")
        assert st.returncode == 0 and f"http://lyst.localhost:{ui}" in st.stdout and str(launcher.pid) in st.stdout
        out = _cli(home, "start", "--demo", "--no-browser", timeout=30)
        assert out.returncode == 0, out.stderr
        assert "already running" in out.stderr and f"http://lyst.localhost:{ui}" in out.stderr
        assert launch.read_record(home).launcher == launcher.pid   # the running one's record is untouched
    finally:
        launcher.kill()
        launcher.wait()
        for s in servers:
            s.shutdown()


def test_start_check_reports_each_check(tmp_path):
    home = tmp_path / "home"
    out = _cli(home, "start", "--check", "--json")
    checks = {c["name"]: c for c in json.loads(out.stdout)}
    assert {"python", "data folder", "encryption key", "database", "engine port", "interface port"} <= set(checks)
    assert checks["encryption key"]["ok"] and checks["database"]["ok"]
    text = _cli(home, "start", "--check")
    assert "encryption key" in text.stdout and text.returncode == out.returncode


def test_commands_are_named_as_the_person_can_type_them(monkeypatch):
    monkeypatch.setenv("UV_RUN_RECURSION_DEPTH", "1")          # launched with `uv run`
    assert launch.cmd("stop") == "`uv run lyst stop`"
    monkeypatch.delenv("UV_RUN_RECURSION_DEPTH")
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)   # `lyst` not on PATH
    assert launch.cmd("status") == "`uv run lyst status`"
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/venv/bin/lyst")
    assert launch.cmd("status") == "`lyst status`"


def test_stale_status_names_the_uv_command_under_uv_run(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _record(home, _dead_pid())
    assert "`uv run lyst start`" in _cli(home, "status", UV_RUN_RECURSION_DEPTH="1").stdout


@pytest.mark.parametrize("value", ["http", "99999", "0"])
def test_a_bad_port_variable_exits_with_a_clear_message(monkeypatch, value):
    monkeypatch.setenv("LYST_PORT", value)
    with pytest.raises(SystemExit, match="LYST_PORT"):
        _port("LYST_PORT", 8770, set())


def test_the_master_key_is_never_written_to_a_pipe(monkeypatch, tmp_path, capsys):
    from lyst import cli

    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="--out"):
        cli._show_key("ab" * 32, None, "Recovery copy")
    assert "ab" * 32 not in capsys.readouterr().out
    path = tmp_path / "key.txt"
    cli._show_key("ab" * 32, str(path), "Recovery copy")
    assert path.read_text().strip() == "ab" * 32 and (path.stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        cli._show_key("cd" * 32, str(path), "Recovery copy")  # never overwrites a saved key
