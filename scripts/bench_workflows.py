# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Measure what repeated analytical work costs: model calls, SQL queries, steps and wall time per task.

    uv run python scripts/bench_workflows.py                 # compiled workflows on (the default)
    LYST_WORKFLOWS=0 uv run python scripts/bench_workflows.py  # the same bank with compilation off

Runs in-process against a fresh demo workspace with a stub model on a local endpoint, so every number
is counted, not estimated: a "model call" is one request the gateway sent to the stub. The stub maps
an unfamiliar term to operating margin (what a real model would do) and writes a numberless narrative.
The bank asks the same three analyses for several periods, as a person repeating a monthly or
quarterly review would. Wall time is this machine's and excludes real model latency.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

BANK = [
    "Why did profitability fall in {p}?",
    "What was revenue in {p} by region?",
    "Why did operating margin decline in {p}?",
]
PERIODS = ["Q4 2025", "Q1 2026", "Q2 2026", "Q3 2026"]


def main() -> None:
    os.environ.setdefault("LYST_MASTER_KEY", "ef" * 32)
    os.environ["LYST_KEYRING"] = "0"
    for k in ("LYST_OPENROUTER_API_KEY", "LYST_LLM_BASE_URL", "LYST_LLM_MODEL", "LYST_EMBED_MODEL"):
        os.environ.pop(k, None)
    from lyst import demo
    from lyst.app import Lyst
    from lyst.config import LLMSettings, load_settings
    from lyst.llm import LLMClient

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("response_format"):
            calls.append("classify")
            text = json.dumps({"metrics": ["operating_margin"], "intent": "explain"})
        else:
            calls.append("narrative")
            text = "The decline is concentrated in one region; the evidence below has the figures."
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {}})

    with tempfile.TemporaryDirectory() as home:
        app = Lyst.build(load_settings(Path(home)))
        try:
            extra = {"credentials": app.credentials} if hasattr(app, "credentials") else {}
            app.services.llm = LLMClient(LLMSettings("http://localhost:11434/v1", "stub"), app.stores,
                                         transport=httpx.MockTransport(handler), **extra)
            owner = app.guard.create_user("bench@northwind.test", "Bench")
            demo.seed(app, owner)
            ws = app.stores.workspace("northwind")
            rows = []
            for p in PERIODS:
                for q in BANK:
                    question = q.format(p=p)
                    before_calls, before_q = len(calls), ws.scalar("SELECT count(*) FROM query_history")
                    t0 = time.perf_counter()
                    tid = app.runtime.submit(owner, "northwind", question)
                    app.runtime.drain()
                    secs = time.perf_counter() - t0
                    t = app.runtime.get(owner, tid)
                    r = t["result"] or {}
                    rows.append({
                        "question": question, "status": t["status"], "answer_status": r.get("status"),
                        "confidence": (r.get("confidence") or {}).get("level"),
                        "answer": (r.get("answer") or "")[:90],
                        "workflow": ((r.get("supporting") or {}).get("workflow") or {}).get("used"),
                        "model_calls": len(calls) - before_calls,
                        "queries": ws.scalar("SELECT count(*) FROM query_history") - before_q,
                        "steps": len(t["steps"]), "seconds": round(secs, 3),
                    })
        finally:
            app.close()
    for r in rows:
        print(json.dumps(r))
    first = rows[: len(BANK)]
    repeat = rows[len(BANK):]

    def agg(rs):
        return {"tasks": len(rs), "model_calls": sum(r["model_calls"] for r in rs), "queries": sum(r["queries"] for r in rs),
                "steps": sum(r["steps"] for r in rs), "median_seconds": round(statistics.median(r["seconds"] for r in rs), 3),
                "verified": sum(r["confidence"] == "verified" for r in rs)}

    print(json.dumps({"compilation": os.environ.get("LYST_WORKFLOWS", "1") != "0", "first_period": agg(first),
                      "repeat_periods": agg(repeat), "all": agg(rows)}, indent=1))


if __name__ == "__main__":
    main()
