"""Attempt lease recovery and the maintenance passes beside it."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.application.send_now import recover_handoffs
from cloud_agent.host_pool import HEARTBEAT_INTERVAL_S, MISSED_HEARTBEATS_BEFORE_OFFLINE
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import integer_setting
from cloud_agent.workers.terminal_replay import (
    ReplayCursor,
    TerminalReplayStore,
    _reconcile_settled_terminals,
)

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        AttemptCommands,
        ConversationCommands,
        ConversationQueries,
        HostPoolRepository,
        PendingMessageRepository,
        ResidencyRepository,
        SandboxRepository,
        SessionForkRepository,
        SkillRepository,
        SnapshotReapingRepository,
        SourceControlRepository,
    )

#: The moved loop keeps logging under the module it came from: the deployment's
#: log selection is on ``cloud_agent.app``, and a relocation must not move these
#: records to a logger nobody watches.
logger = get_logger("cloud_agent.app")


class _Reaping(Protocol):
    """Capabilities used by one attempt-recovery pass."""

    attachments: AttachmentRepository
    attempt_commands: AttemptCommands
    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    source_control: SourceControlRepository
    host_pool: HostPoolRepository
    pending_messages: PendingMessageRepository
    residency: ResidencyRepository
    sandbox: SandboxRepository
    session_fork: SessionForkRepository
    skills: SkillRepository

    @property
    def snapshot(self) -> SnapshotReapingRepository:
        """Snapshot persistence needed by this flow."""
        ...


#: How often the reaper looks for expired leases and stalled publishes.
REAP_INTERVAL_S = 30.0


#: Member cleanups one reaper pass runs at once.
RESIDENCY_CLEANUP_CONCURRENCY = 16


#: How long the reaper waits for a host's sandbox manager to stop one member
#: process it has fenced out. The store has already closed that residency
#: generation, and the host's next heartbeat is told so and stops the process
#: itself, so the call only makes that prompt. Most attempts the reaper ends
#: expired because their host went away, and a host that no longer answers
#: must not hold the pass -- every other host's reaping -- for a whole request
#: timeout per member.
RESIDENCY_CLEANUP_TIMEOUT_S = 5.0


async def _reap_offline_hosts(store: HostPoolRepository) -> None:
    """Move hosts that stopped heartbeating to ``offline``; finish pending removals.

    Runs on the same loop as the attempt reaper rather than on its own, because
    the two are one recovery story told in order: a host disappears, it stops
    being claimable, and then the work it was holding comes back. Splitting
    them into two schedules would make the interval between them a race that
    only shows up under load.
    """
    from cloud_agent.sandbox_manager_routing import (
        retire_sandbox_manager_hosts,
        sweep_sandbox_manager_clients,
    )

    sweep_sandbox_manager_clients()
    offline = await store.reap_offline_hosts(
        interval_seconds=HEARTBEAT_INTERVAL_S,
        missed_intervals=MISSED_HEARTBEATS_BEFORE_OFFLINE,
    )
    if offline:
        logger.warning(
            "agent_hosts_offline",
            extra={"event": "agent_hosts_offline", "hosts": offline},
        )
    # A removal the operator asked for while the host was busy completes here,
    # once the pass that ended its last attempt has given the slot back.
    removed = await store.finish_host_removals()
    retire_sandbox_manager_hosts(removed)
    if removed:
        logger.info(
            "agent_hosts_removed",
            extra={"event": "agent_hosts_removed", "hosts": removed},
        )


async def _reap_lapsed_residencies(store: ResidencyRepository) -> None:
    """Lapse residencies whose lease ran out, so a silent host's members stop."""
    lapsed = await store.reap_expired_residencies()
    if not lapsed:
        return
    logger.info(
        "agent_residencies_lapsed",
        extra={
            "event": "agent_residencies_lapsed",
            "threads": [row["thread_id"] for row in lapsed],
        },
    )


async def _cleanup_reaped_shared_residencies(
    store: HostPoolRepository,
    actions: list[dict[str, Any]],
) -> None:
    """Fence exact shared member processes orphaned by attempt expiry.

    The members a pass reaps mostly share the host that went away, so each
    member's cleanup runs beside the others, at most
    :data:`RESIDENCY_CLEANUP_CONCURRENCY` at once, and waits for its host for at
    most :data:`RESIDENCY_CLEANUP_TIMEOUT_S`. Taken one after another, with
    only the client's own timeout, an unanswering host would hold the rest of
    the pass for that whole timeout per member.
    """
    cleanups: list[tuple[Any, Any]] = [
        (action, residency_cleanup)
        for action in actions
        if (residency_cleanup := action.get("cleanup_residency"))
    ]
    if not cleanups:
        return

    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

    limit = asyncio.Semaphore(RESIDENCY_CLEANUP_CONCURRENCY)

    async def cleanup_one(action: dict[str, Any], cleanup: Any) -> None:
        async with limit:
            try:
                async with asyncio.timeout(RESIDENCY_CLEANUP_TIMEOUT_S):
                    sandbox_manager = await sandbox_manager_for_job(store, str(action["job_id"]))
                    if sandbox_manager is not None:
                        await sandbox_manager.shared_residency_lost(
                            str(cleanup["thread_id"]),
                            generation=int(cleanup["generation"]),
                        )
            except Exception:
                # The durable residency generation is already closed. A later
                # host heartbeat echoes the dead generation and retries local
                # process cleanup, so sandbox manager reachability cannot block reaping.
                logger.warning(
                    "agent_reaped_shared_residency_cleanup_failed",
                    extra={
                        "event": "agent_reaped_shared_residency_cleanup_failed",
                        "thread_id": cleanup["thread_id"],
                        "generation": cleanup["generation"],
                    },
                    exc_info=True,
                )

    await asyncio.gather(*(cleanup_one(action, cleanup) for action, cleanup in cleanups))


async def _cleanup_failed_session_fork(
    store: HostPoolRepository, operation: dict[str, Any]
) -> None:
    """Fence the member processes one failed Fork left behind.

    The store failed the operation and closed these residencies in its own
    commit, and it never returns that operation again, so this is the only
    cleanup it gets. A sandbox manager that cannot be resolved or reached is
    left to a later host heartbeat, which is told the residency was lost and
    stops its process. One that does not answer is given
    :data:`RESIDENCY_CLEANUP_TIMEOUT_S` per member, like a reaped attempt's.
    """
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

    sandbox_manager = None
    try:
        sandbox_manager = await sandbox_manager_for_job(store, operation["source_job_id"])
    except Exception:
        logger.warning(
            "agent_session_fork_cleanup_sandbox_manager_unavailable",
            extra={
                "event": "agent_session_fork_cleanup_sandbox_manager_unavailable",
                "operation_id": operation.get("id"),
            },
            exc_info=True,
        )
    for residency in operation.get("cleanup_residencies") or []:
        if sandbox_manager is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(RESIDENCY_CLEANUP_TIMEOUT_S):
                    await sandbox_manager.shared_residency_lost(
                        str(residency["thread_id"]),
                        generation=int(residency["generation"]),
                    )


async def _run_attempt_reaper(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
    store: _Reaping,
    *,
    interval_seconds: float = REAP_INTERVAL_S,
    max_attempts: int | None = None,
) -> None:
    """Recover attempts and related resources whose leases or work stalled.

    Owners' waiting queues are not settled here. A queue's check can wait on
    GitHub, so the sweep is a task of its own with its own deadline
    (``run_pending_message_maintenance``), and a slow one cannot delay the
    next lease pass.

    Args:
        store: The agent store.
        interval_seconds: Delay between passes.
        max_attempts: How many attempts a job gets before it fails for good.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        # Hosts first. A machine that stopped heartbeating should be out of the
        # claim gate before the pass that requeues its work, otherwise the
        # requeued job can be handed straight back to the host that just lost
        # it — the retry budget burns down while nothing moves.
        #
        # In its own try, though. Sharing one with the attempt reaper below
        # would let a host-reaping failure skip lease reaping and stalled
        # publishes for that pass, which is a strictly worse outage than the
        # one it was reporting: jobs stop being recovered at all.
        try:
            await _reap_offline_hosts(store.host_pool)
        except Exception:
            logger.warning("Host reaper pass failed", exc_info=True)
        # Residencies after hosts, before attempts: a lapsed hot sandbox must
        # lose its generation (heartbeats, affinity) before the
        # attempt pass requeues anything that might route back to it. Its own
        # try, for the same reason as the host pass above.
        try:
            await _reap_lapsed_residencies(store.residency)
        except Exception:
            logger.warning("Residency reaper pass failed", exc_info=True)
        try:
            actions = await store.attempt_commands.reap_expired(
                max_attempts=(
                    max_attempts if max_attempts is not None else integer_setting("max_attempts")
                )
            )
            await _cleanup_reaped_shared_residencies(store.host_pool, actions)
            if actions:
                logger.info(
                    "agent_attempts_reaped",
                    extra={"event": "agent_attempts_reaped", "count": len(actions)},
                )
        except Exception:
            logger.warning("Agent attempt reaper pass failed", exc_info=True)
        # Stalled captures, publishes and Forks each in a try of their own: they
        # recover different workspaces, so one that fails on every pass must
        # not stop the others -- or the attempt pass above -- from running.
        try:
            stalled_captures = await store.snapshot.reap_stalled_sandbox_snapshot_captures()
            if stalled_captures:
                logger.warning(
                    "agent_snapshot_captures_stalled",
                    extra={
                        "event": "agent_snapshot_captures_stalled",
                        "snapshot_ids": stalled_captures,
                    },
                )
        except Exception:
            logger.warning("Stalled snapshot capture reaper pass failed", exc_info=True)
        try:
            stalled_snapshot_publishes = await store.snapshot.reap_stalled_snapshot_publishes()
            if stalled_snapshot_publishes:
                logger.warning(
                    "agent_snapshot_publishes_stalled",
                    extra={
                        "event": "agent_snapshot_publishes_stalled",
                        "snapshot_ids": stalled_snapshot_publishes,
                    },
                )
        except Exception:
            logger.warning("Stalled snapshot publish reaper pass failed", exc_info=True)
        try:
            stalled_session_forks = await store.session_fork.reap_stalled_session_forks()
            if stalled_session_forks:
                recovered: list[str] = []
                cleaned: list[str] = []
                for operation in stalled_session_forks:
                    operation_id = str(operation["id"])
                    # Each operation in a try of its own. The failed ones are
                    # never returned again, so an error on one must not leave
                    # the processes of the ones after it running.
                    try:
                        if operation.get("recovery_action") == "finalize":
                            if await store.session_fork.finalize_session_fork(
                                operation_id=operation_id
                            ):
                                recovered.append(operation_id)
                            continue
                        await _cleanup_failed_session_fork(store.host_pool, operation)
                    except Exception:
                        logger.warning(
                            "agent_session_fork_reconcile_failed",
                            extra={
                                "event": "agent_session_fork_reconcile_failed",
                                "operation_id": operation_id,
                            },
                            exc_info=True,
                        )
                        continue
                    cleaned.append(operation_id)
                logger.warning(
                    "agent_session_forks_reconciled",
                    extra={
                        "event": "agent_session_forks_reconciled",
                        "recovered_operation_ids": recovered,
                        "cleaned_operation_ids": cleaned,
                    },
                )
        except Exception:
            logger.warning("Stalled Fork reaper pass failed", exc_info=True)
        # Abandoned staged uploads count against staging quota until collected.
        # Pending-message and job-claimed rows are kept by the repository
        # predicate. Own try: a failure here must not skip later reaping.
        try:
            await store.attachments.purge_stale_staged(
                older_than_hours=integer_setting(
                    "attachments.staged_retention_hours",
                )
            )
        except Exception:
            logger.warning("Staged upload purge failed", exc_info=True)


async def _recover_send_now_handoffs(store: _Reaping, *, interval_seconds: float) -> None:
    """Recover durable handoffs without delaying attempt safety passes."""
    while True:
        try:
            await recover_handoffs(store)
        except Exception:
            logger.warning("Send-now handoff recovery pass failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def _replay_settled_terminals(store: TerminalReplayStore, *, interval_seconds: float) -> None:
    """Replay settled-terminal markers without delaying attempt safety passes.

    A marker stays set until its owning sandbox manager confirms. The cursor
    advances across dispatched candidates even when the bounded pass times
    out, then wraps to retry failures. This task remains independent of lease
    reaping, and owns all of its replay calls.
    """
    cursor = ReplayCursor()
    while True:
        try:
            await _reconcile_settled_terminals(store, cursor=cursor)
        except Exception:
            logger.warning("Settled terminal reconciliation pass failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def reap_expired_attempts(
    store: _Reaping,
    *,
    interval_seconds: float = REAP_INTERVAL_S,
    max_attempts: int | None = None,
) -> None:
    """Run attempt maintenance beside independent handoff and terminal loops.

    Send-now handoff recovery and settled-terminal replay each run in an owned
    task, once at startup and then on the same cadence, so a slow host sandbox
    manager cannot hold up attempt reaping. Cancelling this worker cancels and
    joins both owned tasks, including work they own.
    """
    # Created first, so a restarted control plane still replays settled
    # terminals before anything else, as it did when that replay ran ahead of
    # the first pass.
    terminals = asyncio.create_task(
        _replay_settled_terminals(store, interval_seconds=interval_seconds),
        name="agent-settled-terminal-replay",
    )
    recovery = asyncio.create_task(
        _recover_send_now_handoffs(store, interval_seconds=interval_seconds),
        name="agent-send-now-recovery",
    )
    try:
        await _run_attempt_reaper(
            store, interval_seconds=interval_seconds, max_attempts=max_attempts
        )
    finally:
        tasks = [recovery, terminals]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
