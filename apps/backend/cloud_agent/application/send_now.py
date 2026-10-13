"""Durable delivery of a waiting message into one exact running turn.

The database row is the work queue.  A request may wait eagerly for a host,
but no in-memory task owns the handoff: every running candidate is claimed by
revision under a reclaimable lease, and the attempt reaper retries abandoned
claims after a process restart.  The host deduplicates ``message_id``, so the
unavoidable crash window after host acceptance and before the acknowledgement
commit is safe to replay.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.domain.execution import RUNNING, TERMINAL_STATES
from cloud_agent.logging_utils import get_logger
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager_routing import sandbox_manager_for_attempt
from cloud_agent_shared.shared_protocol import (
    INPUT_PROCESS_GONE,
    INPUT_REFUSALS,
    INPUT_REFUSED,
    INPUT_TURN_ENDED,
    INPUT_UNREACHABLE,
    INPUT_UNSUPPORTED,
)

if TYPE_CHECKING:
    from cloud_agent.ports import ConversationQueries, HostPoolRepository, PendingMessageRepository

logger = get_logger(__name__)

#: A sandbox manager request is bounded at 30 seconds.  Keep the durable lease longer
#: than that so a second process never replays while the first request can
#: still be accepted by the host.
HAND_OVER_LEASE_S = 60.0
#: A queued turn gets this long to acquire a host before the owner's message
#: returns to its original queue position with an explicit status.
HAND_OVER_WAIT_S = 600.0
HAND_OVER_POLL_S = 0.5
HAND_OVER_IDLE_S = 5.0
#: Recovery runs beside the attempt-reaper loop. Sandbox Manager calls are concurrent
#: and each pass is bounded so shutdown and the next recovery pass stay prompt.
HAND_OVER_RECOVERY_CONCURRENCY = 8
HAND_OVER_RECOVERY_DEADLINE_S = 35.0

TARGET_ENDED = "target_ended"
CLAIM_TIMEOUT = "claim_timeout"


class SendNowStorage(Protocol):
    """Capabilities used by both the request and recovery paths."""

    conversation_queries: ConversationQueries
    host_pool: HostPoolRepository
    pending_messages: PendingMessageRepository


@dataclass(frozen=True)
class HandoffOutcome:
    """What one fenced delivery attempt changed."""

    claimed: bool = False
    accepted: bool = False
    requeued: bool = False
    reason: str | None = None


def failure_status(reason: str) -> str:
    """Human-readable durable status for a handoff that returned to the queue."""
    messages = {
        TARGET_ENDED: (
            "Send now was not delivered because the target turn ended before taking the message."
        ),
        CLAIM_TIMEOUT: (
            "Send now was not delivered because the target turn did not acquire "
            "a host within ten minutes."
        ),
        INPUT_TURN_ENDED: (
            "Send now was not delivered because the target turn ended before taking the message."
        ),
        INPUT_UNREACHABLE: (
            "Send now was not delivered because the target turn's host was unreachable."
        ),
        INPUT_PROCESS_GONE: (
            "Send now was not delivered because the target turn's agent process stopped."
        ),
        INPUT_UNSUPPORTED: (
            "Send now was not delivered because the target turn cannot accept mid-turn input."
        ),
        INPUT_REFUSED: "Send now was refused by the target turn.",
    }
    return messages.get(reason, messages[INPUT_REFUSED])


async def hand_over(
    store: SendNowStorage, *, thread_id: str, attempt_id: int, message: dict[str, Any]
) -> str | None:
    """Give the exact attempt's host the message, or return its refusal word."""
    sandbox_manager = await sandbox_manager_for_attempt(store.host_pool, attempt_id)
    if sandbox_manager is None:
        return INPUT_UNREACHABLE
    try:
        answer = await sandbox_manager.inject_shared_input(
            thread_id,
            owner=f"attempt:{attempt_id}",
            message_id=str(message["id"]),
            text=str(message["prompt"]),
        )
    except SandboxManagerError:
        logger.warning(
            "agent_send_now_outcome_uncertain",
            extra={"event": "agent_send_now_outcome_uncertain", "thread_id": thread_id},
            exc_info=True,
        )
        # The host may have accepted before its response was lost. Requeueing
        # would send the same message again as a later turn. Leave this claim
        # leased; recovery replays the ID and the host returns its receipt.
        raise
    if answer.get("accepted") is True:
        return None
    refused = answer.get("reason")
    if answer.get("accepted") is False and refused in INPUT_REFUSALS:
        return str(refused)
    # A malformed response says nothing about whether the side effect happened.
    raise SandboxManagerError(502, "The shared chat supervisor returned invalid input status.")


