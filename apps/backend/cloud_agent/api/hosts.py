"""The two endpoints a host agent calls about itself: register and heartbeat.

Kept out of ``routes.py``. That file is the moved gateway router and is held to
a move-as-is diff; these are net-new phase-G surface, and mixing them in would
make the one file nobody should be editing the file everybody edits.

**Why enrollment exists at all.** A host must be something the platform issued
a credential to, so revoking it has meaning and a heartbeat/claim is evidence
rather than a request-body assertion. The credential opens those surfaces for
exactly one host and carries the machine identity, so a compromised host cannot
claim as its neighbour. There is no pool-wide claim credential.

Registration is unauthenticated *except* for the enrollment token, and that is
the point: a machine being set up has no credential yet. The token is
one-time, short-lived, hashed at rest, and **names the host it may register**,
so the window it opens is one machine, under one name, once, for fifteen
minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException

from cloud_agent.api._body_limits import HostBodyLimitRoute
from cloud_agent.api.deps import get_grant_client, get_renewal_schedule, get_storage
from cloud_agent.application.grant_revocation import revoke_detached_grants
from cloud_agent.grants import (
    RenewalSchedule,  # noqa: TC001 — FastAPI resolves annotations at runtime.
)
from cloud_agent.host_pool import (
    HEARTBEAT_INTERVAL_S,
    hash_secret,
)
from cloud_agent.sandbox_manager_routing import (
    SandboxManagerUrlRefused,
    probe_error_for_host,
    probe_sandbox_manager,
    validate_sandbox_manager_url,
)
from cloud_agent.schemas import (
    HostHeartbeatRequest,
    HostHeartbeatResponse,
    HostRegisterRequest,
    HostRegisterResponse,
    HostSandboxArchiveSettings,
    ResidencyBeat,
    RetainedSandboxBeat,
    RetainedSandboxStop,
)
from cloud_agent.tokens import InvalidHostToken, mint_host_token, parse_host_token
from cloud_agent_shared.host_settings import merge_admin_values, reported_values
from cloud_agent_shared.sandbox_archive_settings import SANDBOX_CHECKPOINT_CAPABILITY

if TYPE_CHECKING:
    from cloud_agent.ports import (
        GrantRepository,
        HostPoolRepository,
        ResidencyRepository,
        SandboxRepository,
    )

logger = logging.getLogger(__name__)


class _HostsStorage(Protocol):
    """The capabilities this module's handlers reach the store for.

    Named here so that reaching for one more is a deliberate edit to
    this list rather than a call that happens to type-check.
    """

    grants: GrantRepository
    host_pool: HostPoolRepository
    residency: ResidencyRepository
    sandbox: SandboxRepository


# Host traffic arrives through the tunnel, past the edge's body limits.
router = APIRouter(prefix="/v1/agent/host", tags=["agent-hosts"], route_class=HostBodyLimitRoute)


def _require_store(store: _HostsStorage | None) -> _HostsStorage:
    """Return the store or 503 — agent jobs are an optional deployment part."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Agent jobs are not configured for this deployment.",
        )
    return store


def _unauthorized(message: str) -> HTTPException:
    """The single refusal every host-credential failure produces.

    One message for an unknown host, a revoked credential, a superseded one and
    a disabled row, because distinguishing them tells an attacker which of
    those they achieved.
    """
    return HTTPException(
        status_code=401,
        detail={"error": {"type": "unauthorized", "message": message}},
    )


async def authenticate_host(
    authorization: str | None = Header(None),
    store: _HostsStorage | None = Depends(get_storage),
) -> dict[str, Any]:
    """Resolve the host behind a host credential, or refuse.

    Two checks, and both are needed. The signature proves this service minted
    the token — but a token this service minted last month for a machine that
    has since been revoked is still perfectly signed, so the hash comparison
    against the live row is what revocation actually acts on. Fails closed on
    every branch.
    """
    job_store = _require_store(store)
    token = (
        authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
    )
    if not token:
        raise _unauthorized("Missing host credential.")
    try:
        claims = parse_host_token(token)
    except InvalidHostToken as exc:
        raise _unauthorized(f"Invalid host credential: {exc}") from exc
    except ValueError as exc:
        # ``AGENT_CONTROL_TOKEN_SECRET`` is unset, so nothing this service ever
        # minted can be verified. 401 rather than the 500 the raise would
        # produce: the credential genuinely does not authenticate here, and the
        # operator's signal is the boot-time failure to mint, not a stack trace
        # per heartbeat.
        raise _unauthorized("Host credentials cannot be verified by this deployment.") from exc
    host = await job_store.host_pool.authenticate_host(
        host=claims["host"], credential_hash=hash_secret(token)
    )
    if host is None:
        raise _unauthorized("This host credential is no longer valid.")
    # The digest rides along in the authentication context and nowhere else.
    # The capacity half of a heartbeat re-checks it inside the statement that
    # writes, which is what refuses a delayed request from a credential that
    # has since been replaced by a re-enrollment of the same name; it is never
    # logged, never stored in a snapshot and never returned to a caller.
    return {**host, "credential_hash": hash_secret(token)}


