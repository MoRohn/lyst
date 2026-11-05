# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Run the representative tasks (specialists/representative.py) and record which disciplines passed.

    uv run python scripts/evidence_specializations.py           # print results
    uv run python scripts/evidence_specializations.py --write   # record src/lyst/specialists/evidence.json

Rules-only on a throwaway demo workspace plus the synthetic samples; no model provider is called.
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
    from lyst.specialists import representative, samples

    with tempfile.TemporaryDirectory() as home:
        lyst = Lyst.build(load_settings(Path(home) / "home"), lease_seconds=30)
        try:
            owner = lyst.guard.create_user("evidence@lyst.local", "Evidence")
            demo.seed(lyst, owner)
            samples.load_all(lyst, owner, "northwind")
            results = representative.run(lyst, owner, "northwind")
        finally:
            lyst.close()
    for pid, r in results.items():
        bad = [c["check"] for s in r["scenarios"] for c in s["checks"] if not c["passed"]]
        print(f"{'PASS' if r['passed'] else 'FAIL'} {pid}" + (f"  ({'; '.join(bad)})" if bad else ""))
    if "--write" in sys.argv:
        path = ROOT / "src" / "lyst" / "specialists" / "evidence.json"
        path.write_text(json.dumps(representative.evidence_entries(results), indent=1) + "\n")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
