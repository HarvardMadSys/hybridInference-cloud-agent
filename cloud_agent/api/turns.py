"""The two write paths that add a turn to an existing chat.

A follow-up continues a conversation; a fork splits one off at an anchor. Both
are substantial orchestrations — admission checks, entitlement re-checks,
idempotency, sandbox residency, grant minting.

Fork's own orchestration is not here: `application.session_fork` drives a
PREPARING operation to MATERIALIZED, so its failure branches are reachable
without an HTTP request. What stays is the anchor validation, the store's
`prepare_session_fork`, the finalize and the response — and
`SESSION_FORK_COORDINATOR_LEASE_S`, which bounds that flow, moved with it.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException

from cloud_agent.api._job_views import _job_response
from cloud_agent.api._owner_auth import _owned_job, require_agent_owner
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_grant_client,
    get_storage,
    require_store,
)
from cloud_agent.application.job_admission import (
    _idempotency_conflict,
    _require_offered_effort,
    _require_registered_runtime,
    _require_resolvable_model,
    _resolve_attachment_selection,
    _resolve_skill_selection,
)
from cloud_agent.application.session_fork import materialize_session_fork
from cloud_agent.domain.execution import SUCCEEDED, TERMINAL_STATES
from cloud_agent.domain.requests import request_fingerprint
from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import RESET_REASONING_EFFORT, AgentFollowUpRequest, AgentJobResponse

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        ConversationCommands,
        ConversationQueries,
        GrantRepository,
        HostPoolRepository,
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
    """The capabilities the follow-up and fork entry points reach for.

    ``attachments`` is here because a follow-up resolves the uploads it names
    before it creates the turn.
    """

    attachments: AttachmentRepository
    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    grants: GrantRepository
    host_pool: HostPoolRepository
    residency: ResidencyRepository
    session_fork: SessionForkRepository
    skills: SkillRepository
    source_control: SourceControlPort


def _require_store(store: _TurnsStorage | None) -> _TurnsStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


def _sandbox_context_lost_error() -> HTTPException:
    """Return the stable owner-facing error for an unrunnable retained chat."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "sandbox_context_lost",
                "message": (
                    "This chat's retained sandbox has an indeterminate or lost "
                    "context and cannot safely accept another message."
                ),
            }
        },
    )


