"""The two write paths that add a turn to an existing chat.

A message continues a conversation; a fork splits one off at an anchor. Both
are substantial orchestrations — admission checks, entitlement re-checks,
idempotency, sandbox residency, grant minting.

A message is sent through one route whatever the conversation is doing. Idle,
it becomes the next turn in the same request. Busy, it waits in the thread's
queue, where the owner can still edit or delete it, and the queue sends it when
the turn in front of it has ended — see `application.pending_messages`. The
routes that edit, delete and continue that queue live here too.

Fork's own orchestration is not here: `application.session_fork` drives a
PREPARING operation to MATERIALIZED, so its failure branches are reachable
without an HTTP request. What stays is the anchor validation, the store's
`prepare_session_fork`, the finalize and the response — and
`SESSION_FORK_COORDINATOR_LEASE_S`, which bounds that flow, moved with it.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import ValidationError

from cloud_agent.api._job_views import (
    _job_response,
    _pending_response,
    mid_turn_input_supported,
)
from cloud_agent.api._owner_auth import _owned_job, require_agent_owner
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_grant_client,
    get_job_event_signal,
    get_storage,
    require_store,
)
from cloud_agent.application import send_now as send_now_delivery
from cloud_agent.application.follow_ups import (
    prepare_follow_up,
    sandbox_context_lost_error,
    session_archived_error,
)
from cloud_agent.application.job_admission import _idempotency_conflict
from cloud_agent.application.pending_messages import prepared_head, settle_queue
from cloud_agent.application.session_fork import materialize_session_fork
from cloud_agent.domain.execution import RUNNING, SUCCEEDED, TERMINAL_STATES
from cloud_agent.domain.pending_messages import PreparedHead, SettleOutcome
from cloud_agent.domain.requests import request_fingerprint
from cloud_agent.logging_utils import get_logger
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager_routing import sandbox_manager_for_attempt
from cloud_agent.schemas import (
    AgentJobResponse,
    AgentPendingMessageSendNow,
    AgentPendingMessageUpdate,
    AgentQuestionAnswer,
    AgentQuestionAnswerResponse,
    AgentThreadMessageCreate,
    AgentThreadMessageResult,
    AgentThreadQuestionsResponse,
    AgentThreadQueueResponse,
)
from cloud_agent.workers.job_event_signal import (  # noqa: TC001 — FastAPI resolves it.
    JobEventSignal,
)

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        ConversationCommands,
        ConversationQueries,
        GrantRepository,
        HostPoolRepository,
        PendingMessageRepository,
        ResidencyRepository,
        SessionForkRepository,
        SkillRepository,
        SourceControlRepository as SourceControlPort,
    )


router = APIRouter(prefix="/v1/agent")

#: The moved handlers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _TurnsStorage(Protocol):
    """The capabilities the message, queue and fork entry points reach for.

    ``attachments`` is here because a message resolves the uploads it names
    before it is accepted, and again before its turn is created.
    """

    attachments: AttachmentRepository
    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    grants: GrantRepository
    host_pool: HostPoolRepository
    pending_messages: PendingMessageRepository
    residency: ResidencyRepository
    session_fork: SessionForkRepository
    skills: SkillRepository
    source_control: SourceControlPort


def _require_store(store: _TurnsStorage | None) -> _TurnsStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


def _no_such_thread_error(thread_id: str) -> HTTPException:
    """A conversation this owner does not have, answered like a missing one."""
    return HTTPException(
        status_code=404,
        detail={"error": {"type": "not_found", "message": f"No such task: {thread_id}"}},
    )


def _no_such_message_error() -> HTTPException:
    """A waiting message that is not there any more — deleted, or already sent."""
    return HTTPException(
        status_code=404,
        detail={
            "error": {
                "type": "pending_message_not_found",
                "message": "This message is no longer waiting: it was sent or removed.",
            }
        },
    )


def _message_refused(result: dict[str, Any]) -> HTTPException:
    """The owner-facing refusal for a queue write the store turned down."""
    error = result.get("error")
    if error == "not_found":
        return _no_such_message_error()
    if error == "session_archived":
        return session_archived_error()
    if error == "sandbox_context_lost":
        return sandbox_context_lost_error()
    if error == "client_request_conflict":
        return _idempotency_conflict()
    if error == "conversation_changed":
        return HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "conversation_changed",
                    "message": (
                        "A chat sharing this workspace changed or is still busy. Wait for "
                        "all chats' messages and workspace publication to finish, then "
                        "refresh and try again."
                    ),
                }
            },
        )
    if error == "attachments_unavailable":
        return HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_attachment_selection",
                    "message": "One or more uploads are no longer available. Attach them again.",
                }
            },
        )
    current = result.get("current")
    if error == "revision_conflict":
        message = "This message changed since you read it. The current version is attached."
    else:
        message = "This message is being handed to the running turn and cannot change now."
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": str(error),
                "message": message,
                # The message as it is now, so a stale tab can show what won
                # instead of retrying over it.
                "current": _pending_response(current).model_dump(mode="json")
                if current is not None
                else None,
            }
        },
    )


async def _owned_latest_turn(
    job_store: _TurnsStorage, thread_id: str, user: dict[str, Any]
) -> dict[str, Any]:
    """The owner's conversation's newest turn, or 404 for anyone else's.

    The newest turn is what a message follows, so its read is also the
    ownership check: a thread somebody else owns, or one that was deleted,
    answers exactly like one that never existed.
    """
    latest = await job_store.conversation_queries.latest_turn(thread_id=thread_id)
    if latest is None or latest["user_id"] != user["user_id"] or latest.get("deleted_at"):
        raise _no_such_thread_error(thread_id)
    return latest


def _question_unreachable() -> HTTPException:
    """Keep a transport failure distinct from an agent with no questions."""
    return HTTPException(
        status_code=503,
        detail={
            "error": {
                "type": "question_unreachable",
                "message": "Could not reach the agent's questions. Try again.",
            }
        },
    )


@router.get("/threads/{thread_id}/questions", response_model=AgentThreadQuestionsResponse)
async def get_thread_questions(
    thread_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
) -> AgentThreadQuestionsResponse:
    """Read the live attempt's native questions, including after a page reload."""
    job_store = _require_store(store)
    latest = await _owned_latest_turn(job_store, thread_id, user)
    attempt_id = latest.get("current_attempt_id")
    result = AgentThreadQuestionsResponse(job_id=str(latest["id"]), attempt_id=attempt_id)
    if latest.get("state") != RUNNING or latest.get("cancel_requested") or attempt_id is None:
        return result
    sandbox_manager = await sandbox_manager_for_attempt(job_store.host_pool, int(attempt_id))
    if sandbox_manager is None:
        raise _question_unreachable()
    try:
        pending = await sandbox_manager.pending_shared_questions(
            thread_id, owner=f"attempt:{attempt_id}"
        )
    except SandboxManagerError as exc:
        raise _question_unreachable() from exc
    try:
        return AgentThreadQuestionsResponse.model_validate(
            {"job_id": result.job_id, "attempt_id": attempt_id, "questions": pending["questions"]}
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "invalid_question_response",
                    "message": "The agent returned an invalid question. Try again.",
                }
            },
        ) from exc


