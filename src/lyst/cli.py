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


def run_eval(label: str) -> None:
    import tempfile

    from . import demo, evals
    from .app import Lyst

    with tempfile.TemporaryDirectory() as home:
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
    elif args.cmd == "eval":
        run_eval(args.label)
    elif args.cmd == "ui":
        app = Path(__file__).parent / "ui" / "app.py"
        settings = load_settings()
        env = {**os.environ, "LYST_HOME": str(settings.home)}
        sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app),
                                  "--server.port", os.environ.get("LYST_UI_PORT", "8501")], env=env,
                                 cwd=Path(__file__).resolve().parents[2]))
