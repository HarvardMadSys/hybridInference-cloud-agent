"""The runner-facing worker surface: claim, report, settle, finish.

Eighteen endpoints a host agent calls with a per-attempt capability token
rather than as an owner. The token carries the ``(job_id, attempt_id,
lease_generation)`` fence, so a worker whose lease was reaped is refused by the
store even though its token still parses; those refusals surface as 409, the
agreed signal for "you lost the lease, stop now".

Split out of the aggregator so the host-facing surface and the browser-facing
one are readable apart, and so the machine gate has one home. The router is a
flat leaf: FastAPI composes the aggregator's ``/v1/agent`` prefix with this
router's own, so a prefix here would double it.
"""

from __future__ import annotations

import base64
import contextlib
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from cloud_agent.api._body_limits import (
    HostBodyLimitRoute,
    payload_too_large,
    read_bounded_body,
)
from cloud_agent.api._sensitive_routes import no_store, no_store_exception
from cloud_agent.api._workspace_access import _workspace_id, _workspace_not_ready
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_owner_directory,
    get_source_control,
    get_storage,
    get_work_signal,
    require_store,
)
from cloud_agent.api.errors import as_http_exception
from cloud_agent.application.model_connections import residency_credential
from cloud_agent.application.pending_messages import settle_queue_when_possible
from cloud_agent.application.residency import residency_ttl_s
from cloud_agent.application.ssh_servers import push_ssh_access
from cloud_agent.application.turn_claim import ClaimRefusal, ClaimStorage, hand_over_claim
from cloud_agent.attachments.models import AttachmentLeaseLost, AttachmentView
from cloud_agent.domain.execution import QUEUED, RUNNING, TERMINAL_STATES
from cloud_agent.git_identity import residency_github_fields
from cloud_agent.goals.controller import continue_after_finish
from cloud_agent.goals.report import (
    GoalReportEnvelope,
    parse_goal_report,
    source_digest,
    unavailable,
)
from cloud_agent.host_pool import hash_secret
from cloud_agent.logging_utils import get_logger
from cloud_agent.measurement_ingest import Owner
from cloud_agent.measurement_persistence import persist_measurement
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager_routing import (
    ENV_SANDBOX_MANAGER_TOKEN,
    hydrate_shared_sandbox_manager,
    sandbox_manager_for_attempt,
)
from cloud_agent.schemas import (
    HostResidencyEstablishResponse,
    WorkerAckResponse,
    WorkerAttachmentContentResponse,
    WorkerAttachmentListResponse,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerContinuityRequest,
    WorkerEnvironmentSnapshotReport,
    WorkerFinishRequest,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerMeasurementRequest,
    WorkerMeasurementResponse,
    WorkerReleaseRequest,
    WorkerResourcesRequest,
    WorkerSandboxAbandonRequest,
    WorkerSandboxAdmissionRequest,
    WorkerSandboxProcessRequest,
    WorkerSandboxReserveRequest,
    WorkerSandboxResidencyRequest,
    WorkerSandboxRunningRequest,
    WorkerSandboxSettleRequest,
    WorkerTerminalSuspendRequest,
    WorkerThreadSandbox,
    WorkerTurnResourcesRequest,
    WorkerWaitRequest,
    WorkerWaitResponse,
)
from cloud_agent.source_control_runtime import (
    SourceControlRuntime,  # noqa: TC001 — FastAPI resolves it.
)
from cloud_agent.tokens import (
    SCOPE_FULL,
    InvalidAgentToken,
    InvalidHostToken,
    mint_residency_mcp_token,
    parse_host_token,
    parse_worker_token,
)
from cloud_agent.workers.work_signal import WorkSignal  # noqa: TC001 — FastAPI resolves it.
from cloud_agent.workspace_errors import _sandbox_manager_http_error

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from cloud_agent.ports import (
        ConversationCommands,
        ConversationQueries,
        ExecutionQueries,
        GoalRepository,
        GoalWakeupRepository,
        OperationMeasurements,
        PendingMessageRepository,
        ResidencyRepository,
        SandboxRepository,
        SkillRepository,
        SourceControlRepository as SourceControlPort,
    )
    from cloud_agent.sandbox_manager_client import SandboxManagerClient


#: Check the raw report before FastAPI parses JSON or validates its model.
MAX_TURN_RESOURCES_BODY_BYTES = 16 * 1024


class _BoundedTurnResourcesRoute(HostBodyLimitRoute):
    """Limit every worker request body before parsing, including chunked ones.

    Every body-carrying worker route gets the host ceiling. Turn-resource
    reports get their own, much smaller one, and a fixed validation answer.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the resource-report endpoint specially; the rest generically."""
        if (
            not self.path.endswith("/turn-resources")
            or self.methods is None
            or "POST" not in self.methods
        ):
            return super().get_route_handler()
        handler = APIRoute.get_route_handler(self)

        async def bounded_handler(request: Request) -> Response:
            body = await read_bounded_body(request, MAX_TURN_RESOURCES_BODY_BYTES)
            if body is None:
                return payload_too_large(MAX_TURN_RESOURCES_BODY_BYTES, "A turn resource snapshot")
            # Request.body() uses this same cache; give FastAPI only the bytes
            # accepted above rather than asking it to consume the stream again.
            request._body = body
            try:
                response = await handler(request)
            except RequestValidationError:
                # Missing fields otherwise repeat the entire input per error.
                # A fixed response keeps malformed reports and their content out of logs.
                return no_store(
                    JSONResponse(
                        status_code=422,
                        content={
                            "detail": {
                                "error": {
                                    "type": "invalid_request",
                                    "message": "Invalid turn resource snapshot.",
                                }
                            }
                        },
                    )
                )
            except HTTPException as exc:
                no_store_exception(exc)
                raise
            return no_store(response)

        return bounded_handler


router = APIRouter(prefix="/v1/agent", route_class=_BoundedTurnResourcesRoute)

#: The moved handlers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _WorkerStorage(ClaimStorage, Protocol):
    """Capabilities reached by runner endpoints and their claim/finish helpers."""

    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    execution_queries: ExecutionQueries
    goal_wakeups: GoalWakeupRepository
    goals: GoalRepository
    operation_measurements: OperationMeasurements
    pending_messages: PendingMessageRepository
    residency: ResidencyRepository
    sandbox: SandboxRepository
    skills: SkillRepository
    source_control: SourceControlPort


def _require_store(store: _WorkerStorage | None) -> _WorkerStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


#: Cheap structural check before touching the signing key: host credentials
#: carry their own token prefix, so unrelated credentials never reach the host
#: parser at all.
_HOST_TOKEN_HINT = "aht."


async def verify_claiming_host(
    authorization: str | None = Header(None),
    # The assembly, because that is what `get_storage` returns. Annotating one
    # port here did not narrow anything -- `Depends` erases the type, so the
    # annotation was simply false and the attribute below resolved against the
    # assembly at runtime, where it does not exist.
    store: _WorkerStorage | None = Depends(get_storage),
) -> dict[str, str]:
    """Authenticate the enrolled host that is claiming retained work.

    The credential names the host and is re-checked by ``claim_job`` in the
    same transaction that takes a capacity slot. There is no shared claim
    token and no request-body host name, so a machine cannot claim as a peer.
    """
    presented = (
        authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
    )
    if presented and store is not None and presented.startswith(_HOST_TOKEN_HINT):
        try:
            claims = parse_host_token(presented)
        except (InvalidHostToken, ValueError):
            # ValueError is the unset-``AGENT_CONTROL_TOKEN_SECRET`` case.
            claims = None
        if claims is not None:
            credential_hash = hash_secret(presented)
            host = await store.host_pool.authenticate_host(
                host=claims["host"], credential_hash=credential_hash
            )
            if host is None:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "error": {
                            "type": "unauthorized",
                            "message": "This host credential is no longer valid.",
                        }
                    },
                )
            return {"host": host["host"], "credential_hash": credential_hash}
    raise HTTPException(
        status_code=401,
        detail={
            "error": {
                "type": "unauthorized",
                "message": "A valid enrolled host credential is required.",
            }
        },
    )


def _worker_claims(authorization: str | None) -> dict[str, Any]:
    """Parse and verify the worker capability token from the auth header."""
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "unauthorized", "message": "Missing worker token."}},
        )
    try:
        claims = parse_worker_token(token)
    except InvalidAgentToken as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "unauthorized", "message": f"Invalid worker token: {exc}"}},
        ) from exc
    if claims.get("scope") != SCOPE_FULL:
        # Anything that is not the runner's full-scope control token. The one
        # narrower worker scope is ``mcp``, which the sandbox carries to reach
        # this service's MCP relay. (The old ``model`` scope no longer parses
        # at all: #822.)
        #
        # This is the structural refusal — and the reason the relay's own
        # check is written positively rather than as "not full". A sandbox
        # credential arriving on the worker surface is what an escaped
        # credential looks like.
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "insufficient_scope",
                    "message": (
                        "This credential may only be used for MCP relay calls, not for "
                        "reporting job state."
                    ),
                }
            },
        )
    return claims


