"""Sending the owner's pending messages, in order, as the turns in front of them end.

One entry point, :func:`settle_queue`, for every place a queue can move: the
worker's finish, a cancel, the owner's submit, edit, delete and continue, the
goal controller before it schedules an automatic round, and the periodic sweep
that covers a turn the reaper ended. They all run the same settlement, so the
queue has one set of rules however it was woken.

The settlement itself is one transaction in the store. What happens here is the
part that cannot be inside it: checking the first message against the turn it
would follow — the owner's Agent access, model, effort, repository access,
skills, uploads — can reach the gateway and the source-control provider, and a
thread's lock must not be held across either. The check is made outside, handed
back in, and applied only if it was made for exactly the message and the turn
the transaction then finds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException

from cloud_agent.application.follow_ups import PreparedFollowUp, prepare_follow_up
from cloud_agent.domain.pending_messages import PreparedHead, SettleOutcome
from cloud_agent.logging_utils import get_logger
from cloud_agent.owner_access import (
    AGENT_ACCESS_WITHDRAWN,
    AgentAccessUnavailable,
    owner_may_use_agent,
)
from cloud_agent.source_control_runtime import SourceControlUnavailable

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        ConversationQueries,
        PendingMessageRepository,
        SkillRepository,
        SourceControlRepository as SourceControlPort,
    )
    from cloud_agent.source_control_runtime import GitHubAppSource

logger = get_logger(__name__)

#: How many times one settlement re-checks a first message that changed under
#: it. Each round is a check and a transaction; a message edited faster than
#: that is left for the next settlement, which every edit triggers anyway.
_SETTLE_ROUNDS = 3

#: Stands in for the credentials of a configured GitHub App this process cannot
#: load just now — which is not the same as having no App (issue #47).
_APP_UNAVAILABLE = object()


class QueueStorage(Protocol):
    """What settling a queue reaches for.

    The queue itself, the turn a message would follow, and what checking the
    message against that turn reads: uploads, skills and repository access.
    """

    pending_messages: PendingMessageRepository
    conversation_queries: ConversationQueries
    attachments: AttachmentRepository
    skills: SkillRepository
    source_control: SourceControlPort


def prepared_head(
    message: dict[str, Any], *, parent_job_id: str, resolved: PreparedFollowUp
) -> PreparedHead:
    """A message's check result, in the form the settling transaction accepts."""
    return PreparedHead(
        message_id=str(message["id"]),
        revision=int(message["revision"]),
        parent_job_id=parent_job_id,
        model=resolved.model,
        reasoning_effort=resolved.reasoning_effort,
        reset_reasoning_effort=resolved.reset_reasoning_effort,
        skills=resolved.skills,
        attachments=resolved.attachments,
    )


def _refusal(exc: HTTPException) -> str:
    """The sentence a refused check gave its owner, for the held message."""
    detail = exc.detail
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return str(detail)


async def prepare_message(
    store: QueueStorage,
    *,
    message: dict[str, Any],
    parent_job_id: str,
    gateway: Any,
    app_credentials: Any | None,
) -> PreparedHead | None:
    """Check one waiting message against the turn it would follow.

    The same checks a person's submission passes, against the turn that is the
    parent *now* — which is not the turn that was running when the message was
    written, and may run a different model. A refusal comes back as a
    ``PreparedHead`` carrying the reason, and the settlement holds the message
    with it. ``None`` means the check could not be made — the gateway did not
    answer, say — and nothing about the message should change: the next
    settlement asks again.

    **The owner's Agent access is asked first.** A message waiting in the queue
    is sent with no request of its owner's behind it, so nothing else here
    would notice that their access was withdrawn after they wrote it. A denial
    holds the message with the reason; a Gateway that cannot answer leaves it
    waiting, exactly as for any other check that could not be made.
    """
    parent = await store.conversation_queries.get_job(parent_job_id)
    if parent is None:
        return None
    try:
        permitted = await owner_may_use_agent(gateway, user_id=str(message["user_id"]))
    except AgentAccessUnavailable:
        logger.warning(
            "agent_pending_message_check_unavailable",
            extra={
                "event": "agent_pending_message_check_unavailable",
                "thread_id": message.get("thread_id"),
                "message_id": message.get("id"),
                "reason": "agent_access_unavailable",
            },
            exc_info=True,
        )
        return None
    if not permitted:
        return PreparedHead(
            message_id=str(message["id"]),
            revision=int(message["revision"]),
            parent_job_id=parent_job_id,
            error=AGENT_ACCESS_WITHDRAWN,
        )
    try:
        resolved = await prepare_follow_up(
            parent,
            user={"user_id": str(message["user_id"])},
            skill_ids=message.get("skill_ids"),
            attachment_ids=message.get("attachment_ids"),
            runtime=None,
            model=message.get("model"),
            reasoning_effort=message.get("reasoning_effort"),
            store=store,
            app_credentials=app_credentials,
            gateway=gateway,
        )
    except HTTPException as exc:
        if exc.status_code >= 500:
            logger.warning(
                "agent_pending_message_check_unavailable",
                extra={
                    "event": "agent_pending_message_check_unavailable",
                    "thread_id": message.get("thread_id"),
                    "message_id": message.get("id"),
                    "status_code": exc.status_code,
                },
            )
            return None
        return PreparedHead(
            message_id=str(message["id"]),
            revision=int(message["revision"]),
            parent_job_id=parent_job_id,
            error=_refusal(exc),
        )
    except Exception:
        # An outage is not a verdict on the message. Holding it would make the
        # owner fix a message nothing was wrong with.
        logger.warning(
            "agent_pending_message_check_failed",
            extra={
                "event": "agent_pending_message_check_failed",
                "thread_id": message.get("thread_id"),
                "message_id": message.get("id"),
            },
            exc_info=True,
        )
        return None
    return prepared_head(message, parent_job_id=parent_job_id, resolved=resolved)