def _checked_sandbox_manager_url(url: str | None) -> str | None:
    """Refuse a sandbox manager address this deployment will not send its token to.

    The control plane sends the pool's shared sandbox-manager token to
    whatever address a host reports, so accepting one is handing over a
    credential good for every sandbox manager in the pool. Refused with a 400 rather
    than dropped: a host whose address was silently ignored looks enrolled and
    then never serves a workspace.
    """
    try:
        return validate_sandbox_manager_url(url)
    except SandboxManagerUrlRefused as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_sandbox_manager_url", "message": str(exc)}},
        ) from exc


@router.post("/register", response_model=HostRegisterResponse)
async def register_host(
    body: HostRegisterRequest,
    store: _HostsStorage | None = Depends(get_storage),
) -> HostRegisterResponse:
    """Spend a one-time enrollment token and receive a host credential.

    The token is consumed before the credential is minted, and both happen
    before the response is written — so a machine that crashes mid-registration
    has burnt its token and needs a new one. That is the right way round: the
    alternative leaves a token that looks unused but may have already produced
    a credential.

    The host starts at ``enrolling`` and claims nothing until it heartbeats.
    A machine that registers and never comes up therefore never receives work,
    which is the difference between a pool entry and a promise.

    ``name`` is checked against the name the token was issued for, inside the
    same statement that spends it. Registration upserts, so a name already held
    by a live host loses its credential and drops back to ``enrolling``; before
    the binding, one typo here evicted a working machine from the pool. A
    mismatch does not consume the token, so a typo is retried rather than
    re-issued.
    """
    job_store = _require_store(store)
    # Validate every retryable, machine-supplied configuration field before
    # spending the one-time token. A refused sandbox manager address is a 400 the
    # operator can correct; consuming the token first turns that typo into a
    # second privileged admin operation for no security benefit.
    sandbox_manager_url = _checked_sandbox_manager_url(body.sandbox_manager_url)
    enrollment_token_hash = hash_secret(body.enroll_token)
    if not await job_store.host_pool.consume_enroll_token(
        token_hash=enrollment_token_hash, host=body.name
    ):
        # One answer for expired, already-spent, never-issued and issued-for-
        # another-host: they are the same fact from the presenter's side — this
        # token is not usable here — and separating them says which happened.
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "type": "unauthorized",
                    "message": (
                        "This enrollment token is not valid for this host name. Issue a new one."
                    ),
                }
            },
        )
    credential = mint_host_token(host=body.name)
    try:
        host = await job_store.host_pool.register_host(
            host=body.name,
            credential_hash=hash_secret(credential),
            enrollment_token_hash=enrollment_token_hash,
            capabilities=body.capabilities,
            slots_total=body.slots_total,
            sandbox_manager_url=sandbox_manager_url,
            runner_version=body.runner_version,
            labels=dict(body.labels),
        )
    except ValueError as exc:
        error_type = str(exc)
        if error_type == "enrollment_token_removed":
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "type": "unauthorized",
                        "message": "This enrollment token is no longer valid. Issue a new one.",
                    }
                },
            ) from exc
        if error_type == "host_drain_fenced":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "host_drain_fenced",
                        "message": (
                            "This host is held by an automated deployment drain. "
                            "The enrollment token was consumed; release the drain "
                            "and issue a new token before retrying."
                        ),
                    }
                },
            ) from exc
        if error_type != "resource_retiring":
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "resource_retiring",
                    "message": "This resource is retiring; registration is closed.",
                }
            },
        ) from exc
    return HostRegisterResponse(
        host=host["host"],
        credential=credential,
        status=host["status"],
        # Forced: enrolment is the one moment an operator is watching, and the
        # answer costs one request. Everything downstream of a wrong address
        # here is a job failing minutes later with `503` as its only clue.
        sandbox_manager_error=await _probed_sandbox_manager_error(
            host["host"], body.sandbox_manager_url, force=True
        ),
    )


