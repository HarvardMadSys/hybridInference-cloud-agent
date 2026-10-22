"""Bound best-effort revocation after the local credential fence is closed."""

from __future__ import annotations

import asyncio
from typing import Any

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)
REVOCATION_CONCURRENCY = 16
REVOCATION_DEADLINE_S = 5.0


async def revoke_detached_grants(grants: Any, grant_ids: list[str]) -> None:
    """Attempt a detached batch once, abandoning unfinished work to the grant TTL.

    The caller must fence/detach before handing over these identifiers. A
    deadline cancels and joins every worker; it does not persist retries or
    promise to contact the gateway for every identifier. This preserves the
    existing one-shot cleanup contract without letting an outage hold recovery.
    """
    pending = iter(grant_ids)
    completed = 0

    async def worker() -> None:
        nonlocal completed
        for grant_id in pending:
            try:
                if not await grants.revoke(grant_id):
                    logger.warning("agent_grant_not_revoked", extra={"grant_id": grant_id})
            except Exception:
                logger.warning("agent_grant_revoke_failed", exc_info=True)
            completed += 1

    try:
        async with asyncio.timeout(REVOCATION_DEADLINE_S):
            await asyncio.gather(
                *(worker() for _ in range(min(REVOCATION_CONCURRENCY, len(grant_ids))))
            )
    except TimeoutError:
        logger.warning(
            "agent_grant_revocation_pass_timed_out",
            extra={"completed": completed, "abandoned": len(grant_ids) - completed},
        )