def _session_archived_error() -> HTTPException:
    """Require an explicit unarchive before accepting another conversation turn."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "session_archived",
                "message": "Unarchive this session before continuing the conversation.",
            }
        },
    )


@router.post("/jobs/{job_id}/follow-ups", response_model=AgentJobResponse, status_code=201)
async def create_agent_follow_up(
    job_id: str,
    body: AgentFollowUpRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _TurnsStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentJobResponse:
    """Append a turn and queue its member in the retained shared sandbox.

    An ``Idempotency-Key`` names this one submission, as for creation: a
    resend returns the turn the first send made, different content under the
    same key is a 409 ``idempotency_conflict``.
    """
    job_store = _require_store(store)
    parent = await _owned_job(job_store.conversation_queries, job_id, user)
    # Ownership is proved above; the resend is answered next, before the checks
    # that can change between two sends of one submission. A first send whose
    # response was lost, followed by the session being archived or its sandbox
    # context going indeterminate, must still return the turn that send made —
    # otherwise the retry is refused for a reason the first send never saw.
    fingerprint = (
        request_fingerprint(
            "follow_up",
            {
                "parent_job_id": job_id,
                **body.model_dump(mode="json"),
            },
        )
        if idempotency_key is not None
        else None
    )
    if idempotency_key is not None:
        replayed = await job_store.conversation_commands.replayed_submission(
            user_id=user["user_id"],
            client_request_id=idempotency_key,
            fingerprint=fingerprint,
        )
        if replayed is not None:
            if replayed.get("error") == "client_request_conflict":
                raise _idempotency_conflict()
            return _job_response(
                await _owned_job(job_store.conversation_queries, str(replayed["id"]), user)
            )
    if parent.get("archived_at") is not None:
        raise _session_archived_error()
    retained = parent.get("thread_sandbox")
    if isinstance(retained, dict) and (
        retained.get("context_status") == "lost"
        or retained.get("state") in {"stopped_dirty", "lost"}
    ):
        raise _sandbox_context_lost_error()
    # The harness is fixed per thread: continuity is the harness
    # session, and no other harness can continue it. The field survives as an
    # explicit no-op so existing callers that echo the thread's runtime keep
    # working; a *different* runtime is refused rather than silently starting
    # a turn the thread's memory cannot follow.
    if body.runtime is not None:
        _require_registered_runtime(body.runtime)
        if body.runtime != parent["runtime"]:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            f"this thread runs on {parent['runtime']!r} and a thread's "
                            "harness is fixed; start a new chat to use "
                            f"{body.runtime!r}"
                        ),
                    }
                },
            )
    # A follow-up may switch models; the switched-to model gets the same
    # fail-fast as a fresh job. An omitted model inherits the parent's, which
    # was validated when it was chosen.
    model = None
    if body.model:
        model = await _require_resolvable_model(body.model, gateway=gateway, user=user)
    # Effort is per turn, like the model, and inherits when omitted. Unlike the
    # model it needs a way *back*: null already means "no override, inherit", so
    # clearing an inherited effort takes the explicit reset sentinel. Validated
    # against the model this turn will run — which may be the switched-to one,
    # and a pair that was offered last turn need not be offered this one.
    reasoning_effort: str | None = None
    reset_effort = body.reasoning_effort == RESET_REASONING_EFFORT
    if body.reasoning_effort is not None and not reset_effort:
        reasoning_effort = await _require_offered_effort(
            body.reasoning_effort,
            runtime=parent["runtime"],
            model=model or parent["model"],
            gateway=gateway,
            user=user,
        )
    try:
        await require_entitled_repo(
            parent["repo"],
            user["user_id"],
            store=job_store.source_control,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    # An unavailable account integration must not prevent unrelated conversation
    # work. The runner receives only usable servers, resolved when it claims
    # this turn, and Tools retains each connection's diagnosis.
    skills = (
        await _resolve_skill_selection(
            job_store.skills, user_id=user["user_id"], skill_ids=body.skill_ids
        )
        if body.skill_ids is not None
        else None
    )
    attachments = await _resolve_attachment_selection(
        job_store.attachments, user_id=user["user_id"], attachment_ids=body.attachment_ids
    )
    job = await job_store.conversation_commands.create_follow_up(
        parent_job_id=job_id,
        user_id=user["user_id"],
        prompt=body.prompt,
        runtime=body.runtime,
        # Canonical, or the parent's when omitted — never the alias the caller
        # typed, which the gateway's grant would not match.
        model=model if body.model else None,
        reasoning_effort=reasoning_effort,
        reset_reasoning_effort=reset_effort,
        **({"skills": skills} if skills is not None else {}),
        **({"attachments": attachments} if attachments is not None else {}),
        client_request_id=idempotency_key,
        # The same value the replay lookup above used, so the pre-check and the
        # unique index cannot disagree about what was submitted.
        client_request_fingerprint=fingerprint,
    )
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    if job.get("error") == "client_request_conflict":
        raise _idempotency_conflict()
    if job.get("error") == "session_archived":
        raise _session_archived_error()
    if job.get("error") == "sandbox_context_lost":
        # The durable store repeats the check under its thread/sandbox locks.
        # It can legitimately disagree with the advisory read above when the
        # active attempt becomes indeterminate during entitlement/tool checks.
        raise _sandbox_context_lost_error()
    logger.info(
        "agent_follow_up_created",
        extra={
            "event": "agent_follow_up_created",
            "job_id": job["id"],
            "thread_id": job.get("thread_id"),
            "parent_job_id": job.get("parent_job_id"),
        },
    )
    # Command rows omit retained membership joins. Return the same owner read
    # as GET /jobs/{id}, including the sandbox and native fork provenance.
    return _job_response(await _owned_job(job_store.conversation_queries, str(job["id"]), user))


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
