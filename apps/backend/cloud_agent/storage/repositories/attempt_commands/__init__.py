"""Postgres implementation of the attempt lifecycle port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.

The repository is composed of one mixin per concern: claiming a turn, an
attempt's lifecycle, what a running attempt reports, and the reaper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage.repositories.attempt_commands.claims import ClaimsMixin
from cloud_agent.storage.repositories.attempt_commands.lifecycle import LifecycleMixin
from cloud_agent.storage.repositories.attempt_commands.reaper import ReaperMixin
from cloud_agent.storage.repositories.attempt_commands.reports import ReportsMixin

if TYPE_CHECKING:
    import asyncpg


class PostgresAttemptCommands(ClaimsMixin, LifecycleMixin, ReportsMixin, ReaperMixin):
    """Claim, heartbeat, transition and reap -- every fenced write a worker makes."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool
