"""Ending an owner's work when their Agent access ends, and keeping it ended.

The administrator's transaction (revoke or suspend) has already committed the
new state, the version, the audit row and an open *termination record* for
that version. From that moment every claim, goal round, waiting message, owner
request and residency establishment or heartbeat reads the denial. This stops
what was already under way, through the paths that already end work safely:

1. Queued turns are cancelled and running ones asked to stop, waiting messages
   and active goals pause, environment tests, prebuilds and actions are
   cancelled, and every residency is released, in one transaction
   (:meth:`~cloud_agent.ports.accounts.OwnerWorkTermination.end_owner_work`).
2. Each released residency's member process is closed through
   :func:`~cloud_agent.application.residency.close_released_residencies`; a
   host that does not answer closes it at its next residency heartbeat, which
   no longer extends it.
3. In each sandbox group whose members are all this owner's, the group's
   terminals are frozen (new ones refused), listed and killed, and the freeze
   is lifted again; the workspace itself is kept.

The record stays open until a pass finds step 3 acknowledged for every group
and no turn of the owner still running. The enforcement pass retries open
records every :data:`ENFORCEMENT_INTERVAL_S`, and while one is open the
account cannot be approved or made an administrator again, so a retry never
touches work started under a newer approval. Restoring access replays
nothing: cancelled turns stay cancelled, and paused goals and queues wait for
their owner.

**How it converges** (default settings). New requests, claims, rounds and
residencies are refused at commit. Open owner streams re-check every 30 s. A
running turn is asked to stop at once and stops at its host's next heartbeat,
or when its attempt lease lapses if the host is silent. Open records are
retried every :data:`ENFORCEMENT_INTERVAL_S`, at most :data:`ENFORCEMENT_BATCH`
per pass, so a host that keeps failing, or a long backlog, delays completion:
there is no fixed wall-clock deadline, only a check cadence and a record that
stays open, blocking re-approval, until every step is acknowledged. See
``docs/developer/local-accounts.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.application.residency import close_released_residencies
from cloud_agent.domain.accounts import permissions_for
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

logger = get_logger(__name__)

#: How often the enforcement pass retries open terminations.
ENFORCEMENT_INTERVAL_S = 30.0
#: Records one pass handles; the next pass continues.
ENFORCEMENT_BATCH = 50


def terminal_freeze_token(user_id: str, generation: int) -> str:
    """The suspension token a termination holds on a group's terminals."""
    return f"account-access:{user_id}:{generation}"


async def _end_group_terminals(storage: Storage, group: dict[str, Any], token: str) -> bool:
    """Freeze, kill and release one group's terminals; whether it was acknowledged."""
    from cloud_agent.sandbox_manager_client import SandboxManagerError
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_host

    host = await storage.host_pool.get_host(host=str(group["host"]))
    manager = sandbox_manager_for_host(host) if host is not None else None
    if manager is None:
        # The host left the pool: nothing of the group is reachable to stop.
        return True
    workspace = str(group["workspace_id"])
    try:
        await manager.suspend_terminals(workspace, lease_generation=0, job_id=token)
        listing = await manager.list_terminals(workspace)
        for terminal in listing.get("terminals") or []:
            try:
                await manager.delete_terminal(workspace, str(terminal["id"]))
            except SandboxManagerError as exc:
                if exc.status_code != 404:
                    raise
        await manager.resume_terminals(workspace, lease_generation=0, job_id=token)
    except SandboxManagerError as exc:
        # No such workspace any more: its processes went with it.
        return exc.status_code == 404
    except Exception:
        logger.warning(
            "agent_owner_terminal_cleanup_failed",
            exc_info=True,
            extra={
                "event": "agent_owner_terminal_cleanup_failed",
                "sandbox_id": group["sandbox_id"],
            },
        )
        return False
    return True


async def terminate_owner_access(
    storage: Storage, grants: Any, user_id: str, generation: int | None = None
) -> bool:
    """Stop one owner's work; whether every step is now acknowledged.

    The whole pass holds the account's termination lock, which approval and
    granting administration also need: no grant commits while a pass, in any
    process, can still stop work, and a pass that starts after one reads it
    and stops nothing. A pass that finds the lock taken leaves the record for
    the pass holding it.

    The record's revision is read under the lock before any work starts, and
    the pass closes the record only at that revision. A late terminal that
    reopens the record while the pass runs bumps the revision, so the record
    stays open for the next pass, which then kills that terminal too.

    Args:
        storage: The assembled storage.
        grants: The grant client, for revoking released residency grants.
        user_id: The owner whose access ended.
        generation: The termination record's account version, or ``None``
            for a sweep of residual work with no record behind it.
    """
    async with storage.local_accounts.termination_lock(user_id) as held:
        if not held:
            return False
        observed = None
        if generation is not None:
            observed = await storage.local_accounts.termination_revision(
                account_id=user_id, generation=generation
            )
        record = (generation, observed)
        account = await storage.local_accounts.get_account(user_id)
        if account is not None and permissions_for(account):
            # Access was granted again: nothing here may touch what that
            # approval allows. (The record barrier makes this unreachable.)
            await _record(storage, user_id, record, complete=True, outcome="superseded")
            return True
        return await _stop_work(storage, grants, user_id, record)


async def _stop_work(
    storage: Storage, grants: Any, user_id: str, record: tuple[int | None, int | None]
) -> bool:
    generation = record[0]
    result = await storage.owner_work.end_owner_work(user_id)
    await close_released_residencies(
        storage.host_pool, grants, result.get("released_residencies") or []
    )
    token = terminal_freeze_token(user_id, generation or 0)
    groups_done = True
    for group in result.get("groups") or []:
        groups_done = await _end_group_terminals(storage, group, token) and groups_done
    running = int(result.get("running") or 0)
    complete = groups_done and running == 0
    outcome = "complete" if complete else ("turns_stopping" if groups_done else "terminals_pending")
    closed = await _record(storage, user_id, record, complete=complete, outcome=outcome)
    if complete and not closed and generation is not None:
        outcome = "reopened"
    logger.info(
        "agent_owner_access_terminated",
        extra={
            "event": "agent_owner_access_terminated",
            "user_id": user_id,
            "generation": generation,
            "cancelled": int(result.get("cancelled") or 0),
            "stopping": int(result.get("stopping") or 0),
            "running": running,
            "released": len(result.get("released_residencies") or []),
            "outcome": outcome,
        },
    )
    return complete if generation is None else closed


async def _record(
    storage: Storage,
    user_id: str,
    record: tuple[int | None, int | None],
    *,
    complete: bool,
    outcome: str,
) -> bool:
    """Count the attempt; whether the record is now closed. Nothing without a record."""
    generation, revision = record
    if generation is None or revision is None:
        return False
    return await storage.local_accounts.record_termination_attempt(
        account_id=user_id,
        generation=generation,
        revision=revision,
        complete=complete,
        outcome=outcome,
    )


async def enforce_ended_access(storage: Storage, grants: Any) -> int:
    """One pass: retry every open termination, then sweep residual work.

    Returns:
        How many accounts were handled.
    """
    targets: list[tuple[str, int | None]] = list(
        await storage.local_accounts.open_terminations(limit=ENFORCEMENT_BATCH)
    )
    recorded = {user_id for user_id, _generation in targets}
    targets.extend(
        (user_id, None)
        for user_id in await storage.local_accounts.accounts_with_ended_access(
            limit=ENFORCEMENT_BATCH
        )
        if user_id not in recorded
    )
    handled = 0
    for user_id, generation in targets:
        try:
            await terminate_owner_access(storage, grants, user_id, generation)
        except Exception:
            logger.warning(
                "agent_owner_access_termination_failed",
                exc_info=True,
                extra={"event": "agent_owner_access_termination_failed", "user_id": user_id},
            )
            continue
        handled += 1
    return handled


__all__ = [
    "ENFORCEMENT_BATCH",
    "ENFORCEMENT_INTERVAL_S",
    "enforce_ended_access",
    "terminal_freeze_token",
    "terminate_owner_access",
]