async def deliver_handoff(
    store: SendNowStorage, *, message_id: str, revision: int
) -> HandoffOutcome:
    """Claim and deliver one handoff under its revision lease.

    The host call deliberately happens outside the database transaction.  A
    later owner first increments ``revision``; therefore an expired owner can
    no longer acknowledge or requeue the row after the replacement claim.
    The target attempt's own lease may expire during sandbox manager I/O too. In that
    case neither acceptance nor refusal is authoritative: both acknowledgements
    leave this handoff leased, and recovery returns it as ``target_ended`` once
    the handoff lease is reclaimable. Work from an expired attempt is never a
    durable delivery receipt.
    """
    claimed = await store.pending_messages.claim_send_now_handoff(
        message_id=message_id,
        revision=revision,
        lease_seconds=HAND_OVER_LEASE_S,
    )
    if claimed is None:
        return HandoffOutcome()
    message = claimed["message"]
    attempt_id = int(claimed["attempt_id"])
    reason = await hand_over(
        store,
        thread_id=str(message["thread_id"]),
        attempt_id=attempt_id,
        message=message,
    )
    claimed_revision = int(message["revision"])
    if reason is None:
        marked = await store.pending_messages.mark_send_now_handoff_accepted(
            message_id=message_id,
            revision=claimed_revision,
            attempt_id=attempt_id,
        )
        return HandoffOutcome(claimed=True, accepted=marked)
    current = await store.pending_messages.return_to_queue(
        message_id=message_id,
        revision=claimed_revision,
        last_error=failure_status(reason),
        attempt_id=attempt_id,
    )
    requeued = current is not None and current.get("state") == "queued"
    # A still-sending row means this sandbox manager owner lost its fence (normally to
    # a replacement attempt). Its answer says nothing about the new owner, so
    # leave the lease for recovery rather than reporting a durable refusal.
    durable_reason = None if current is not None and current.get("state") == "sending" else reason
    return HandoffOutcome(
        claimed=True,
        requeued=requeued,
        reason=durable_reason,
    )


async def wait_for_handoff(
    store: SendNowStorage,
    *,
    job_id: str,
    message: dict[str, Any],
    signal: Any | None = None,
) -> HandoffOutcome:
    """Wait for the target to acquire a host, then enter the durable claim path."""
    deadline = time.monotonic() + HAND_OVER_WAIT_S
    subscription = signal.subscribe(job_id) if signal is not None else contextlib.nullcontext()
    with subscription as woken:
        while time.monotonic() < deadline:
            job = await store.conversation_queries.get_job(job_id)
            if job is None or job.get("state") in TERMINAL_STATES or job.get("cancel_requested"):
                current = await store.pending_messages.return_to_queue(
                    message_id=str(message["id"]),
                    revision=int(message["revision"]),
                    last_error=failure_status(TARGET_ENDED),
                )
                return HandoffOutcome(
                    requeued=current is not None and current.get("state") == "queued",
                    reason=TARGET_ENDED,
                )
            if job.get("state") == RUNNING and job.get("current_attempt_id") is not None:
                return await deliver_handoff(
                    store,
                    message_id=str(message["id"]),
                    revision=int(message["revision"]),
                )
            if signal is None or woken is None:
                await asyncio.sleep(HAND_OVER_POLL_S)
            else:
                await signal.wait(
                    woken,
                    HAND_OVER_IDLE_S if signal.listening else HAND_OVER_POLL_S,
                )
    current = await store.pending_messages.return_to_queue(
        message_id=str(message["id"]),
        revision=int(message["revision"]),
        last_error=failure_status(CLAIM_TIMEOUT),
    )
    return HandoffOutcome(
        requeued=current is not None and current.get("state") == "queued",
        reason=CLAIM_TIMEOUT,
    )


