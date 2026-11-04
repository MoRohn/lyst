# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""`lyst start`: one process brings up the engine and the interface, reports the Lyst address, and stops both on Ctrl-C."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_start_brings_up_engine_and_interface_and_stops_them(tmp_path):
    api_port, ui_port = _free_port(), _free_port()
    env = {**os.environ, "LYST_HOME": str(tmp_path / "home"), "LYST_MASTER_KEY": "66" * 32, "LYST_KEYRING": "0",
           "LYST_SECRETS_DIR": str(tmp_path / "none"), "LYST_PORT": str(api_port), "LYST_UI_PORT": str(ui_port), "LYST_WORKERS": "1"}
    env.pop("LYST_OPENROUTER_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "-c", "from lyst.cli import main; main(['start', '--no-browser'])"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    lines: list[str] = []
    banner = threading.Event()

    def _read():
        for line in proc.stderr:
            lines.append(line)
            if "Lyst is running at" in line:
                banner.set()

    threading.Thread(target=_read, daemon=True).start()
    try:
        deadline = time.time() + 150
        while not banner.is_set() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.3)
        assert banner.is_set(), (proc.poll(), "".join(lines)[-3000:])
        with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/health", timeout=5) as r:
            assert json.loads(r.read())["status"] == "ok"
        with urllib.request.urlopen(f"http://127.0.0.1:{ui_port}/_stcore/health", timeout=5) as r:
            assert r.read().decode() == "ok"
        assert socket.gethostbyname("lyst.localhost") == "127.0.0.1"  # the branded address is this machine
        assert any(f"http://lyst.localhost:{ui_port}" in line for line in lines)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    deadline = time.time() + 20
    while time.time() < deadline:  # both children are gone: nothing listens on either port any more
        try:
            for port in (api_port, ui_port):
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
            time.sleep(0.5)
        except OSError:
            break
    for port in (api_port, ui_port):
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