@router.post("/threads/{thread_id}/questions", response_model=AgentQuestionAnswerResponse)
async def answer_thread_question(
    thread_id: str,
    body: AgentQuestionAnswer,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
) -> AgentQuestionAnswerResponse:
    """Answer only the native request belonging to the attempt the owner saw."""
    job_store = _require_store(store)
    latest = await _owned_latest_turn(job_store, thread_id, user)
    if (
        latest.get("state") != RUNNING
        or latest.get("cancel_requested")
        or str(latest["id"]) != body.job_id
        or latest.get("current_attempt_id") != body.attempt_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "question_expired",
                    "message": "This question's turn has ended or changed. Refresh the questions.",
                }
            },
        )
    sandbox_manager = await sandbox_manager_for_attempt(job_store.host_pool, body.attempt_id)
    if sandbox_manager is None:
        raise _question_unreachable()
    try:
        answer = await sandbox_manager.answer_shared_question(
            thread_id,
            owner=f"attempt:{body.attempt_id}",
            request_id=body.request_id,
            answers=body.answers,
            reject=body.reject,
        )
    except SandboxManagerError as exc:
        raise _question_unreachable() from exc
    return AgentQuestionAnswerResponse(
        accepted=answer.get("accepted") is True,
        reason=answer.get("reason") if answer.get("accepted") is not True else None,
    )


async def _queue_result(
    job_store: _TurnsStorage,
    *,
    thread_id: str,
    user: dict[str, Any],
    outcome: SettleOutcome,
    message_id: str | None = None,
    client_request_id: str | None = None,
    fingerprint: str | None = None,
) -> AgentThreadMessageResult:
    """What a queue action left behind: the turn it started and the message still waiting."""
    job = (
        _job_response(
            await _owned_job(job_store.conversation_queries, str(outcome.job["id"]), user)
        )
        if outcome.job is not None
        else None
    )
    state = await job_store.pending_messages.queue_state(
        thread_id=thread_id, user_id=user["user_id"]
    )
    pending = None
    if message_id is not None and state is not None:
        pending = next((row for row in state["messages"] if row["id"] == message_id), None)
    if job is None and pending is None and client_request_id is not None:
        # Sent by a settlement that ran between this request's own and the read
        # above. Its key travelled onto the turn it became.
        replayed = await job_store.conversation_commands.replayed_submission(
            user_id=user["user_id"],
            client_request_id=client_request_id,
            fingerprint=fingerprint,
        )
        if replayed is not None and replayed.get("id") is not None:
            job = _job_response(
                await _owned_job(job_store.conversation_queries, str(replayed["id"]), user)
            )
    return AgentThreadMessageResult(
        pending=_pending_response(pending) if pending is not None else None,
        job=job,
        queue_paused_reason=(state or {}).get("queue_paused_reason"),
    )


async def _settle(
    job_store: _TurnsStorage,
    *,
    thread_id: str,
    gateway: Any,
    app_credentials: Any | None,
    prepared: PreparedHead | None = None,
) -> SettleOutcome:
    """Settle after an owner's queue change; a failure leaves it for the sweep.

    The change itself has committed. A settlement that cannot finish — the
    gateway not answering a model check, say — must not turn it into a 500 the
    owner would retry; the next settlement, at the latest the periodic sweep,
    does the rest.
    """
    try:
        return await settle_queue(
            job_store,
            thread_id=thread_id,
            gateway=gateway,
            app_credentials=app_credentials,
            prepared=prepared,
        )
    except Exception:
        logger.warning(
            "agent_pending_queue_settle_failed",
            extra={"event": "agent_pending_queue_settle_failed", "thread_id": thread_id},
            exc_info=True,
        )
        return SettleOutcome()