async def recover_handoffs(store: SendNowStorage, *, limit: int = 50) -> dict[str, int]:
    """Replay abandoned handoffs and return stale unclaimed work to its queue."""
    recovered = accepted = requeued = 0
    candidates = await store.pending_messages.recoverable_send_now_handoffs(
        lease_seconds=HAND_OVER_LEASE_S,
        limit=limit,
    )
    concurrency = asyncio.Semaphore(HAND_OVER_RECOVERY_CONCURRENCY)

    async def recover_one(candidate: dict[str, Any]) -> HandoffOutcome | None:
        try:
            async with concurrency:
                return await deliver_handoff(
                    store,
                    message_id=str(candidate["message_id"]),
                    revision=int(candidate["revision"]),
                )
        except Exception:
            logger.warning(
                "agent_send_now_recovery_failed",
                extra={
                    "event": "agent_send_now_recovery_failed",
                    "message_id": candidate["message_id"],
                },
                exc_info=True,
            )
            return None

    tasks = {asyncio.create_task(recover_one(candidate)): candidate for candidate in candidates}
    done: set[asyncio.Task[HandoffOutcome | None]] = set()
    pending: set[asyncio.Task[HandoffOutcome | None]] = set()
    if tasks:
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=HAND_OVER_RECOVERY_DEADLINE_S,
            )
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    for task in pending:
        # A task cancelled after its claim deliberately leaves the lease in
        # place.  It is reclaimable after HAND_OVER_LEASE_S, and a late owner
        # cannot commit because the replacement increments the revision.
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning(
            "agent_send_now_recovery_timed_out",
            extra={
                "event": "agent_send_now_recovery_timed_out",
                "completed": len(done),
                "outstanding": len(pending),
                "deadline_seconds": HAND_OVER_RECOVERY_DEADLINE_S,
            },
        )
    for task in done:
        outcome = task.result()
        if outcome is None:
            continue
        recovered += int(outcome.claimed)
        accepted += int(outcome.accepted)
        requeued += int(outcome.requeued)

    expired = await store.pending_messages.expired_send_now_handoffs(
        wait_seconds=HAND_OVER_WAIT_S,
        lease_seconds=HAND_OVER_LEASE_S,
        limit=limit,
    )
    for candidate in expired:
        try:
            current = await store.pending_messages.return_to_queue(
                message_id=str(candidate["message_id"]),
                revision=int(candidate["revision"]),
                last_error=failure_status(str(candidate["reason"])),
            )
        except Exception:
            logger.warning(
                "agent_send_now_requeue_failed",
                extra={
                    "event": "agent_send_now_requeue_failed",
                    "message_id": candidate["message_id"],
                },
                exc_info=True,
            )
            continue
        requeued += int(current is not None and current.get("state") == "queued")
    if recovered or requeued:
        logger.info(
            "agent_send_now_handoffs_recovered",
            extra={
                "event": "agent_send_now_handoffs_recovered",
                "claimed": recovered,
                "accepted": accepted,
                "requeued": requeued,
            },
        )
    return {"claimed": recovered, "accepted": accepted, "requeued": requeued}


__all__ = [
    "CLAIM_TIMEOUT",
    "HAND_OVER_IDLE_S",
    "HAND_OVER_LEASE_S",
    "HAND_OVER_POLL_S",
    "HAND_OVER_RECOVERY_CONCURRENCY",
    "HAND_OVER_RECOVERY_DEADLINE_S",
    "HAND_OVER_WAIT_S",
    "TARGET_ENDED",
    "HandoffOutcome",
    "deliver_handoff",
    "failure_status",
    "hand_over",
    "recover_handoffs",
    "wait_for_handoff",
]