@router.post("/heartbeat", response_model=HostHeartbeatResponse)
async def heartbeat_host(
    body: HostHeartbeatRequest,
    background_tasks: BackgroundTasks,
    host: dict[str, Any] = Depends(authenticate_host),
    store: _HostsStorage | None = Depends(get_storage),
    grants: Any = Depends(get_grant_client),
    # Annotated rather than a `Depends` default, so the handler called
    # directly, outside a request, is given no schedule and renews every grant.
    schedule: Annotated[RenewalSchedule | None, Depends(get_renewal_schedule)] = None,
) -> HostHeartbeatResponse:
    """Report liveness, and whatever about this machine has changed.

    Promotes ``enrolling`` and ``offline`` hosts to ``online``: a machine that
    comes back from a partition needs no operator to put it back to work.
    Deliberately does **not** promote a ``draining`` one — a heartbeat says "I
    am here", not "I should be taking work" — and a ``disabled`` host cannot
    get this far, because its credential no longer authenticates.
    """
    job_store = _require_store(store)
    updated = await job_store.host_pool.record_host_heartbeat(
        host=host["host"],
        credential_hash=host.get("credential_hash"),
        # An unusable block was already dropped by the request model, so this
        # is either a validated observation or nothing at all. The store
        # decides whether it is new enough to keep.
        capacity=None if body.capacity is None else body.capacity.model_dump(mode="json"),
        capacity_sample_age_ms=None if body.capacity is None else body.capacity.sample_age_ms,
        physical_host=body.physical_host,
        slots_total=body.slots_total,
        launch_error=body.launch_error,
        runner_version=body.runner_version,
        sandbox_manager_url=_checked_sandbox_manager_url(body.sandbox_manager_url),
        capabilities=body.capabilities,
        sandbox_runtime=body.sandbox_runtime,
        sandbox_image_digest=body.sandbox_image_digest,
        runtime_versions=body.runtime_versions,
        runtime_versions_available=body.runtime_versions_available,
        # Reduced to known settings and finite numbers before they are stored:
        # a host can be newer than this control plane, and JSON's Infinity is
        # not a number Postgres will keep.
        settings_applied=reported_values(body.settings_applied),
        settings_local=reported_values(body.settings_local),
        sandbox_archive_settings_applied_revision=(body.sandbox_archive_settings_applied_revision),
        sandbox_archive_settings_revision_reported=(
            "sandbox_archive_settings_applied_revision" in body.model_fields_set
        ),
        runtime_downloads=body.runtime_downloads,
    )
    if updated is None:
        # The row vanished, or its credential changed, between authentication
        # and the write — a forget, a removal or a re-enrollment landing
        # mid-request. Refusing is right: this credential now names nothing,
        # and the residencies below must not be extended on its word.
        raise _unauthorized("This host credential is no longer valid.")
    # Residency beats ride the host heartbeat (design §7): one wire message
    # says "I am here" and "these sandboxes are still mine", generation-scoped
    # so a beat from a taken-over residency extends nothing — it comes back in
    # residencies_lost and the host tears that sandbox down. The grants of
    # every residency this beat extends, once each, are renewed after the
    # reply: see ``_renew_residency_grants``.
    renewals: dict[str, None] = {}
    ttl_seconds = residency_ttl_s()
    lost = await _extend_residencies(
        job_store,
        updated["host"],
        [(beat.thread_id, beat.generation) for beat in body.residencies or []],
        ttl_seconds=ttl_seconds,
        renewals=renewals,
    )
    # Docker is the lifecycle authority for retained chat containers; the
    # database is the scheduling/fencing authority.  Heartbeats join those
    # views without treating absence as deletion. In particular, a sandbox manager's
    # settled idle sweep can stop Docker after the worker token that settled
    # the turn has expired, so host authentication is the durable path for
    # recording that STOPPED transition. Stopped containers stay listed until
    # their chats are deleted, and an observation that changes nothing writes
    # nothing (``reconcile_thread_sandbox_observations``).
    retained_rows = body.retained_sandboxes or []
    await job_store.sandbox.reconcile_thread_sandbox_observations(
        host=updated["host"],
        observations=[_sandbox_observation(retained) for retained in retained_rows],
    )
    await _record_checkpoints(job_store, updated["host"], retained_rows)
    revocations = await _release_stopped_residencies(
        job_store, updated["host"], retained_rows, grants
    )
    # Starting/restarting is a bounded launch window. Renew only a process that
    # has proved itself LIVE.
    live = [
        (member.thread_id, member.residency_generation)
        for retained in retained_rows
        if retained.running
        for member in retained.members
        if member.residency_generation > 0 and member.resident_state == "live"
    ]
    for beat in await _extend_residencies(
        job_store, updated["host"], live, ttl_seconds=ttl_seconds, renewals=renewals
    ):
        if beat not in lost:
            lost.append(beat)
    retained_stops = await _due_retained_stops(job_store, updated["host"], retained_rows)
    if revocations:
        background_tasks.add_task(revoke_detached_grants, grants, revocations)
    if renewals and grants is not None:
        background_tasks.add_task(_renew_residency_grants, grants, list(renewals), schedule)
    # The answer is made of the policy the beat's own write read, under the
    # same lock, rather than of four more reads of it. A part of it that did
    # not decode is None and is read again here, now that the beat's own work
    # is done and any agent update it completes has switched: that read fails
    # the answer with its error, as it did before the beat read the policy.
    policy = updated["runner_policy"]
    runtime_downloads = await _runtime_downloads(job_store, updated["host"], policy)
    host_settings = policy["host_settings"]
    if host_settings is None:
        host_settings = await job_store.host_pool.host_settings()
    archive = policy["sandbox_archive_settings"]
    if archive is None:
        archive = await job_store.host_pool.sandbox_archive_settings()
    return HostHeartbeatResponse(
        host=updated["host"],
        status=updated["status"],
        slots_total=updated["slots_total"],
        slots_used=updated["slots_used"],
        # The operator's requested limit rides the answer to the beat that
        # carried the host's current one: the host adopts it, and the next beat
        # reports it back as the limit it is now serving.
        slots_desired=updated.get("slots_desired"),
        # The whole set every time: this server's values over global values
        # and built-in defaults, so clearing an override never revives its .env.
        settings=merge_admin_values(host_settings, updated.get("settings_desired")),
        sandbox_archive_settings=_host_archive_settings(
            archive,
            updated.get("capabilities") or body.capabilities or [],
        ),
        runtime_downloads=runtime_downloads,
        residencies_lost=lost,
        retained_sandboxes_stop=retained_stops,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_S,
        # Rate-limited inside the probe. Heartbeats are this host's liveness
        # signal and must not come to depend on a second service answering, but
        # probing only at enrolment would never catch a credential that was
        # already wrong when the host enrolled — which is how the first remote
        # host was misconfigured, and it stayed invisible for a day.
        sandbox_manager_error=await _probed_sandbox_manager_error(
            updated["host"], body.sandbox_manager_url
        ),
    )