@router.post(
    "/threads/{thread_id}/messages", response_model=AgentThreadMessageResult, status_code=201
)
async def submit_thread_message(
    thread_id: str,
    body: AgentThreadMessageCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentThreadMessageResult:
    """Send a message: the next turn now if nothing is running, otherwise queued.

    Every message goes through here. It is checked against the conversation's
    newest turn — the same checks a follow-up always passed — and put at the end
    of the thread's queue; the queue is then settled, which on an idle
    conversation makes it the next turn before this returns (``job``). With a
    turn running it waits (``pending``) and is sent, in order, when that turn
    ends. Sending on an idle conversation whose queue was paused is the owner
    continuing: older waiting messages go first.

    An ``Idempotency-Key`` names this one submission. A resend answers with what
    the first send made — the turn if it has become one, the waiting message if
    not — and the same key under different content is a 409
    ``idempotency_conflict``.

    ``require_idle`` binds a fresh action to ``expected_latest_job_id`` and
    refuses it with ``conversation_changed`` if any chat sharing the workspace
    has a live turn or waiting message, a Fork is active, or the workspace is
    unpublished at the locked write. An accepted idle-only action creates its
    turn in that same transaction without entering the pending queue.
    """
    job_store = _require_store(store)
    latest = await _owned_latest_turn(job_store, thread_id, user)
    fingerprint = (
        request_fingerprint(
            "thread_message",
            {"thread_id": thread_id, **body.model_dump(mode="json")},
        )
        if idempotency_key is not None
        else None
    )
    if idempotency_key is not None:
        # Before the checks that can change between two sends of one
        # submission: a first send whose response was lost must still reach
        # what it made, whatever has happened to the conversation since.
        replayed_job = await job_store.conversation_commands.replayed_submission(
            user_id=user["user_id"], client_request_id=idempotency_key, fingerprint=fingerprint
        )
        if replayed_job is not None:
            if replayed_job.get("error") == "client_request_conflict":
                raise _idempotency_conflict()
            return AgentThreadMessageResult(
                job=_job_response(
                    await _owned_job(job_store.conversation_queries, str(replayed_job["id"]), user)
                )
            )
        replayed = await job_store.pending_messages.replayed_message(
            user_id=user["user_id"], client_request_id=idempotency_key, fingerprint=fingerprint
        )
        if replayed is not None:
            if replayed.get("error") == "client_request_conflict":
                raise _idempotency_conflict()
            return AgentThreadMessageResult(pending=_pending_response(replayed["message"]))
    prepared = await prepare_follow_up(
        latest,
        user=user,
        skill_ids=body.skill_ids,
        attachment_ids=body.attachment_ids,
        runtime=None,
        model=body.model,
        reasoning_effort=body.reasoning_effort,
        store=job_store,
        app_credentials=app_credentials,
        gateway=gateway,
    )
    submitted = await job_store.pending_messages.submit_message(
        thread_id=thread_id,
        user_id=user["user_id"],
        prompt=body.prompt,
        skill_ids=body.skill_ids,
        attachment_ids=body.attachment_ids,
        # The canonical id the check resolved, never the alias typed: it is
        # checked again against whatever turn is newest when this is sent.
        model=prepared.model,
        reasoning_effort=prepared.reasoning_effort if body.require_idle else body.reasoning_effort,
        client_request_id=idempotency_key,
        client_request_fingerprint=fingerprint,
        require_idle=body.require_idle,
        expected_latest_job_id=body.expected_latest_job_id,
        reset_reasoning_effort=prepared.reset_reasoning_effort,
        skills=prepared.skills,
        attachments=prepared.attachments,
    )
    if submitted.get("error") is not None:
        if submitted["error"] == "not_found":
            raise _no_such_thread_error(thread_id)
        raise _message_refused(submitted)
    if submitted.get("job_id") is not None:
        # A guarded action created its turn atomically, or a concurrent send
        # with this key got there first.
        return AgentThreadMessageResult(
            job=_job_response(
                await _owned_job(job_store.conversation_queries, str(submitted["job_id"]), user)
            )
        )
    message = submitted["message"]
    logger.info(
        "agent_message_submitted",
        extra={
            "event": "agent_message_submitted",
            "thread_id": thread_id,
            "message_id": message["id"],
        },
    )
    outcome = await _settle(
        job_store,
        thread_id=thread_id,
        gateway=gateway,
        app_credentials=app_credentials,
        # The check this request already made, so an idle conversation does
        # not ask the gateway twice for the same message.
        prepared=prepared_head(message, parent_job_id=str(latest["id"]), resolved=prepared),
    )
    return await _queue_result(
        job_store,
        thread_id=thread_id,
        user=user,
        outcome=outcome,
        message_id=str(message["id"]),
        client_request_id=idempotency_key,
        fingerprint=fingerprint,
    )


@router.get("/threads/{thread_id}/messages", response_model=AgentThreadQueueResponse)
async def get_thread_queue(
    thread_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
) -> AgentThreadQueueResponse:
    """The conversation's waiting messages and its newest turn, without the transcript.

    The page reads this while messages wait: when the newest turn changes to
    one of them, it has been sent.
    """
    job_store = _require_store(store)
    latest = await _owned_latest_turn(job_store, thread_id, user)
    state = await job_store.pending_messages.queue_state(
        thread_id=thread_id, user_id=user["user_id"]
    )
    if state is None:
        raise _no_such_thread_error(thread_id)
    return AgentThreadQueueResponse(
        thread_id=thread_id,
        pending_messages=[_pending_response(message) for message in state["messages"]],
        queue_paused_reason=state["queue_paused_reason"],
        latest_job_id=str(latest["id"]),
        latest_turn_no=latest.get("turn_no"),
        latest_job_state=latest.get("state"),
        mid_turn_input_supported=mid_turn_input_supported(latest),
    )


@router.patch("/threads/{thread_id}/messages/{message_id}", response_model=AgentThreadMessageResult)
async def edit_thread_message(
    thread_id: str,
    message_id: str,
    body: AgentPendingMessageUpdate,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentThreadMessageResult:
    """Change a waiting message, at the revision the caller read.

    Only the fields sent change. ``state: held`` is how an editor stops the
    queue at this message while it is being rewritten; ``state: queued`` lets
    it go again. A stale ``revision`` is a 409 ``revision_conflict`` carrying
    the message as it is now.
    """
    job_store = _require_store(store)
    latest = await _owned_latest_turn(job_store, thread_id, user)
    changes = {
        field: getattr(body, field) for field in body.model_fields_set if field != "revision"
    }
    if changes.keys() & {"skill_ids", "attachment_ids", "model", "reasoning_effort"}:
        # A changed choice is checked the way a new message's is, now rather
        # than when the message is sent, so a bad one is refused while its
        # author is still looking at it.
        prepared = await prepare_follow_up(
            latest,
            user=user,
            skill_ids=changes.get("skill_ids"),
            attachment_ids=changes.get("attachment_ids"),
            runtime=None,
            model=changes.get("model"),
            reasoning_effort=changes.get("reasoning_effort"),
            store=job_store,
            app_credentials=app_credentials,
            gateway=gateway,
        )
        if changes.get("model"):
            changes["model"] = prepared.model
    result = await job_store.pending_messages.edit_message(
        thread_id=thread_id,
        user_id=user["user_id"],
        message_id=message_id,
        revision=body.revision,
        changes=changes,
    )
    if result.get("error") is not None:
        raise _message_refused(result)
    outcome = await _settle(
        job_store, thread_id=thread_id, gateway=gateway, app_credentials=app_credentials
    )
    return await _queue_result(
        job_store, thread_id=thread_id, user=user, outcome=outcome, message_id=message_id
    )


@router.delete(
    "/threads/{thread_id}/messages/{message_id}", response_model=AgentThreadMessageResult
)
async def delete_thread_message(
    thread_id: str,
    message_id: str,
    revision: int = Query(..., ge=1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentThreadMessageResult:
    """Withdraw a waiting message, at the revision the caller read.

    Its uploads go back to being staged. A stale ``revision`` is a 409
    ``revision_conflict`` carrying the message as it is now.
    """
    job_store = _require_store(store)
    await _owned_latest_turn(job_store, thread_id, user)
    result = await job_store.pending_messages.delete_message(
        thread_id=thread_id, user_id=user["user_id"], message_id=message_id, revision=revision
    )
    if result.get("error") is not None:
        raise _message_refused(result)
    # The message may have been what stopped the line; what was behind it can
    # go now.
    outcome = await _settle(
        job_store, thread_id=thread_id, gateway=gateway, app_credentials=app_credentials
    )
    return await _queue_result(job_store, thread_id=thread_id, user=user, outcome=outcome)


def _send_now_refused(result: dict[str, Any]) -> HTTPException:
    """The owner-facing refusal for a message that cannot be sent into the turn now."""
    error = result.get("error")
    if error == "not_running":
        return HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "not_running",
                    "message": "The turn has not started, or is stopping; the message "
                    "stays in line and goes when it can.",
                }
            },
        )
    if error == "has_attachments":
        return HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "send_now_has_attachments",
                    "message": "A message with uploads starts a turn of its own; it "
                    "cannot join the running one.",
                }
            },
        )
    if error == "not_editable":
        current = result.get("current") or {}
        if current.get("state") != "sending":
            return HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "not_editable",
                        "message": "This message is being edited or was refused; save it first.",
                        "current": _pending_response(current).model_dump(mode="json")
                        if current
                        else None,
                    }
                },
            )
    return _message_refused(result)


