"""Appending a person's turn to a conversation the way production does.

Production never appends a follow-up in a transaction of its own. Every
person's turn after the first is written by ``insert_follow_up`` inside the
transaction that decided to write it: the queue's settlement promoting a
waiting message, or an idle-only message. A test whose subject is something
else and that needs a later turn as a precondition calls that same function,
committed on its own, rather than a store method only tests reached.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.storage.repositories.conversation_commands import insert_follow_up


async def append_follow_up(pool: Any, **fields: Any) -> dict[str, Any] | None:
    """Append one turn with ``insert_follow_up`` and commit it.

    ``fields`` are ``insert_follow_up``'s own keyword arguments. The answer is
    its answer: the new turn, ``None`` for a conversation the owner does not
    have, or ``{"error": ...}``.
    """
    async with pool.acquire() as conn, conn.transaction():
        return await insert_follow_up(conn, **fields)