def _lease_lost() -> HTTPException:
    """The 409 every fenced worker write raises when its lease is gone."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "lease_lost",
                "message": (
                    "This attempt no longer holds the job lease; stop all work immediately."
                ),
            }
        },
    )


def _turn_summary_conflict() -> HTTPException:
    """The 409 for a snapshot the store's ordering rules refused.

    Deliberately not ``_lease_lost``: the lease is fine and the worker must
    not stop, but this particular report can never be accepted — a stale
    ``report_seq``, two payloads claiming one sequence, a ``collecting``
    snapshot trying to overwrite a stored ``final`` one, or a second
    measurement claiming an attempt whose window is already fixed. Retrying
    the same body will produce the same answer, so the message says so.
    """
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "turn_summary_conflict",
                "message": (
                    "This turn resource snapshot conflicts with the one already stored "
                    "for this attempt; do not retry it."
                ),
            }
        },
    )


def _match_job(claims: dict[str, Any], job_id: str) -> None:
    """Reject a token minted for a different job."""
    if claims["job_id"] != job_id:
        raise _lease_lost()


async def _restore_terminal_suspension(
    sandbox_manager: SandboxManagerClient | None,
    *,
    workspace_id: str,
    job_id: str,
    lease_generation: int,
) -> None:
    """Best-effort compensation after a worker fails to finalize a resume."""
    if sandbox_manager is None:
        return
    try:
        await sandbox_manager.suspend_terminals(
            workspace_id,
            lease_generation=lease_generation,
            job_id=job_id,
        )
    except SandboxManagerError:
        logger.warning(
            "agent_terminal_resume_compensation_failed",
            exc_info=True,
            extra={
                "event": "agent_terminal_resume_compensation_failed",
                "workspace_id": workspace_id,
                "job_id": job_id,
                "lease_generation": lease_generation,
            },
        )


async def _worker_sandbox_manager(
    job_store: _WorkerStorage,
    job: dict[str, Any],
    *,
    attempt_id: int,
    required: bool,
) -> SandboxManagerClient | None:
    """Resolve the retained owner for worker-side terminal coordination.

    A failed attempt may settle before it ever reserves Docker, in which case
    there is genuinely no terminal state to coordinate. Once a group mapping
    exists, however, a missing sandbox manager is a routing failure and must not be
    treated as a successful no-op.
    """
    if job.get("thread_sandbox") is None:
        if required:
            raise _workspace_not_ready()
        return None
    sandbox_manager = await sandbox_manager_for_attempt(job_store.host_pool, attempt_id)
    try:
        sandbox_manager = await hydrate_shared_sandbox_manager(job, sandbox_manager)
    except SandboxManagerError as exc:
        raise _sandbox_manager_http_error(exc) from exc
    if sandbox_manager is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "The retained workspace owner is unavailable.",
                }
            },
        )
    return sandbox_manager


@router.post("/worker/claim", response_model=WorkerClaimResponse | None)
async def worker_claim(
    body: WorkerClaimRequest,
    claiming_host: dict[str, str] = Depends(verify_claiming_host),
    store: _WorkerStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    owners: Any = Depends(get_owner_directory),
) -> WorkerClaimResponse | None:
    """Claim the next queued job and mint this attempt's capability token.

    Returns ``null`` (HTTP 200) when the queue is empty, and equally when an
    operator has pinned agent jobs to a different host than the one this runner
    reports. The two are deliberately the same answer on the wire: a runner
    that has been switched away from should idle exactly as it does when there
    is no work, not treat it as an error worth retrying differently. Which host
    is active is an admin question, answered on the admin surface.

    **Enrolled-host only.** ``claim_job`` takes the oldest queued job across all
    tenants, and the response carries that job's repo, prompt, and metadata
    plus a working capability token for it — so ordinary API-key auth here
    would let any customer dequeue and read another customer's job, and drain
    the queue besides.

    The per-host credential opens registration/heartbeat/claim surfaces and
    never enters the sandbox; only the returned per-attempt tokens go in.
    """
    job_store = _require_store(store)
    from cloud_agent.settings import setting_env

    if not (setting_env(ENV_SANDBOX_MANAGER_TOKEN) or "").strip():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "The retained sandbox manager credential is not configured.",
                }
            },
        )
    claim = await job_store.attempt_commands.claim_job(
        worker_id=body.worker_id,
        lease_ttl_seconds=body.lease_ttl_seconds,
        host=claiming_host["host"],
        host_credential_hash=claiming_host["credential_hash"],
        claim_attestation=(body.attestation.model_dump() if body.attestation is not None else None),
        expected_deployment_sha=(os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip() or None,
    )
    if claim is None:
        return None
    try:
        outcome = await hand_over_claim(
            job_store,
            claim,
            app_credentials=app_credentials,
            owners=owners,
            attestation=body.attestation,
            host=claiming_host["host"],
        )
    except Exception as exc:
        # Anything that fails between the claim and the answer other than a
        # refusal — a store read, a malformed MCP binding — would otherwise
        # escape as a 500 with the claim still held: the job would wait out a
        # lease nothing is using, and the reaper would charge it a retry for a
        # turn no agent ever started.
        logger.warning(
            "agent_job_claim_failed",
            exc_info=True,
            extra={"event": "agent_job_claim_failed", "job_id": claim["id"]},
        )
        released = False
        with contextlib.suppress(Exception):
            released = await job_store.attempt_commands.release_claim(
                job_id=claim["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
                reason="claim_failed",
                retry_after_seconds=30,
            )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "claim_failed",
                    "message": (
                        "This job's claim could not be prepared; it has been returned "
                        "to the queue and no attempt was spent."
                        if released
                        else "This job's claim could not be prepared or returned to the "
                        "queue; its lease decides what happens next."
                    ),
                }
            },
        ) from exc
    if isinstance(outcome, ClaimRefusal):
        # The refusal has already released or ended its claim.
        raise as_http_exception(outcome.error) from outcome.error
    return outcome.response


@router.post("/worker/wait", response_model=WorkerWaitResponse)
async def worker_wait(
    body: WorkerWaitRequest,
    claiming_host: dict[str, str] = Depends(verify_claiming_host),
    signal: WorkSignal | None = Depends(get_work_signal),
) -> WorkerWaitResponse:
    """Hold an idle host's request until there may be work, then answer.

    A host whose claim found nothing asks here instead of sleeping out its poll
    interval, and claims again when this answers: at once if its token is stale,
    when a job it may take is queued, requeued or ends, or after ``timeout_seconds``.
    The answer is the token for its next wait, never a job: only a claim takes one.

    Enrolled hosts only, like the claim it precedes. 404 where nothing listens
    for work, which is also what a control plane predating this answers, so a
    host treats both the same way and polls.
    """
    if signal is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "not_found",
                    "message": "This listener does not signal work; poll the claim instead.",
                }
            },
        )
    token = await signal.wait(body.since, body.timeout_seconds, host=claiming_host["host"])
    return WorkerWaitResponse(token=token)


@router.post("/worker/jobs/{job_id}/heartbeat", response_model=WorkerHeartbeatResponse)
async def worker_heartbeat(
    job_id: str,
    body: WorkerHeartbeatRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerHeartbeatResponse:
    """Renew the attempt's lease and learn about pending cancellation."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    beat = await job_store.attempt_commands.heartbeat(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        lease_ttl_seconds=body.lease_ttl_seconds,
    )
    if not beat["ok"]:
        raise _lease_lost()
    return WorkerHeartbeatResponse(
        ok=True, state=beat["state"], cancel_requested=beat["cancel_requested"]
    )


