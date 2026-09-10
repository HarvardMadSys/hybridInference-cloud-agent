"""Attempt accounting helpers that two commands share."""

from __future__ import annotations

from typing import Any

import cloud_agent.domain.execution as _execution


async def attempts_spent(conn: Any, job_id: str) -> int:
    """Count attempts that actually got their turn.

    The retry budget used to be read off ``attempt_no``, which numbers
    every claim including ones the platform abandoned before the agent
    started. Counting instead means an aborted claim is free: numbers still
    advance (they are referenced by events, and gaps are informative), but
    only attempts that ran spend the budget.
    """
    return await conn.fetchval(
        "SELECT count(*) FROM agent_attempts WHERE job_id = $1 AND status <> $2",
        job_id,
        _execution.ATTEMPT_ABORTED,
    )
