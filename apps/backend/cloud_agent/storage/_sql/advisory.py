"""Releasing a session advisory lock without ever pooling a connection that holds one.

The control plane's pool returns connections without asyncpg's reset query
(``bootstrap/lifespan.py``), so a connection given back while it still holds a
session lock would hand that lock to whichever caller borrows it next. Every
session lock is therefore released here: unlocked, or the connection reset,
or closed if even the reset fails, and finished even if the caller is
cancelled meanwhile.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

#: The key expression every session lock here uses.
LOCK_KEY_SQL = "hashtextextended($1::text, 0)"


async def unlock_or_reset(conn: asyncpg.Connection, lock_key: str) -> None:
    """Release ``lock_key``; reset, or close, a connection whose release is uncertain."""
    unlocked = False
    with suppress(BaseException):
        unlocked = bool(await conn.fetchval(f"SELECT pg_advisory_unlock({LOCK_KEY_SQL})", lock_key))
    if unlocked:
        return
    try:
        await conn.reset()
    except BaseException:
        await conn.close()


async def finish_lock_cleanup(conn: asyncpg.Connection, lock_key: str) -> None:
    """Run :func:`unlock_or_reset` to completion, then re-raise a cancellation it absorbed."""
    cleanup = asyncio.create_task(unlock_or_reset(conn, lock_key))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    await cleanup
    if cancelled:
        raise asyncio.CancelledError


__all__ = ["LOCK_KEY_SQL", "finish_lock_cleanup", "unlock_or_reset"]
