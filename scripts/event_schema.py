# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Write the event envelope's JSON Schema to docs/events/envelope.schema.json (checked by tests/test_event_engine.py)."""

import json
from pathlib import Path

from lyst.events import Event

out = Path(__file__).resolve().parents[1] / "docs" / "events" / "envelope.schema.json"
out.write_text(json.dumps(Event.model_json_schema(by_alias=True), indent=2) + "\n")
print(f"wrote {out}")
