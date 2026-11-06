# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Run every specialization's example requests on a fresh demo workspace and record what happened.

    uv run python scripts/audit_specializations.py           # print the summary
    uv run python scripts/audit_specializations.py --write   # also write docs/quality/specializations-audit.json
                                                              # and docs/SPECIALIZATIONS_AUDIT.md

Uses a throwaway home with an ephemeral key; no model provider is called (the run is rules-only, so it is
reproducible and free).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    os.environ.update(LYST_MASTER_KEY=secrets.token_hex(32), LYST_KEYRING="0", LYST_CATALOG_AUTO="0")
    for k in ("LYST_OPENROUTER_API_KEY", "LYST_LLM_BASE_URL", "LYST_LLM_MODEL"):
        os.environ.pop(k, None)
    from lyst import demo
    from lyst.app import Lyst
    from lyst.config import load_settings
    from lyst.specialists import audit, packs, samples

    with tempfile.TemporaryDirectory() as home:
        lyst = Lyst.build(load_settings(Path(home) / "home"), lease_seconds=30)
        try:
            owner = lyst.guard.create_user("audit@lyst.local", "Audit")
            demo.seed(lyst, owner)
            samples.load_all(lyst, owner, "northwind")  # synthetic data for the specialist methods
            packs.load_all(lyst, owner, "northwind")  # domain packs with planted causes
            report = audit.run(lyst, owner, "northwind")
        finally:
            lyst.close()
    print(json.dumps(report["summary"], indent=1))
    if "--write" in sys.argv:
        (ROOT / "docs" / "quality").mkdir(parents=True, exist_ok=True)
        stable = {k: v for k, v in report.items() if k != "seconds"}
        (ROOT / "docs" / "quality" / "specializations-audit.json").write_text(json.dumps(stable, indent=1) + "\n")
        (ROOT / "docs" / "SPECIALIZATIONS_AUDIT.md").write_text(audit.markdown(report))
        print("wrote docs/quality/specializations-audit.json and docs/SPECIALIZATIONS_AUDIT.md")


if __name__ == "__main__":
    main()
