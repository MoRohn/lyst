# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The HTTP API stays compatible with what callers and the SDK rely on (docs/api/contract.txt)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("api_contract", ROOT / "scripts" / "api_contract.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def test_no_breaking_change_against_the_record():
    recorded = contract.parse(contract.RECORD.read_text())
    problems = contract.breaking(recorded, contract.operations())
    assert not problems, ("Breaking API change. Keep the old shape, or bump API_VERSION in lyst/api/extend.py and rerun "
                          "scripts/api_contract.py --write:\n" + "\n".join(problems))


def test_the_check_catches_removals_and_new_required_fields():
    was = {"GET /a": {"query": [], "body": []}, "POST /b": {"query": [], "body": ["x"]}}
    now = {"POST /b": {"query": ["since"], "body": ["x", "y"]}, "GET /c": {"query": ["q"], "body": []}}
    assert contract.breaking(was, now) == ["GET /a: removed", "POST /b: new required query since", "POST /b: new required body y"]
    assert contract.parse(contract.render(now)) == now
