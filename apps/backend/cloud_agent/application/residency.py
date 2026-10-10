"""Minting the worker-fenced grant one resident process spends.

A residency is a member of a shared sandbox group, and each one gets its own
scoped credential — siblings cannot read or reuse each other's. This mints it
against the thread-attributed store row and links it there, so the lease and
the grant are renewed by the same heartbeat and revoked together.

It lived in `api/hosts.py`, a router module, although three surfaces reach it
and none of them is a route. The lease length travels with it.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting, setting_env

if TYPE_CHECKING:
    from cloud_agent.ports import GrantRepository

#: The moved helper keeps logging under the module it came from: the
#: deployment's log selection is on ``cloud_agent.api.hosts``.
logger = get_logger("cloud_agent.api.hosts")


#: How long a residency's lease lives between beats (design §7) — and, since
#: the lease and the thread-attributed grant are renewed by the same beat and
#: revoked together (retained-session design §§3 and 6), the cap on how long an idle resident
#: process keeps a live credential after its host goes quiet.
RESIDENCY_TTL_S = 1800.0


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
    try:
        grant = await grants.mint(
            user_id=row["user_id"],
            job_id=f"thread:{row['thread_id']}",
            attempt_id=str(row["establishing_attempt_id"]),
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
        await grants.revoke(grant_id)
        return None
    return token