def _mid_turn_input_unsupported() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "mid_turn_input_unsupported",
                "message": "This conversation's turn cannot take a message while it "
                "runs; the message stays in line and goes when the turn ends.",
            }
        },
    )


#: Hand-overs waiting for a host, kept so they are not collected mid-wait.
_hand_overs: set[asyncio.Task[None]] = set()


async def close_send_now_handovers() -> None:
    """Cancel and join request-spawned handoffs before app resources close."""
    tasks = tuple(_hand_overs)
    for task in tasks:
        task.cancel()
    if tasks:
        joined = asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            # The owner still has to wait for task cleanup before closing the
            # store and sandbox manager clients; propagate its cancellation afterwards.
            await joined
            raise


async def _hand_over_once_claimed(
    job_store: _TurnsStorage,
    *,
    thread_id: str,
    job_id: str,
    message: dict[str, Any],
    gateway: Any,
    app_credentials: Any | None,
    signal: JobEventSignal | None = None,
) -> None:
    """Eagerly wait for a host; the durable worker remains the crash backstop."""
    try:
        outcome = await send_now_delivery.wait_for_handoff(
            job_store,
            job_id=job_id,
            message=message,
            signal=signal,
        )
        if outcome.requeued:
            await _settle(
                job_store,
                thread_id=thread_id,
                gateway=gateway,
                app_credentials=app_credentials,
            )
    except Exception:
        logger.warning(
            "agent_send_now_hand_over_failed",
            extra={"event": "agent_send_now_hand_over_failed", "thread_id": thread_id},
            exc_info=True,
        )


