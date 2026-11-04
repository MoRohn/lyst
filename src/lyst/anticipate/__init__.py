# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Anticipative intelligence: noticing what is likely needed next, preparing it within explicit bounds, and saying why."""

from .policy import LEVELS, OPERATIONS
from .service import AnticipationService

__all__ = ["LEVELS", "OPERATIONS", "AnticipationService"]