from cloud_agent.application.residency import residency_ttl_s


def _sandbox_observation(retained: RetainedSandboxBeat) -> dict[str, Any]:
    """What the store reconciles of one reported group, the host aside."""
    observation: dict[str, Any] = {
        "thread_id": retained.thread_id,
        "sandbox_id": retained.sandbox_id,
        "container_id": retained.container_id,
        "sandbox_generation": retained.generation,
        "running": retained.running,
        "activity_active": retained.activity_active,
        "idle_remaining_seconds": retained.idle_remaining_seconds,
        "members": [member.model_dump() for member in retained.members],
    }
    if retained.resource_usage is not None:
        observation["resource_usage"] = retained.resource_usage.model_dump(mode="json")
    if retained.terminal_count is not None:
        observation["terminal_count"] = retained.terminal_count
    return observation


async def _extend_residencies(
    store: _HostsStorage,
    host: str,
    residencies: list[tuple[str, int]],
    *,
    ttl_seconds: float,
    renewals: dict[str, None],
) -> list[ResidencyBeat]:
    """Extend these ``(thread_id, generation)`` residencies together; return those lost.

    One transaction for all of them, which raises rather than answer a
    database failure as "not extended". The grant of each one extended joins
    ``renewals``: the credential lives exactly as long as the residency, so the
    beat that extends the lease extends the grant. A lost one echoes its
    generation back (review finding 5): a beat carrying gen N can be answered
    after the host re-established gen N+1, and a thread-id-only loss would let
    the stale answer kill the successor. The host drops only on an exact
    generation match.
    """
    if not residencies:
        return []
    extended = await store.residency.heartbeat_residencies(
        residencies=residencies, host=host, ttl_seconds=ttl_seconds
    )
    lost: list[ResidencyBeat] = []
    for thread_id, generation in residencies:
        grant = extended.get((thread_id, generation))
        if grant:
            renewals[grant] = None
        elif grant is None:
            lost.append(ResidencyBeat(thread_id=thread_id, generation=generation))
    return lost