@router.post(
    "/threads/{thread_id}/messages/{message_id}/send-now",
    response_model=AgentThreadMessageResult,
)
async def send_pending_message_now(
    thread_id: str,
    message_id: str,
    body: AgentPendingMessageSendNow,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
    gateway: Any = Depends(get_gateway_client),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    signal: JobEventSignal | None = Depends(get_job_event_signal),
) -> AgentThreadMessageResult:
    """Send one waiting message into the live turn.

    The owner's choice, kept: the message goes in front of the model before
    its next tool call, and joins the transcript where the turn's own stream
    says the turn took it. Until then it is ``sending``. A turn no host has
    taken yet is given it once one has. A turn that answers without another
    tool call never takes it, and it goes as the next turn, as any waiting
    message does. A turn that cannot have it at all — its host is out of
    reach, or it just ended — leaves it waiting where it was, and
    ``not_delivered_reason`` says why. With nothing live, this is an ordinary
    send.
    """
    job_store = _require_store(store)
    latest = await _owned_latest_turn(job_store, thread_id, user)
    if latest.get("state") not in TERMINAL_STATES and not mid_turn_input_supported(latest):
        raise _mid_turn_input_unsupported()
    began = await job_store.pending_messages.begin_send_now(
        thread_id=thread_id,
        user_id=user["user_id"],
        message_id=message_id,
        revision=body.revision,
    )
    if began.get("idle"):
        outcome = await _settle(
            job_store, thread_id=thread_id, gateway=gateway, app_credentials=app_credentials
        )
        return await _queue_result(
            job_store, thread_id=thread_id, user=user, outcome=outcome, message_id=message_id
        )
    if "error" in began:
        raise _send_now_refused(began)
    message = began["message"]
    if began["attempt_id"] is None:
        task = asyncio.create_task(
            _hand_over_once_claimed(
                job_store,
                thread_id=thread_id,
                job_id=str(began["job_id"]),
                message=message,
                gateway=gateway,
                app_credentials=app_credentials,
                signal=signal,
            )
        )
        _hand_overs.add(task)
        task.add_done_callback(_hand_overs.discard)
        return await _queue_result(
            job_store,
            thread_id=thread_id,
            user=user,
            outcome=SettleOutcome(),
            message_id=message_id,
        )
    try:
        handoff = await send_now_delivery.deliver_handoff(
            job_store,
            message_id=message_id,
            revision=int(message["revision"]),
        )
    except Exception:
        # The durable row has committed.  A transient database or sandbox manager error
        # must not make the owner retry the request; the recovery worker owns it.
        logger.warning(
            "agent_send_now_hand_over_failed",
            extra={"event": "agent_send_now_hand_over_failed", "thread_id": thread_id},
            exc_info=True,
        )
        handoff = send_now_delivery.HandoffOutcome()
    if handoff.reason is None:
        return await _queue_result(
            job_store,
            thread_id=thread_id,
            user=user,
            outcome=SettleOutcome(),
            message_id=message_id,
        )
    # The turn may have ended meanwhile. If this owner put the message back,
    # settle it now; otherwise that concurrent ending already did so.
    outcome = (
        await _settle(
            job_store,
            thread_id=thread_id,
            gateway=gateway,
            app_credentials=app_credentials,
        )
        if handoff.requeued
        else SettleOutcome()
    )
    result = await _queue_result(
        job_store, thread_id=thread_id, user=user, outcome=outcome, message_id=message_id
    )
    result.not_delivered_reason = handoff.reason
    return result


