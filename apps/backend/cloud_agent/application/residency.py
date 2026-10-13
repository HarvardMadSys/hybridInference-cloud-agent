"""Minting the worker-fenced grant one resident process spends.

A residency is a member of a shared sandbox group, and each one gets its own
scoped credential — siblings cannot read or reuse each other's. This mints it
against the thread-attributed store row and links it there, so the lease and
the grant are renewed by the same heartbeat and revoked together.

It lived in `api/hosts.py`, a router module, although three surfaces reach it
and none of them is a route. The lease length travels with it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting, setting_env

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cloud_agent.ports import GrantRepository, HostPoolRepository

#: The moved helper keeps logging under the module it came from: the
#: deployment's log selection is on ``cloud_agent.api.hosts``.
logger = get_logger("cloud_agent.api.hosts")


#: How long a residency's lease lives between beats (design §7) — and, since
#: the lease and the thread-attributed grant are renewed by the same beat and
#: revoked together (retained-session design §§3 and 6), the cap on how long an idle resident
#: process keeps a live credential after its host goes quiet.
RESIDENCY_TTL_S = 1800.0

#: How long a chat deletion waits on the gateway and hosts after its commit,
#: and how many released residencies it closes at once. A sandbox manager
#: call alone may take a minute to time out; the host's residency heartbeat
#: finishes anything this leaves undone.
RELEASED_RESIDENCY_CLOSE_TIMEOUT_S = 5.0
RELEASED_RESIDENCY_CLOSE_CONCURRENCY = 16


def residency_ttl_s() -> float:
    """Read the current lease duration for newly established or renewed residents."""
    fallback = float(setting_env("AGENT_RESIDENCY_TTL_S") or RESIDENCY_TTL_S)
    return number_setting("residency_ttl_s", fallback)


async def _mint_residency_grant(
    job_store: GrantRepository, grants: Any, *, row: dict[str, Any]
) -> str | None:
    """Mint the worker-fenced grant for one supervisor-owned member process."""
    if grants is None:
        return None
    # The gateway's mint is idempotent on (job, attempt), even after revoke.
    # A Fork retry or another establishment within the same worker attempt
    # rotates the residency and revokes its predecessor. Include that durable
    # generation so its replacement cannot receive the revoked grant again,
    # while retries of this particular mint still recover the same capability.
    mint_attempt_id = f"residency:{row['establishing_attempt_id']}:{int(row['generation'])}"
    try:
        grant = await grants.mint(
            user_id=row["user_id"],
            job_id=f"thread:{row['thread_id']}",
            attempt_id=mint_attempt_id,
            models=[row["model"]],
            ttl_seconds=int(residency_ttl_s()),
        )
    except Exception:
        logger.warning(
            "agent_residency_grant_unavailable",
            exc_info=True,
            extra={
                "event": "agent_residency_grant_unavailable",
                "thread_id": row["thread_id"],
            },
        )
        return None
    grant_id = str(grant.get("grant_id") or "")
    token = str(grant.get("token") or "")
    if not grant_id:
        return None
    if not token:
        # The gateway named a grant but handed no credential for it. Nothing
        # can spend it, so returning here would leave a live capability with no
        # owner until its own TTL ran out. Revoke it, best-effort: this path
        # makes no other network call, and a gateway that refuses the revoke
        # must not turn a clean "no credential" into a 500.
        with contextlib.suppress(Exception):
            await grants.revoke(grant_id)
        return None
    binding_id = getattr(grants, "binding_id", None)
    binding = {"binding_id": binding_id} if isinstance(binding_id, str) and binding_id else {}
    linked = await job_store.link_residency_grant(
        thread_id=row["thread_id"], generation=row["generation"], grant_id=grant_id, **binding
    )
    if not linked:
        # Generation takeover or clock expiry won the race with minting. The
        # token must never escape, even when best-effort cleanup is unavailable.
        with contextlib.suppress(Exception):
            await grants.revoke(grant_id)
        return None
    return token


async def close_released_residencies(
    host_pool: HostPoolRepository,
    grants: Any,
    released: Iterable[dict[str, Any]],
    *,
    timeout_s: float | None = None,
) -> None:
    """Revoke each released grant and close its member process, best-effort.

    The release has already committed, so its generation authorizes nothing
    more. Revoking ends the credential now rather than at its TTL, and the
    sandbox manager call closes the process now rather than at the host's
    next residency heartbeat, which retries the same close by itself.

    The owner is waiting on this, so it is bounded: rows proceed concurrently,
    a few at a time, and the whole step stops after ``timeout_s``. A host that
    never answers delays only its own rows. What is cut off is left to that
    heartbeat; a grant not revoked can no longer be renewed and ends at its TTL.
    """
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_host

    rows = list(released)
    if not rows:
        return
    # Separate limits: a revoke never queues behind a host that holds its
    # close slot, so every grant is revoked even while hosts are silent.
    revoking = asyncio.Semaphore(RELEASED_RESIDENCY_CLOSE_CONCURRENCY)
    closing = asyncio.Semaphore(RELEASED_RESIDENCY_CLOSE_CONCURRENCY)

    async def close_one(row: dict[str, Any]) -> None:
        grant_id = str(row.get("grant_id") or "")
        if grant_id and grants is not None:
            async with revoking:
                with contextlib.suppress(Exception):
                    await grants.revoke(grant_id)
        async with closing:
            try:
                host = await host_pool.get_host(host=str(row["host"]))
                manager = sandbox_manager_for_host(host) if host is not None else None
                if manager is not None:
                    await manager.shared_residency_lost(
                        str(row["thread_id"]), generation=int(row["generation"])
                    )
            except Exception:
                logger.warning(
                    "agent_released_residency_close_failed",
                    exc_info=True,
                    extra={
                        "event": "agent_released_residency_close_failed",
                        "thread_id": row.get("thread_id"),
                        "generation": row.get("generation"),
                    },
                )

    try:
        async with asyncio.timeout(
            RELEASED_RESIDENCY_CLOSE_TIMEOUT_S if timeout_s is None else timeout_s
        ):
            await asyncio.gather(*(close_one(row) for row in rows))
    except TimeoutError:
        logger.warning(
            "agent_released_residency_close_timed_out",
            extra={
                "event": "agent_released_residency_close_timed_out",
                "threads": [row.get("thread_id") for row in rows],
            },
        )
