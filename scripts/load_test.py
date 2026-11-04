# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Load test against a running Lyst API: N people asking at once, each sending the question bank in turn.

    uv run python scripts/load_test.py --url http://127.0.0.1:8770 --token $(cat var/dev-token) --users 8 --rounds 2

Reports submit latency (immediate acknowledgment), end-to-end completion time (p50/p95), throughput and
failures. Numbers describe this machine and this demo data; set production objectives from your own run.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import threading
import time

import httpx

QUESTIONS = [
    "What was revenue last quarter?",
    "Why did operating margin decline last quarter?",
    "What was revenue last quarter by region?",
    "Forecast revenue",
    "Investigate our declining operating margins across the last six quarters, isolate the strongest contributing "
    "factors, test alternative explanations, and prepare an executive briefing.",
]


def run(url: str, token: str, workspace: str, users: int, rounds: int, timeout: float, ramp: float = 0.0,
        think: float = 0.0) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    submit_ms: list[float] = []
    e2e_s: list[float] = []
    failures: list[str] = []
    lock = threading.Lock()

    def person(i: int) -> None:
        # people arrive spread over the ramp and pause between questions; 0 and 0 is a burst (everyone at once)
        rnd = random.Random(i)  # noqa: S311 - load shaping, not security
        time.sleep(rnd.uniform(0, ramp))
        with httpx.Client(base_url=url, timeout=30) as c:
            for r in range(rounds):
                if r:
                    time.sleep(rnd.expovariate(1 / think) if think else 0)
                q = QUESTIONS[(i + r) % len(QUESTIONS)]
                t0 = time.perf_counter()
                resp = c.post(f"/v1/workspaces/{workspace}/tasks", json={"question": q}, headers=h)
                t1 = time.perf_counter()
                if resp.status_code != 202:
                    with lock:
                        failures.append(f"submit {resp.status_code}")
                    continue
                tid = resp.json()["task_id"]
                status = "running"
                while time.perf_counter() - t0 < timeout:
                    t = c.get(f"/v1/tasks/{tid}", headers=h).json()
                    status = t["status"]
                    if status in ("done", "failed", "cancelled"):
                        break
                    time.sleep(0.2)
                with lock:
                    submit_ms.append((t1 - t0) * 1000)
                    if status == "done":
                        e2e_s.append(time.perf_counter() - t0)
                    else:
                        failures.append(f"{status}: {t.get('error')}")

    start = time.perf_counter()
    threads = [threading.Thread(target=person, args=(i,)) for i in range(users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - start

    def pct(xs: list[float], p: float) -> float | None:
        if not xs:
            return None
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]

    return {"users": users, "rounds": rounds, "ramp_s": ramp, "think_s": think, "tasks": users * rounds, "completed": len(e2e_s), "failures": failures,
            "wall_seconds": round(wall, 2), "tasks_per_second": round(len(e2e_s) / wall, 2) if wall else None,
            "submit_ms_p50": round(statistics.median(submit_ms), 1) if submit_ms else None,
            "submit_ms_p95": round(pct(submit_ms, 0.95), 1) if submit_ms else None,
            "e2e_s_p50": round(statistics.median(e2e_s), 2) if e2e_s else None,
            "e2e_s_p95": round(pct(e2e_s, 0.95), 2) if e2e_s else None,
            "e2e_s_max": round(max(e2e_s), 2) if e2e_s else None}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--token", required=True)
    ap.add_argument("--workspace", default="northwind")
    ap.add_argument("--users", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--ramp", type=float, default=0.0, help="spread arrivals over this many seconds (0: all at once)")
    ap.add_argument("--think", type=float, default=0.0, help="mean pause between one person's questions, seconds")
    a = ap.parse_args()
    print(json.dumps(run(a.url, a.token, a.workspace, a.users, a.rounds, a.timeout, a.ramp, a.think), indent=1))