@router.post("/threads/{thread_id}/queue/resume", response_model=AgentThreadMessageResult)
async def resume_thread_queue(
    thread_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentThreadMessageResult:
    """Continue a paused queue from its first message.

    The pause is cleared and, when nothing is running, the ending that caused
    it counts as seen, so the queue does not stop again for the same turn. The
    queue is then settled: on an idle conversation its first message becomes
    the next turn before this returns (``job``).
    """
    job_store = _require_store(store)
    await _owned_latest_turn(job_store, thread_id, user)
    result = await job_store.pending_messages.resume_queue(
        thread_id=thread_id, user_id=user["user_id"]
    )
    if result.get("error") is not None:
        if result["error"] == "not_found":
            raise _no_such_thread_error(thread_id)
        raise _message_refused(result)
    logger.info(
        "agent_pending_queue_resumed",
        extra={"event": "agent_pending_queue_resumed", "thread_id": thread_id},
    )
    outcome = await _settle(
        job_store, thread_id=thread_id, gateway=gateway, app_credentials=app_credentials
    )
    return await _queue_result(job_store, thread_id=thread_id, user=user, outcome=outcome)


@router.post("/jobs/{job_id}/fork", response_model=AgentJobResponse, status_code=201)
async def fork_agent_job(
    job_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
    grants: Any = Depends(get_grant_client),
) -> AgentJobResponse:
    """Fork one settled native session into the same shared Docker workspace."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    not_settled = HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "not_settled",
                "message": "This turn is still active; wait for it to settle before forking it.",
            }
        },
    )
    if job["state"] not in TERMINAL_STATES:
        raise not_settled
    if job["state"] != SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "anchor_not_settled",
                    "message": "Only a successfully settled turn can be used as a Fork anchor.",
                }
            },
        )
    if job.get("thread_sandbox") is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_context_unavailable",
                    "message": "This historical chat has no shared native-session sandbox.",
                }
            },
        )

    sandbox = job.get("thread_sandbox") or {}
    if sandbox.get("shared_live") is not True:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_fork_unavailable",
                    "message": (
                        "This historical retained chat predates shared native-session "
                        "forks. Start a new chat to use same-workspace Fork."
                    ),
                }
            },
        )

    operation = await job_store.session_fork.prepare_session_fork(
        source_job_id=job_id,
        user_id=user["user_id"],
        idempotency_key=idempotency_key or f"generated:{secrets.token_urlsafe(24)}",
    )
    if operation is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    error = operation.get("error")
    if error:
        error_contract = {
            "session_archived": (
                "session_archived",
                "This conversation is archived. Restore it before creating a side chat.",
            ),
            "idempotency_conflict": (
                "idempotency_conflict",
                "This Idempotency-Key was already used for a different fork source.",
            ),
            "not_settled": (
                "not_settled",
                "This turn is still active; stop the run before forking it.",
            ),
            "anchor_not_settled": (
                "historical_snapshot_unavailable",
                "Native Fork requires a successfully completed assistant turn.",
            ),
            "not_shared_live": (
                "native_fork_unavailable",
                "This chat does not use a shared-native sandbox.",
            ),
            "native_fork_unavailable": (
                "native_fork_unavailable",
                "This retained sandbox version cannot fork from a historical turn.",
            ),
            "sandbox_not_ready": (
                "native_context_unavailable",
                "This chat's retained native context is not safe to fork.",
            ),
            "source_busy": (
                "sandbox_busy",
                "This chat is currently running a turn; retry after it settles.",
            ),
            "fork_in_progress": (
                "fork_in_progress",
                "Another native fork is already being prepared from this chat.",
            ),
            "resident_capacity_exhausted": (
                "resident_capacity_exhausted",
                "This shared sandbox has no free resident Agent CLI slot.",
            ),
            "native_context_unavailable": (
                "native_context_unavailable",
                "This chat has no settled native session to fork.",
            ),
            "runtime_mismatch": (
                "native_fork_unavailable",
                "The retained runtime no longer matches this chat.",
            ),
        }
        error_type, message = error_contract.get(
            str(error),
            ("native_fork_unavailable", "The retained native session cannot be forked now."),
        )
        raise HTTPException(
            status_code=409,
            detail={"error": {"type": error_type, "message": message}},
        )

    operation_id = str(operation["id"])

    if operation["state"] == "failed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_fork_failed",
                    "message": str(
                        operation.get("last_error") or "Native Fork could not complete."
                    ),
                }
            },
        )

    if operation["state"] == "preparing":
        # Resolve the host only for the state that still needs a native
        # mutation.  READY and MATERIALIZED are durable control-plane states:
        # a retry after a lost HTTP response must return/finalize the same
        # child even when the retaining host has since become unreachable.
        await materialize_session_fork(
            operation,
            job_id=job_id,
            job_store=job_store,
            grants=grants,
        )

    forked = await job_store.session_fork.finalize_session_fork(operation_id=operation_id)
    if forked is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_fork_fence_changed",
                    "message": "The source chat changed before native Fork finalization.",
                }
            },
        )
    logger.info(
        "agent_job_forked",
        extra={
            "event": "agent_job_forked",
            "job_id": forked["id"],
            "thread_id": forked.get("thread_id"),
            "source_job_id": job_id,
            "sandbox_clone": False,
            "workspace_shared": True,
            "fork_operation_id": operation_id,
        },
    )
    return _job_response(forked)