async def settle_queue(
    store: QueueStorage,
    *,
    thread_id: str,
    gateway: Any,
    app_credentials: Any | None,
    prepared: PreparedHead | None = None,
) -> SettleOutcome:
    """Act on the newest turn's ending, and send the first waiting message if it may go.

    Safe to call from anywhere, at any time, any number of times: under the
    thread's lock a second call finds the turn the first one created and does
    nothing. ``prepared`` is a check the caller already made — the submit route
    checks the message it is adding — so the common case asks the gateway once.
    """
    for _ in range(_SETTLE_ROUNDS):
        outcome = await store.pending_messages.settle_queue(thread_id=thread_id, prepared=prepared)
        if outcome.head is None or outcome.parent_job_id is None:
            if outcome.job is not None:
                logger.info(
                    "agent_pending_message_sent",
                    extra={
                        "event": "agent_pending_message_sent",
                        "thread_id": thread_id,
                        "message_id": outcome.message_id,
                        "job_id": outcome.job.get("id"),
                    },
                )
            return outcome
        prepared = await prepare_message(
            store,
            message=outcome.head,
            parent_job_id=outcome.parent_job_id,
            gateway=gateway,
            app_credentials=app_credentials,
        )
        if prepared is None:
            return SettleOutcome()
    return SettleOutcome()


def _app_credentials(github_app: GitHubAppSource | None) -> Any:
    """The App's credentials now, ``None`` without an App, or the unavailable marker."""
    try:
        return github_app.get() if github_app is not None else None
    except SourceControlUnavailable:
        return _APP_UNAVAILABLE


async def _settle_with(
    store: QueueStorage, *, thread_id: str, gateway: Any, app_credentials: Any
) -> SettleOutcome:
    if app_credentials is _APP_UNAVAILABLE:
        # The ending is still recorded and a failed or cancelled turn still
        # pauses the queue; only sending waits. Without the App, the first
        # message's repository check would see just the allowlist and hold a
        # message its owner may send. Unprepared, the store sends nothing.
        return await store.pending_messages.settle_queue(thread_id=thread_id, prepared=None)
    return await settle_queue(
        store, thread_id=thread_id, gateway=gateway, app_credentials=app_credentials
    )


async def settle_queue_when_possible(
    store: QueueStorage,
    *,
    thread_id: str,
    gateway: Any,
    github_app: GitHubAppSource | None,
) -> SettleOutcome:
    """Settle one queue for a caller that holds no App credentials of its own.

    The worker's finish and a cancel: they must answer whether or not the
    GitHub App can be loaded right now, so the credentials are asked for here.
    While the App cannot be loaded the ending is acted on but nothing is sent;
    the periodic sweep sends it once the App is back.
    """
    return await _settle_with(
        store,
        thread_id=thread_id,
        gateway=gateway,
        app_credentials=_app_credentials(github_app),
    )


@dataclass
class QueueSweep:
    """Where the next periodic sweep starts: past the last thread one reached.

    A full page moves it on and a short one, which reached the end, starts the
    next sweep over. However many threads before it cannot move, a waiting
    queue is reached within one pass over them all.
    """

    after: str | None = None


async def settle_waiting_queues(
    store: QueueStorage,
    *,
    gateway: Any,
    github_app: GitHubAppSource | None,
    limit: int = 50,
    sweep: QueueSweep | None = None,
) -> int:
    """Settle every queue a settlement could move or pause; the periodic sweep.

    Not the mechanism — the finish route, cancel and the owner's own actions
    settle as they happen. This is for the endings that reach no route: the
    reaper failing an attempt whose lease ran out, a control plane restarted
    between a turn's end and its settlement, and a message that waited while
    the GitHub App could not be loaded. The App is asked for once per pass.
    Each thread is settled on its own, so one bad thread cannot stop the rest.
    A ``sweep`` carried from one pass to the next pages through the threads,
    so a page of queues that cannot move does not hide the ones after it.
    Returns how many were settled.
    """
    app_credentials = _app_credentials(github_app)
    settled = 0
    page = await store.pending_messages.threads_to_settle(
        limit=limit, after=sweep.after if sweep is not None else None
    )
    if sweep is not None:
        sweep.after = page[-1] if len(page) >= limit else None
    for thread_id in page:
        try:
            await _settle_with(
                store, thread_id=thread_id, gateway=gateway, app_credentials=app_credentials
            )
        except Exception:
            logger.warning(
                "agent_pending_queue_settle_failed",
                extra={"event": "agent_pending_queue_settle_failed", "thread_id": thread_id},
                exc_info=True,
            )
            continue
        settled += 1
    return settled


__all__ = [
    "QueueStorage",
    "QueueSweep",
    "prepare_message",
    "prepared_head",
    "settle_queue",
    "settle_queue_when_possible",
    "settle_waiting_queues",
]
