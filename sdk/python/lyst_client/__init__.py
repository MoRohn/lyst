# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Python client for the Lyst HTTP API. See README.md."""

from .client import (
    API_VERSION,
    AuthError,
    Invalid,
    Lyst,
    LystError,
    NotFound,
    QueryResult,
    RateLimited,
    ServerError,
    TaskFailed,
    Workspace,
    verify_signature,
)

__version__ = "0.1.0"
__all__ = ["API_VERSION", "AuthError", "Invalid", "Lyst", "LystError", "NotFound", "QueryResult", "RateLimited",
           "ServerError", "TaskFailed", "Workspace", "verify_signature"]
