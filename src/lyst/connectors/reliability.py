# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""One error vocabulary and one retry policy for every connector.

Connectors fail in a handful of ways that matter to the person who owns the connection: the credential is wrong or
expired (**auth**), the account may not read the object (**permission**), the source asks Lyst to slow down
(**rate_limit**), the network or the source is down (**network**, **timeout**, **source**), the table or file is
gone (**not_found**), its columns changed (**schema**), or the connection's settings are wrong (**config**).
``classify`` maps any connector error to one of these, from a category the connector set itself or from the
message, and ``ACTIONS`` says what the person should do in a sentence. Only network, timeout, rate-limit and
source errors are retried, with exponential backoff and jitter, honouring a source's Retry-After up to a cap.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

CATEGORIES = ("auth", "permission", "rate_limit", "timeout", "network", "source", "not_found", "schema", "config", "unknown")
TRANSIENT = frozenset({"rate_limit", "timeout", "network", "source"})

ACTIONS = {
    "auth": "Reconnect: replace the credential in Settings › Secrets & Connections, then test the connection.",
    "permission": "Ask the source system's administrator to grant this account read access, or allow the host.",
    "rate_limit": "Nothing to do now: Lyst waited and retried. If it keeps happening, sync less often.",
    "timeout": "Check that the source is reachable and not overloaded; Lyst retries automatically.",
    "network": "Check that the source is reachable from the Lyst server; Lyst retries automatically.",
    "source": "The source system reported an error of its own; Lyst retries automatically. If it persists, contact its owner.",
    "not_found": "The table or file was renamed or removed at the source; choose it again under Sources.",
    "schema": "The source's columns changed; review the dataset's contract check before relying on new numbers.",
    "config": "Fix the connection's settings and test it again.",
    "unknown": "Test the connection; if it still fails, the message above says what the source reported.",
}

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # specific source messages first: a connection-time failure is often wrapped in "could not connect"
    ("auth", re.compile(r'role "[^"]*" does not exist|no password supplied|password authentication failed', re.I)),
    ("config", re.compile(r'database "[^"]*" does not exist', re.I)),
    ("rate_limit", re.compile(r"\b429\b|rate.?limit|too many requests|throttl", re.I)),
    ("auth", re.compile(r"\b401\b|password authentication failed|authentication|unauthori[sz]ed|invalid (api )?key|"
                        r"invalid token|token (has )?expired|credential (unavailable|revoked)|was revoked|login failed", re.I)),
    ("permission", re.compile(r"\b403\b|permission denied|forbidden|not allowed|access denied|insufficient privilege|"
                              r"not in LYST_REST_ALLOWED_HOSTS|outside the import root", re.I)),
    ("timeout", re.compile(r"timed? ?out|timeout|canceling statement", re.I)),
    ("schema", re.compile(r"column .* does not exist|no such column|schema (changed|mismatch)|type mismatch|could not convert", re.I)),
    ("not_found", re.compile(r"\b404\b|does not exist|not found|no such (table|file)|unknown table", re.I)),
    ("network", re.compile(r"could not connect|connection (refused|reset|closed|error)|connecterror|name or service not known|"
                           r"nodename nor servname|network is unreachable|no route to host|temporary failure in name resolution|"
                           r"server closed the connection|ssl", re.I)),
    ("source", re.compile(r"\b5\d\d\b|internal server error|service unavailable|bad gateway", re.I)),
    ("config", re.compile(r"is required|invalid |must be https|unknown connector|unsupported", re.I)),
]


def classify(exc: BaseException | str) -> str:
    category = getattr(exc, "category", None)
    if category in CATEGORIES:
        return category
    text = str(exc) or type(exc).__name__ if not isinstance(exc, str) else exc
    for name, rx in _PATTERNS:
        if rx.search(text):
            return name
    return "unknown"


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_seconds: float = 0.5
    max_seconds: float = 30.0
    jitter: float = 0.2

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(self.max_seconds, max(0.0, float(retry_after)))
        d = min(self.max_seconds, self.base_seconds * (2 ** (attempt - 1)))
        return d * (1 + random.uniform(-self.jitter, self.jitter))  # noqa: S311 - backoff jitter, not security


DEFAULT_POLICY = RetryPolicy()


@dataclass
class Attempted:
    """What happened across the attempts of one operation: how many there were and the categories of the failures."""
    attempts: int = 0
    failures: list[str] | None = None
    waited: float = 0.0


def run_with_retry[T](fn: Callable[[], T], policy: RetryPolicy = DEFAULT_POLICY, *, sleep: Callable[[float], None] = time.sleep,
                   record: Attempted | None = None) -> T:
    """Call ``fn``; on a transient failure wait and try again, up to ``policy.attempts`` times in all. A permanent
    failure (auth, permission, not found, schema, config) raises at once: retrying it only hides the cause."""
    record = record if record is not None else Attempted()
    record.failures = record.failures or []
    for attempt in range(1, max(1, policy.attempts) + 1):
        record.attempts = attempt
        try:
            return fn()
        except Exception as exc:
            category = classify(exc)
            record.failures.append(category)
            if category not in TRANSIENT or attempt >= policy.attempts:
                raise
            wait = policy.delay(attempt, getattr(exc, "retry_after", None))
            record.waited += wait
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


__all__ = ["ACTIONS", "CATEGORIES", "DEFAULT_POLICY", "TRANSIENT", "Attempted", "RetryPolicy", "classify", "run_with_retry"]
