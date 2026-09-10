"""Shared construction for the Postgres repositories.

Every repository holds one pool and nothing else. A request-scoped connection
is never stored on the instance: two concurrent calls on one repository must
not be able to observe each other's transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class PostgresRepository:
    """Base for the storage implementations: one pool, no request state."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Keep the shared pool; opening and closing it belongs to bootstrap."""
        self._pool = pool
