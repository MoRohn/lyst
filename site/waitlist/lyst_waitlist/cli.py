"""Operator CLI: run the server, manage subscribers, apply retention. Never exposed over HTTP."""

from __future__ import annotations

import argparse
import json
import sys

from cryptography.fernet import Fernet

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lyst-waitlist")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the API (always a single worker: DuckDB has one writer).")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    sub.add_parser("genkey", help="Print a new Fernet encryption key.")
    sub.add_parser("count", help="Print registration counts.")
    sub.add_parser("export", help="Write all registrations as JSON lines to stdout (decrypted).")
    r = sub.add_parser("remove", help="Delete every registration for an email address.")
    r.add_argument("email")
    sub.add_parser("purge", help="Delete registrations older than LYST_WAITLIST_RETENTION_DAYS.")
    a = p.parse_args(argv)

    if a.cmd == "genkey":
        print(Fernet.generate_key().decode())
        return 0

    settings = Settings.from_env()
    if a.cmd == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(settings), host=a.host, port=a.port, workers=1, proxy_headers=settings.trust_proxy, access_log=False)
        return 0

    from .store import Store

    store = Store(settings.db_path, settings.hash_key, settings.encryption_key)
    try:
        if a.cmd == "count":
            print(json.dumps({"early_access": store.count("early_access"), "design_partner": store.count("design_partner")}))
        elif a.cmd == "export":
            for rec in store.export():
                sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        elif a.cmd == "remove":
            print(json.dumps({"removed": store.remove(a.email)}))
        elif a.cmd == "purge":
            print(json.dumps({"purged": store.purge_older_than(settings.retention_days), "retention_days": settings.retention_days}))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