async def _release_stopped_residencies(
    store: _HostsStorage, host: str, retained_rows: list[RetainedSandboxBeat], grants: Any
) -> list[str]:
    """Release the residencies of members with no process to use them; return their grants.

    A stopped group has no process that can legitimately use a residency
    credential, nor a stopped or lost member of a running one. Release it
    instead of renewing it forever; the database fences this against
    concurrent starts. Most were released on an earlier beat, so one read
    finds the generations still open, and only those are offered. The caller
    revokes the returned grants after the reply (``revoke_detached_grants``).
    """
    quiescent = [
        (retained, member)
        for retained in retained_rows
        for member in retained.members
        if member.residency_generation > 0
        and (not retained.running or member.resident_state in {"stopped", "lost"})
    ]
    if not quiescent:
        return []
    still_open = await store.residency.open_shared_residencies(
        residencies=[
            (retained.sandbox_id, member.thread_id, member.residency_generation)
            for retained, member in quiescent
        ]
    )
    revocations: list[str] = []
    for retained, member in quiescent:
        if (retained.sandbox_id, member.thread_id, member.residency_generation) not in still_open:
            continue
        released = await store.residency.release_shared_residency_if_quiescent(
            thread_id=member.thread_id,
            generation=member.residency_generation,
            process_generation=member.process_generation,
            host=host,
            sandbox_id=retained.sandbox_id,
        )
        if released and grants is not None:
            revocations.append(released)
    return revocations


async def _due_retained_stops(
    store: _HostsStorage, host: str, retained_rows: list[RetainedSandboxBeat]
) -> list[RetainedSandboxStop]:
    """The exact stop fence of every running group whose stop the database says is due."""
    stops: list[RetainedSandboxStop] = []
    for retained in retained_rows:
        if not retained.running:
            continue
        due = await store.sandbox.due_thread_sandbox_stop(
            thread_id=retained.thread_id,
            host=host,
            sandbox_id=retained.sandbox_id,
            container_id=retained.container_id,
            sandbox_generation=retained.generation,
        )
        if due is not None:
            # Fence the stop to the activation this beat reported idle. The
            # sandbox manager refuses any other activation, so a database
            # placeholder made every control-plane stop of a used chat stale;
            # a newer activation still refuses this one.
            stops.append(
                RetainedSandboxStop.model_validate(
                    {**due, "activation_generation": retained.activation_generation}
                )
            )
    return stops


async def _runtime_downloads(
    store: _HostsStorage, host: str, policy: dict[str, Any]
) -> list[dict[str, str]]:
    """What the answer tells the host to hold, once any update this beat completes switches.

    This beat may carry the last report an agent update was waiting for, so
    the switch is tried whenever one may be waiting, including when the beat's
    read of the updates did not decode. The targets are read again after a
    switch, since hosts now hold what it made current, and when the beat's
    read of them did not decode, which then fails the answer.
    """
    targets = policy["runtime_download_targets"]
    switched = (
        []
        if policy["runtime_upgrade_waiting"] is False
        else await _switch_ready_runtime_upgrades(store, host)
    )
    if switched or targets is None:
        return await store.host_pool.runtime_download_targets()
    return list(targets)


async def _switch_ready_runtime_upgrades(store: _HostsStorage, host: str) -> list[str]:
    """Switch new chats to each update every online host holds; return the runtimes switched.

    Bookkeeping, so a failure here costs the host nothing but a later switch.
    """
    try:
        switched = await store.host_pool.activate_ready_runtime_upgrades()
    except Exception:
        logger.warning(
            "agent runtime upgrade activation failed",
            extra={"event": "agent_runtime_upgrade_activation_failed", "host": host},
            exc_info=True,
        )
        return []
    for runtime in switched:
        logger.info(
            "agent runtime upgrade activated",
            extra={"event": "agent_runtime_upgrade_activated", "runtime": runtime},
        )
    return switched


