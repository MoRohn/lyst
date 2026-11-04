# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Structured logs and Prometheus metrics.

Logs are one JSON object per line on stderr (``LYST_LOG_FORMAT=text`` for development), with the
request id, task id and workspace when known. Metrics are served at ``/metrics`` in Prometheus text
format: HTTP requests by route and status, task outcomes and step durations, model calls by outcome,
queue depth, and the gateway's in-flight count.
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
import time
import uuid

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from pythonjsonlogger.json import JsonFormatter

request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_id", default=None)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        record.task_id = task_id.get()
        return True


def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_lyst_configured", False):
        return
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("LYST_LOG_FORMAT", "json") == "json":
        handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(task_id)s",
                                           rename_fields={"asctime": "time", "levelname": "level", "name": "logger"}))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(task_id)s] %(message)s"))
    handler.addFilter(_ContextFilter())
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("LYST_LOG_LEVEL", "INFO"))
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel("WARNING")
    root._lyst_configured = True  # type: ignore[attr-defined]


REGISTRY = CollectorRegistry()
HTTP_REQUESTS = Counter("lyst_http_requests_total", "HTTP requests", ["route", "method", "status"], registry=REGISTRY)
HTTP_LATENCY = Histogram("lyst_http_request_seconds", "HTTP request latency", ["route"], registry=REGISTRY,
                         buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
TASKS = Counter("lyst_tasks_total", "Tasks by final status", ["status"], registry=REGISTRY)
STEP_SECONDS = Histogram("lyst_step_seconds", "Capability step duration", ["capability"], registry=REGISTRY,
                         buckets=(0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 15, 60))
MODEL_CALLS = Counter("lyst_model_calls_total", "Upstream model calls", ["outcome", "pool"], registry=REGISTRY)
MODEL_COST = Counter("lyst_model_cost_usd_total", "Model spend in USD", registry=REGISTRY)
QUEUE_DEPTH = Gauge("lyst_tasks_queued", "Tasks waiting for a worker", registry=REGISTRY)
INFLIGHT = Gauge("lyst_model_inflight", "Model calls in flight", registry=REGISTRY)
WORKERS = Gauge("lyst_workers", "Worker threads", registry=REGISTRY)


class Timer:
    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.start


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
