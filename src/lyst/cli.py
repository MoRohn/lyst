# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""lyst init-demo | serve | ui | token"""

from __future__ import annotations

import argparse
import os
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


def init_demo() -> None:
    from . import demo
    from .app import Lyst

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


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="lyst")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-demo", help="create the demo workspace and a development token")
    sub.add_parser("serve", help="run the API and workers")
    sub.add_parser("ui", help="run the Streamlit interface")
    args = p.parse_args(argv)
    if args.cmd == "init-demo":
        init_demo()
    elif args.cmd == "serve":
        from .api import main as serve

        serve()
    elif args.cmd == "ui":
        app = Path(__file__).parent / "ui" / "app.py"
        settings = load_settings()
        env = {**os.environ, "LYST_HOME": str(settings.home)}
        sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app),
                                  "--server.port", os.environ.get("LYST_UI_PORT", "8501")], env=env,
                                 cwd=Path(__file__).resolve().parents[2]))