async def _record_checkpoints(
    store: _HostsStorage, host: str, retained_rows: list[RetainedSandboxBeat]
) -> None:
    """Record the checkpoint receipts a beat carries, the new ones only.

    A host repeats a group's latest receipt on every beat until the group is
    gone. Recording is idempotent and fenced to this host's current
    generation, but it takes the compute fence and the group's lock to find
    that out, so one read first sets aside every receipt already recorded. A
    bookkeeping failure must not cost the host its liveness beat.
    """
    receipts = [
        (retained, retained.checkpoint)
        for retained in retained_rows
        if retained.checkpoint is not None
    ]
    if not receipts:
        return
    try:
        recorded = await store.sandbox.recorded_sandbox_checkpoints(
            checkpoint_ids=[checkpoint.checkpoint_id for _, checkpoint in receipts]
        )
    except Exception:
        # Each is then offered as before: recording one twice changes nothing.
        logger.warning(
            "sandbox checkpoint receipts could not be looked up",
            extra={"event": "sandbox_checkpoint_lookup_failed", "host": host},
            exc_info=True,
        )
        recorded = set()
    for retained, checkpoint in receipts:
        if checkpoint.checkpoint_id in recorded:
            continue
        try:
            await store.sandbox.record_sandbox_checkpoint(
                host=host,
                thread_id=retained.thread_id,
                checkpoint=checkpoint.model_dump(mode="json"),
            )
        except Exception:
            logger.warning(
                "sandbox checkpoint receipt could not be recorded",
                extra={
                    "event": "sandbox_checkpoint_record_failed",
                    "host": host,
                    "sandbox_id": retained.sandbox_id,
                },
                exc_info=True,
            )


async def _renew_residency_grants(
    grants: Any, grant_ids: list[str], schedule: RenewalSchedule | None = None
) -> None:
    """Renew the grants a heartbeat extended, together, after its reply was sent.

    Each renewal may take the gateway's whole request timeout, and the host
    stops waiting for its heartbeat's answer at 15 s. Awaited one by one before
    the reply, two slow renewals were enough for the host never to receive the
    stops and lost residencies that answer carries. Best-effort, as before: a
    missed renewal starves hot turns into warm, never into an outage.

    Only the grants that are running low, though. Every beat extends every
    residency it names, and renewing each one's grant every thirty seconds
    asked the gateway ten times for every step it granted. The beat has just
    extended these residencies, which is all the store says about their grants,
    so the schedule decides only *when*: a grant whose last renewal has plenty
    left waits for a later beat. Without a schedule, every one is renewed.
    """

    async def renew(grant_id: str) -> None:
        if schedule is not None and not schedule.due(grant_id):
            return
        with contextlib.suppress(Exception):
            renewal = await grants.renew(grant_id)
            if schedule is not None:
                schedule.record(grant_id, renewal)

    await asyncio.gather(*(renew(grant_id) for grant_id in grant_ids))


def _host_archive_settings(
    values: dict[str, Any],
    capabilities: list[str],
) -> HostSandboxArchiveSettings:
    """Return the policy one host can apply.

    A host whose sandbox manager predates checkpoints rejects the unknown key,
    so an opt-out is reduced to the default (which serializes to nothing) for
    it. It receives the same revision and keeps archiving without checkpoints.
    """
    settings = HostSandboxArchiveSettings.model_validate(values)
    if SANDBOX_CHECKPOINT_CAPABILITY not in capabilities:
        settings = settings.model_copy(update={"checkpoints_enabled": settings.enabled})
    return settings


async def _probed_sandbox_manager_error(
    host: str, sandbox_manager_url: str | None, *, force: bool = False
) -> str | None:
    """Probe the reported sandbox manager, log a failure, and tell the host it failed.

    Two audiences, because they notice different things. The log is where an
    operator looks after something is already wrong, and it gets the full
    reason. The response is what the runner prints on the machine being
    deployed — which `sandbox_host.sh up` tails, so a misconfigured deploy says
    so while somebody is still watching it. It gets only
    :func:`~cloud_agent.sandbox_manager_routing.probe_error_for_host`: the host chose
    the address being dialled, so the dial's own error would tell it what this
    control plane can reach.

    Never raises and never refuses the registration. A sandbox manager that is slow to
    start is normal — compose brings it up beside the runner — and a control
    plane that rejected a host for it would turn a warning into an outage.
    """
    error = await probe_sandbox_manager(host, sandbox_manager_url, force=force)
    if error is not None:
        logger.warning(
            "agent_host_sandbox_manager_unreachable",
            extra={
                "event": "agent_host_sandbox_manager_unreachable",
                "host": host,
                "sandbox_manager_error": error,
            },
        )
    return probe_error_for_host(error)
