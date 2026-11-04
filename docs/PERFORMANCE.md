# Performance baseline

Measured on 2026-11-04 on the development machine (Apple Silicon laptop, Python 3.13, DuckDB 2.0), one engine
process with `LYST_WORKERS=4`, the Northwind demo workspace, no model configured. `scripts/load_test.py` sends
each simulated person the five-question bank in turn (values, a "why", a breakdown, a forecast and the deep
six-quarter investigation) and polls until done.

| People at once | Tasks | Completed | Submit p50 / p95 | End-to-end p50 / p95 / max | Tasks per second |
|---:|---:|---:|---:|---:|---:|
| 8 (2 rounds) | 16 | 16 | 30 ms / 60 ms | 0.52 s / 0.74 s / 0.93 s | 11.0 |
| 24 (1 round) | 24 | 24 | 110 ms / 119 ms | 1.5 s / 1.95 s / 1.95 s | 12.0 |

What the numbers say: submission is immediate (the UI acknowledges at once; the task runs in the engine),
and four workers finish about twelve demo tasks a second; with 24 people at once the queue, not the
work, sets the end-to-end time. No failures in either run.

What they do not say: nothing here involves a model call (add the provider's latency per call, typically
1 to 10 s, and the gateway's admission limits), the demo data is small (396 rows), and the machine is not a
server. Rerun the script against your deployment before setting objectives.

Suggested starting objectives, to be confirmed by your own runs: submit acknowledgment under 200 ms p95;
fast-path answers under 2 s p95 end to end; deep investigations under 15 s p95 without a model; zero failed
tasks under normal load.