#: How long after its attempt ends a startup measurement is still accepted.
#: The runner sends the record from a background thread as soon as the startup
#: window closes, retrying for seconds; a short turn can end before it lands,
#: which is why no live lease is asked for. An hour is far past any delivery,
#: and still a bound: a worker token carries no expiry of its own.
MEASUREMENT_GRACE_S = 3600.0


def _measurement_window_open(attempt: dict[str, Any], *, now: datetime) -> bool:
    """Whether a measurement may still be filed against this attempt.

    While it runs, always. Once it has ended, only within
    :data:`MEASUREMENT_GRACE_S` of the end the store recorded — an ended
    attempt without one says nothing about when it ended, so it is closed.
    """
    if attempt.get("status") == "running":
        return True
    finished_at = attempt.get("finished_at")
    return isinstance(finished_at, datetime) and now - finished_at <= timedelta(
        seconds=MEASUREMENT_GRACE_S
    )


@router.post(
    "/worker/jobs/{job_id}/measurements",
    response_model=WorkerMeasurementResponse,
)
async def worker_record_measurement(
    job_id: str,
    body: WorkerMeasurementRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerMeasurementResponse:
    """Store one finished measurement without requiring a still-live lease."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    if (
        str(body.record.get("producer") or "") != "runner"
        or str(body.record.get("operation") or "") != "attempt.startup"
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": "measurement_rejected",
                    "message": (
                        "the worker endpoint accepts only runner-rooted attempt.startup records"
                    ),
                }
            },
        )
    attempt_id = claims.get("attempt_id")
    generation = claims.get("lease_generation")
    if attempt_id is None or generation is None:
        raise _lease_lost()
    attempt = await job_store.execution_queries.get_attempt(
        job_id=job_id, attempt_id=int(attempt_id)
    )
    if attempt is None or int(attempt["lease_generation"]) != int(generation):
        raise _lease_lost()
    now = datetime.now(UTC)
    if not _measurement_window_open(attempt, now=now):
        raise _lease_lost()
    job = await job_store.conversation_queries.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": "No such job."}},
        )
    attempt_sandbox = attempt.get("thread_sandbox")
    attempt_sandbox = attempt_sandbox if isinstance(attempt_sandbox, dict) else {}
    current_sandbox = job.get("thread_sandbox")
    current_sandbox = current_sandbox if isinstance(current_sandbox, dict) else {}
    owner = Owner(
        owner_id=str(job.get("user_id") or "") or None,
        job_id=job_id,
        thread_id=str(attempt.get("thread_id") or job.get("thread_id") or "") or None,
        sandbox_id=str(attempt_sandbox.get("sandbox_id") or current_sandbox.get("sandbox_id") or "")
        or None,
        host_id=str(attempt.get("host_id") or "") or None,
        attempt_id=int(attempt_id),
        lease_generation=int(generation),
    )
    if body.run is None:
        # Payload-declared producer names are not trusted.  In-process control
        # plane records never call this endpoint, so every HTTP producer must
        # state the manifest against which its record is checked.
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": "measurement_rejected",
                    "message": "a producer must state its experiment manifest with its record",
                }
            },
        )
    result, reason = await persist_measurement(
        capability=job_store.operation_measurements,
        record=body.record,
        owner=owner,
        run=body.run,
        now=now,
    )
    if result in {"conflict", "run_conflict"}:
        return WorkerMeasurementResponse(
            result=result,
            measurement_id=str(body.record.get("measurement_id") or ""),
            reason=(
                "this measurement id already names different contents"
                if result == "conflict"
                else "this run id already names a different experiment"
            ),
        )
    if result == "rejected":
        raise HTTPException(
            status_code=422,
            detail={"error": {"type": "measurement_rejected", "message": reason}},
        )
    return WorkerMeasurementResponse(
        result=result, measurement_id=str(body.record.get("measurement_id") or "")
    )


@router.get("/worker/jobs/{job_id}/attachments", response_model=WorkerAttachmentListResponse)
async def worker_list_attachments(
    job_id: str,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAttachmentListResponse:
    """List what this conversation's /uploads holds, so it can be rebuilt.

    Scoped to the thread rather than the claimed turn: a retry on a fresh
    container has to restore all of them, and re-importing into a retained one
    simply overwrites the same paths. A file attached again by a later turn is
    listed once, as its newest upload.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    try:
        rows = await job_store.attachments.list_worker_attachments(
            job_id=job_id,
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
        )
    except AttachmentLeaseLost as exc:
        raise _lease_lost() from exc
    return WorkerAttachmentListResponse(
        attachments=[AttachmentView.model_validate(row) for row in rows]
    )


@router.get(
    "/worker/jobs/{job_id}/attachments/{attachment_id}",
    response_model=WorkerAttachmentContentResponse,
)
async def worker_read_attachment(
    job_id: str,
    attachment_id: int,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAttachmentContentResponse:
    """Read one upload's bytes for the host that is running this turn.

    The thread from the worker token's own job is what scopes the read, so a
    host holding a token for one conversation cannot fetch another's files.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    try:
        found = await job_store.attachments.read_worker_attachment(
            job_id=job_id,
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            attachment_id=attachment_id,
        )
    except AttachmentLeaseLost as exc:
        raise _lease_lost() from exc
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "upload_not_found", "message": "Upload not found."}},
        )
    path, content, content_type = found
    return WorkerAttachmentContentResponse(
        id=attachment_id,
        path=path,
        content_type=content_type,
        content_base64=base64.b64encode(content).decode("ascii"),
    )


@router.post("/worker/jobs/{job_id}/continuity", response_model=WorkerAckResponse)
async def worker_report_continuity(
    job_id: str,
    body: WorkerContinuityRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAckResponse:
    """Record which continuity rung this attempt runs on (design §9).

    Observability, deliberately fenced like every other worker write: a
    superseded attempt must not be able to relabel how the live one ran.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    recorded = await job_store.attempt_commands.record_continuity_mode(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        mode=body.mode,
        context_status=body.context_status,
    )
    if not recorded:
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


async def _worker_thread_id(job_store: ConversationQueries, job_id: str) -> str:
    """Resolve the claimed job's thread for restartable-sandbox writes."""
    job = await job_store.get_job(job_id)
    thread_id = None if job is None else job.get("thread_id")
    if not thread_id:
        raise HTTPException(status_code=409, detail="this job has no chat thread")
    return str(thread_id)


@router.post(
    "/worker/jobs/{job_id}/sandbox/reserve",
    response_model=WorkerThreadSandbox,
)
async def worker_reserve_thread_sandbox(
    job_id: str,
    body: WorkerSandboxReserveRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerThreadSandbox:
    """Reserve immutable one-chat/one-container identity before Docker create."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store.conversation_queries, job_id)
    try:
        row = await job_store.sandbox.reserve_thread_sandbox(
            thread_id=thread_id,
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            host=body.host,
            sandbox_id=body.sandbox_id,
            container_name=body.container_name,
            runtime_id=body.runtime_id,
            runtime_version=body.runtime_version,
            sandbox_image_digest=body.sandbox_image_digest,
            execution_profile=body.execution_profile.value,
            resident_slot_capacity=body.resident_slot_capacity,
            memory_limit=body.memory_limit,
            cpu_limit=body.cpu_limit,
            disk_limit_mib=body.disk_limit_mib,
            stop_timeout_seconds=body.stop_timeout_seconds,
            fork_helper_version=body.fork_helper_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise _lease_lost()
    return WorkerThreadSandbox(**row)


@router.post("/worker/jobs/{job_id}/environment-snapshot", status_code=204)
async def worker_report_environment_snapshot(
    job_id: str,
    body: WorkerEnvironmentSnapshotReport,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> Response:
    """Record what a new workspace did with its installation snapshot.

    Usage keeps a snapshot from expiring; a workspace that had none, or whose
    lockfiles or image differed, queues a build for its commit. Fenced like
    every other worker write: only the attempt holding the job's live lease,
    on a host still enrolled, is heard.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    try:
        recorded = await job_store.environment_snapshots.record_use(
            job_id=job_id,
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            snapshot_id=body.snapshot_id,
            outcome=body.outcome,
            base_sha=body.base_sha,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not recorded:
        raise _lease_lost()
    return Response(status_code=204)


@router.post(
    "/worker/jobs/{job_id}/sandbox/running",
    response_model=WorkerThreadSandbox,
)
async def worker_mark_thread_sandbox_running(
    job_id: str,
    body: WorkerSandboxRunningRequest,
    background: BackgroundTasks,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerThreadSandbox:
    """Confirm the exact retained container reached RUNNING."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store.conversation_queries, job_id)
    row = await job_store.sandbox.mark_thread_sandbox_running(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        container_id=body.container_id,
        context_status=body.context_status,
    )
    if row is None:
        raise _lease_lost()
    # The claim's SSH state was read before the group was reserved, and an
    # owner's change made since found no running group to push to. This push
    # is read after the group became running, so it holds every such change
    # and is newer than the claim's: the sandbox manager keeps it whichever of
    # the two arrives first. Every later change finds the group running.
    background.add_task(push_ssh_access, job_store, thread_ids=[thread_id])
    return WorkerThreadSandbox(**row)


@router.post(
    "/worker/jobs/{job_id}/sandbox/residency",
    response_model=HostResidencyEstablishResponse,
)
async def worker_establish_shared_sandbox_residency(
    job_id: str,
    body: WorkerSandboxResidencyRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> HostResidencyEstablishResponse:
    """Bind a member residency to its connection's key before the supervisor opens a CLI."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store.conversation_queries, job_id)
    host = await job_store.host_pool.host_for_job(job_id=job_id)
    if host is None or not host.get("host"):
        raise _lease_lost()
    row = await job_store.residency.establish_residency(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        host=str(host["host"]),
        runtime_id=body.runtime_id,
        runtime_version=body.runtime_version,
        sandbox_id=body.sandbox_id,
        ttl_seconds=residency_ttl_s(),
    )
    if row is None:
        raise _lease_lost()
    credential, revision, plan = await residency_credential(
        job_store,
        job=row,
        row=row,
        host=str(host["host"]),
        fence={"job_id": job_id, **claims},
    )
    if not credential:
        # Resident processes cannot safely outlive an attempt-scoped token, so
        # renewable residency is all-or-nothing at admission.
        await job_store.residency.release_residency(
            thread_id=thread_id,
            generation=int(row["generation"]),
            host=str(host["host"]),
        )
        raise HTTPException(
            status_code=503,
            detail="a renewable shared-session credential could not be minted",
        )
    recorded = await job_store.sandbox.record_shared_thread_residency(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_id=body.sandbox_id,
        residency_generation=int(row["generation"]),
    )
    if not recorded:
        await job_store.residency.release_residency(
            thread_id=thread_id,
            generation=int(row["generation"]),
            host=str(host["host"]),
        )
        raise _lease_lost()
    return HostResidencyEstablishResponse(
        thread_id=thread_id,
        generation=int(row["generation"]),
        ttl_seconds=residency_ttl_s(),
        credential=credential,
        model=row.get("model"),
        model_connection_revision=revision,
        model_connection_plan=plan,
        # The process this residency opens serves later turns too, which are
        # other jobs: a turn's own MCP token would die with this attempt and
        # force a restart at the next message. This one follows the residency.
        mcp_credential=mint_residency_mcp_token(
            thread_id=thread_id, residency_generation=int(row["generation"])
        ),
        **await residency_github_fields(job_store.source_control, thread_id, row),
    )


@router.post(
    "/worker/jobs/{job_id}/sandbox/process",
    response_model=WorkerAckResponse,
)
async def worker_record_thread_sandbox_process(
    job_id: str,
    body: WorkerSandboxProcessRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAckResponse:
    """Fence and record a replacement Agent CLI process generation."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store.conversation_queries, job_id)
    recorded = await job_store.sandbox.record_thread_sandbox_process(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        residency_generation=body.residency_generation,
        process_generation=body.process_generation,
    )
    if not recorded:
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


@router.post(
    "/worker/jobs/{job_id}/sandbox/admit",
    response_model=WorkerAckResponse,
)
async def worker_admit_thread_sandbox_turn(
    job_id: str,
    body: WorkerSandboxAdmissionRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAckResponse:
    """Persist at-most-once admission before the prompt reaches the CLI."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store.conversation_queries, job_id)
    recorded = await job_store.sandbox.admit_thread_sandbox_turn(
        thread_id=thread_id,
        job_id=job_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        residency_generation=body.residency_generation,
    )
    if not recorded:
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


@router.post(
    "/worker/jobs/{job_id}/sandbox/settle",
    response_model=WorkerThreadSandbox,
)
async def worker_settle_thread_sandbox(
    job_id: str,
    body: WorkerSandboxSettleRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerThreadSandbox:
    """Persist the native context boundary before arming idle deactivation."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store.conversation_queries, job_id)
    row = await job_store.sandbox.settle_thread_sandbox(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        residency_generation=body.residency_generation,
        job_id=job_id,
        context_status=body.context_status,
        native_session_id=body.native_session_id,
        base_sha=body.base_sha,
        idle_timeout_seconds=body.idle_timeout_seconds,
    )
    if row is None:
        raise _lease_lost()
    return WorkerThreadSandbox(**row)


@router.post(
    "/worker/jobs/{job_id}/sandbox/abandon",
    response_model=WorkerThreadSandbox,
)
async def worker_abandon_thread_sandbox(
    job_id: str,
    body: WorkerSandboxAbandonRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerThreadSandbox:
    """Release a failed activation; ambiguous admission becomes LOST."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store.conversation_queries, job_id)
    row = await job_store.sandbox.abandon_thread_sandbox(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        lost=body.lost,
        detail=body.detail,
        idle_timeout_seconds=body.idle_timeout_seconds,
    )
    if row is None:
        raise _lease_lost()
    if int(row.get("residency_generation") or 0) > 0:
        # The shared supervisor closes this member's process on abandon; the
        # residency ends with it. The generation fence makes a concurrent
        # successor immune to this cleanup.
        await job_store.residency.release_residency(
            thread_id=thread_id,
            generation=int(row["residency_generation"]),
            host=str(row["host"]),
        )
    return WorkerThreadSandbox(**row)


@router.post("/worker/jobs/{job_id}/resources", response_model=WorkerAckResponse)
async def worker_report_resources(
    job_id: str,
    body: WorkerResourcesRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAckResponse:
    """Record one closed attempt's resource and timing telemetry.

    Fenced like every other worker write, and idempotent: the rollup
    describes an attempt that is already closed, so a retried post is the same
    report said twice rather than a second report to add.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    recorded = await job_store.attempt_commands.record_attempt_resources(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        rollup=body.model_dump(),
    )
    if not recorded:
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


@router.post("/worker/jobs/{job_id}/turn-resources", response_model=WorkerAckResponse)
async def worker_report_turn_resources(
    job_id: str,
    body: WorkerTurnResourcesRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAckResponse:
    """Record one cumulative whole-turn member execution window.

    A separate report from the legacy resource rollup, with its own semantics:
    it is cumulative rather than a closed window, so the same snapshot can
    arrive more than once while ``report_seq`` stays the only thing that
    orders it. ``stored`` and ``duplicate`` are both success — an acknowledged
    checkpoint whose response was lost must be safe to resend unchanged.

    The body is bounded before it is parsed. The contract caps one summary at
    16 KiB of UTF-8, and a rejected oversize report has to be cheap to reject;
    letting the model build first would let an unbounded body allocate before
    the bound is ever checked. Measuring the raw body is also the only check
    that counts what actually crossed the wire rather than what survived
    parsing.

    Identity comes from the signed claims only. The body cannot name an
    attempt, a lease, or a job, so a snapshot can never be filed against
    anything but the caller's own fenced attempt.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    result = await job_store.attempt_commands.record_attempt_turn_summary(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        measurement_id=body.measurement_id,
        report_seq=body.report_seq,
        state=body.state,
        summary=body.model_dump(),
    )
    if result == "lease_lost":
        raise _lease_lost()
    if result == "conflict":
        raise _turn_summary_conflict()
    return WorkerAckResponse(ok=True)


@router.post("/worker/jobs/{job_id}/terminals/suspend", response_model=WorkerAckResponse)
async def worker_suspend_terminals(
    job_id: str,
    body: WorkerTerminalSuspendRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAckResponse:
    """Fence terminal writes, then freeze retained PTYs for a protected phase."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    job = await job_store.conversation_queries.get_job(job_id)
    if job is None:
        raise _lease_lost()
    workspace_id = _workspace_id(job)
    event_id = await job_store.attempt_commands.append_event(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        event_type="lifecycle",
        payload={"phase": body.phase},
    )
    if event_id is None:
        raise _lease_lost()
    sandbox_manager = await _worker_sandbox_manager(
        job_store, job, attempt_id=claims["attempt_id"], required=True
    )
    assert sandbox_manager is not None
    try:
        await sandbox_manager.suspend_terminals(
            workspace_id,
            lease_generation=claims["lease_generation"],
            job_id=job_id,
        )
    except SandboxManagerError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": exc.message,
                }
            },
        ) from exc
    return WorkerAckResponse(ok=True)


@router.post("/worker/jobs/{job_id}/terminals/resume", response_model=WorkerAckResponse)
async def worker_resume_terminals(
    job_id: str,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAckResponse:
    """Resume retained PTYs, then atomically publish workspace readiness."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    job = await job_store.conversation_queries.get_job(job_id)
    if job is None:
        raise _lease_lost()
    workspace_id = _workspace_id(job)
    fenced = await job_store.sandbox.fence_terminal_workspace(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if not fenced:
        raise _lease_lost()
    sandbox_manager = await _worker_sandbox_manager(
        job_store, job, attempt_id=claims["attempt_id"], required=True
    )
    assert sandbox_manager is not None
    try:
        await sandbox_manager.resume_terminals(
            workspace_id,
            lease_generation=claims["lease_generation"],
            job_id=job_id,
        )
    except SandboxManagerError as exc:
        await _restore_terminal_suspension(
            sandbox_manager,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": exc.message,
                }
            },
        ) from exc
    try:
        event_id = await job_store.attempt_commands.append_event(
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
    except Exception:
        await _restore_terminal_suspension(
            sandbox_manager,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise
    if event_id is None:
        await _restore_terminal_suspension(
            sandbox_manager,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


@router.post("/worker/jobs/{job_id}/release", response_model=WorkerAckResponse)
async def worker_release(
    job_id: str,
    body: WorkerReleaseRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
) -> WorkerAckResponse:
    """Hand a claimed turn back to the queue, having never started it.

    The one path from a claim back to ``queued`` without spending an attempt.
    A host claims a turn and then starts the process that will run it; when
    that process cannot be started, or exits without taking delivery, the turn
    has provably not begun and the person waiting on it should not wait out a
    lease that nothing is holding.

    A short retry delay lets other queued work proceed while the host recovers
    from a temporary refusal, including work that can resume an existing sandbox.

    **Not an escape hatch for a turn that is running.** The release is refused
    once the turn has been admitted to the CLI, and the check is the database's
    rather than the caller's: replaying an admitted prompt executes the same
    instruction twice, which is what the retained-session rules exist to
    prevent. A caller that is refused has lost nothing — the lease was always
    the backstop, and it still is.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    released = await job_store.attempt_commands.release_claim(
        job_id=job_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        reason=body.reason,
        retry_after_seconds=30,
    )
    if not released:
        # 409 rather than 400: the request was well formed and would have been
        # accepted a moment earlier. What changed is the turn, and an operator
        # reading this needs to know the work was not lost — it is either
        # running or already decided.
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "turn_not_releasable",
                    "message": (
                        "This turn cannot be returned to the queue: it has started, its "
                        "lease has moved on, or it is no longer running. Nothing was "
                        "changed; its lease decides what happens next."
                    ),
                }
            },
        )
    logger.info(
        "agent_turn_released",
        extra={
            "event": "agent_turn_released",
            "job_id": job_id,
            "attempt_id": claims["attempt_id"],
            "reason": body.reason,
        },
    )
    return WorkerAckResponse(ok=True, state=QUEUED)


async def _goal_envelope_for_turn(
    job: dict[str, Any], queries: ConversationQueries
) -> GoalReportEnvelope | None:
    """How this turn's final message read, if it was a goal round at all.

    Only asked about an *automatic round*: a person's turn is not part of the
    completion protocol, and parsing it would put a report on rows whose only
    relationship to a goal is that one was set while they ran.

    A turn that produced no assistant text at all made no report — it is
    ``absent``, and an absent report simply means "keep going". A turn whose
    message could not be read is different in kind, and is reported as such
    rather than being quietly treated as silence.

    A *clipped* message is the case that distinction exists for. The report
    block sits at the end of a final message, so a bound on its text — the
    shared 256 KiB event payload limit — removes exactly the part a reader needs.
    Reading the clipped text as "no report" would keep a goal working forever
    on a turn that may well have reported completion, so the completeness
    marker the runtime recorded travels back with the text.
    """
    if job.get("goal_id") is None or job.get("turn_source") != "goal":
        return None
    message = await queries.latest_assistant_message_state(job_id=str(job["id"]))
    if message is not None and message["truncated"]:
        return GoalReportEnvelope.from_parsed(
            unavailable("the runtime truncated this turn's final message"),
            source_digest=None,
        )
    text = None if message is None else str(message["content"])
    parsed = parse_goal_report(text)
    return GoalReportEnvelope.from_parsed(parsed, source_digest=source_digest(text))


@router.post("/worker/jobs/{job_id}/finish", response_model=WorkerAckResponse)
async def worker_finish(
    job_id: str,
    body: WorkerFinishRequest,
    authorization: str | None = Header(None),
    store: _WorkerStorage | None = Depends(get_storage),
    source_control: SourceControlRuntime | None = Depends(get_source_control),
    owners: Any = Depends(get_owner_directory),
) -> WorkerAckResponse:
    """Drive the fenced terminal transition for this attempt.

    Then, before acknowledging, act on what the ending means for the rest of
    the conversation: the owner's waiting messages first, then the goal.

    Takes the source-control runtime rather than the App's credentials: a turn
    that finished is recorded and acknowledged whether or not the App can be
    loaded right now (issue #47). Only sending the next waiting message and the
    next goal round wait for it.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    job = await job_store.conversation_queries.get_job(job_id)
    if job is None:
        raise _lease_lost()
    workspace_id = _workspace_id(job)
    if body.state not in TERMINAL_STATES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": f"state must be one of {list(TERMINAL_STATES)}",
                }
            },
        )
    fenced = await job_store.sandbox.fence_terminal_workspace(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if not fenced:
        raise _lease_lost()
    sandbox_manager = await _worker_sandbox_manager(
        job_store, job, attempt_id=claims["attempt_id"], required=False
    )
    if sandbox_manager is not None:
        try:
            await sandbox_manager.resume_terminals(
                workspace_id,
                lease_generation=claims["lease_generation"],
                job_id=job_id,
            )
        except SandboxManagerError as exc:
            await _restore_terminal_suspension(
                sandbox_manager,
                workspace_id=workspace_id,
                job_id=job_id,
                lease_generation=claims["lease_generation"],
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": exc.message,
                    }
                },
            ) from exc
    # Read the turn's self-report before its outcome is committed, so both land
    # in the same transaction: a terminal turn is never one whose goal answer is
    # missing because the read happened to be scheduled after the write.
    goal_envelope = await _goal_envelope_for_turn(job, job_store.conversation_queries)
    try:
        ok = await job_store.attempt_commands.transition(
            job_id=job_id,
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            from_states=(RUNNING,),
            to_state=body.state,
            detail=body.detail,
            base_sha=body.base_sha,
            goal_envelope=goal_envelope,
        )
    except Exception:
        await _restore_terminal_suspension(
            sandbox_manager,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise
    if not ok:
        await _restore_terminal_suspension(
            sandbox_manager,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise _lease_lost()
    # The owner's waiting messages go before anything the goal would add, so
    # the queue is settled first: its first message becomes the next turn now,
    # or a failed or cancelled ending pauses it. In its own guard for the same
    # reason as the goal below — the turn is already decided, and a runner
    # retrying a finish would find no transition left to make.
    github_app = None if source_control is None else source_control.github
    if job.get("thread_id") is not None:
        try:
            await settle_queue_when_possible(
                job_store,
                thread_id=str(job["thread_id"]),
                owners=owners,
                github_app=github_app,
            )
        except Exception:
            logger.warning(
                "agent_pending_queue_settle_failed",
                extra={
                    "event": "agent_pending_queue_settle_failed",
                    "job_id": job_id,
                    "thread_id": job.get("thread_id"),
                },
                exc_info=True,
            )
    # Synchronously, and before the runner is acknowledged. The owner's page
    # watches the newest turn: if the next round did not exist by the time this
    # returned, the conversation would look finished for a moment and the page
    # would stop polling — so the round the user is waiting to see would be the
    # one round nobody fetched.
    #
    # The obligation itself is already durable: it was written inside the
    # terminal transition above, and this only drains it now rather than at the
    # next tick. In its own guard, because the turn is already decided — an
    # error while *starting the next* turn must not turn a successful finish
    # into a 500 the runner will retry. Retrying would not help, and the retry
    # would find no transition left to make.
    #
    # While the GitHub App is unavailable this drains nothing: the wakeup stays
    # pending, unclaimed, for the reaper to consume once the App is back. The
    # page keeps polling an active goal, so the round still appears there.
    # Only this turn's conversation is drained; other conversations' wakeups
    # are the reaper's, and must not hold this runner's request.
    try:
        await continue_after_finish(
            store=job_store,
            job_id=job_id,
            thread_id=job.get("thread_id"),
            github_app=github_app,
            owners=owners,
        )
    except Exception:
        logger.warning(
            "agent_goal_continuation_failed",
            extra={"event": "agent_goal_continuation_failed", "job_id": job_id},
            exc_info=True,
        )
    return WorkerAckResponse(ok=True, state=body.state)
