"""The two endpoints a host agent calls about itself: register and heartbeat.

Kept out of ``routes.py``. That file is the moved gateway router and is held to
a move-as-is diff; these are net-new phase-G surface, and mixing them in would
make the one file nobody should be editing the file everybody edits.

**Why enrollment exists at all.** Before it, a machine joined the pool by
polling with the dispatcher credential, and the pool list was therefore a list
of names anything holding that credential could invent — which #1158's own
module docstring says plainly, and accepts, because the switch it was built for
decides where *our* machines run *our* jobs. A pool that grows and shrinks
needs more than that: a host must be something the platform issued a credential
to, so that revoking it means something and so that a heartbeat is evidence
rather than an assertion.

The credential is deliberately **not** the dispatcher token. That token opens
the claim door for every host; a host credential opens it for exactly one, and
carries the machine's identity with it, so a compromised host cannot claim as
its neighbour.

Registration is unauthenticated *except* for the enrollment token, and that is
the point: a machine being set up has no credential yet. The token is
one-time, short-lived, hashed at rest, and **names the host it may register**,
so the window it opens is one machine, under one name, once, for fifteen
minutes.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException

from cloud_agent.api.deps import get_agent_job_store, get_grant_client
from cloud_agent.broker_routing import BrokerUrlRefused, probe_broker, validate_broker_url
from cloud_agent.host_pool import (
    HEARTBEAT_INTERVAL_S,
    hash_secret,
)
from cloud_agent.schemas import (
    HostHeartbeatRequest,
    HostHeartbeatResponse,
    HostRegisterRequest,
    HostRegisterResponse,
    HostResidencyEstablishRequest,
    HostResidencyEstablishResponse,
    HostResidencyReleaseRequest,
    ResidencyBeat,
    WorkerAckResponse,
)
from cloud_agent.tokens import InvalidHostToken, mint_host_token, parse_host_token

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from cloud_agent.storage.store import AgentJobStore

router = APIRouter(prefix="/v1/agent/host", tags=["agent-hosts"])

#: How long a residency's lease lives between beats (design §7) — and, since
#: the lease and the thread-attributed grant are renewed by the same beat and
#: revoked together (design §14), the cap on how long an idle resident
#: process keeps a live credential after its host goes quiet.
RESIDENCY_TTL_S = float(os.environ.get("AGENT_RESIDENCY_TTL_S") or 1800.0)


def _require_store(store: AgentJobStore | None) -> AgentJobStore:
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
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
    host = await job_store.authenticate_host(
        host=claims["host"], credential_hash=hash_secret(token)
    )
    if host is None:
        raise _unauthorized("This host credential is no longer valid.")
    return host


def _checked_broker_url(url: str | None) -> str | None:
    """Refuse a broker address this deployment will not send its token to.

    The control plane sends the pool's shared workspace-broker token to
    whatever address a host reports, so accepting one is handing over a
    credential good for every broker in the pool. Refused with a 400 rather
    than dropped: a host whose address was silently ignored looks enrolled and
    then never serves a workspace.
    """
    try:
        return validate_broker_url(url)
    except BrokerUrlRefused as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_broker_url", "message": str(exc)}},
        ) from exc


@router.post("/register", response_model=HostRegisterResponse)
async def register_host(
    body: HostRegisterRequest,
    store: AgentJobStore | None = Depends(get_agent_job_store),
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
    if not await job_store.consume_enroll_token(
        token_hash=hash_secret(body.enroll_token), host=body.name
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
    host = await job_store.register_host(
        host=body.name,
        credential_hash=hash_secret(credential),
        capabilities=body.capabilities,
        slots_total=body.slots_total,
        broker_url=_checked_broker_url(body.broker_url),
        runner_version=body.runner_version,
        labels=dict(body.labels),
    )
    return HostRegisterResponse(
        host=host["host"],
        credential=credential,
        status=host["status"],
        # Forced: enrolment is the one moment an operator is watching, and the
        # answer costs one request. Everything downstream of a wrong address
        # here is a job failing minutes later with `503` as its only clue.
        broker_error=await _probed_broker_error(host["host"], body.broker_url, force=True),
    )


@router.post("/heartbeat", response_model=HostHeartbeatResponse)
async def heartbeat_host(
    body: HostHeartbeatRequest,
    host: dict[str, Any] = Depends(authenticate_host),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    grants: Any = Depends(get_grant_client),
) -> HostHeartbeatResponse:
    """Report liveness, and whatever about this machine has changed.

    Promotes ``enrolling`` and ``offline`` hosts to ``online``: a machine that
    comes back from a partition needs no operator to put it back to work.
    Deliberately does **not** promote a ``draining`` one — a heartbeat says "I
    am here", not "I should be taking work" — and a ``disabled`` host cannot
    get this far, because its credential no longer authenticates.
    """
    job_store = _require_store(store)
    updated = await job_store.record_host_heartbeat(
        host=host["host"],
        slots_total=body.slots_total,
        runner_version=body.runner_version,
        broker_url=_checked_broker_url(body.broker_url),
        capabilities=body.capabilities,
        sandbox_runtime=body.sandbox_runtime,
        sandbox_image_digest=body.sandbox_image_digest,
        runtime_versions=body.runtime_versions,
    )
    if updated is None:
        # The row vanished between authentication and the write — a forget
        # landing mid-request. Refusing is right: this credential now names
        # nothing.
        raise _unauthorized("This host credential is no longer valid.")
    # Residency beats ride the host heartbeat (design §7): one wire message
    # says "I am here" and "these sandboxes are still mine", generation-scoped
    # so a beat from a taken-over residency extends nothing — it comes back in
    # residencies_lost and the host tears that sandbox down.
    lost: list[ResidencyBeat] = []
    for beat in body.residencies or []:
        extended = await job_store.heartbeat_residency(
            thread_id=beat.thread_id,
            generation=beat.generation,
            host=updated["host"],
            ttl_seconds=RESIDENCY_TTL_S,
        )
        if extended and grants is not None:
            # The credential lives exactly as long as the residency: the beat
            # that extends the lease extends the grant. Best-effort — a missed
            # renewal starves hot turns into warm, never into an outage.
            with contextlib.suppress(Exception):
                await grants.renew(extended)
        if extended is None:
            # Echo the generation back (review finding 5): a beat carrying
            # gen N can be answered after the host re-established gen N+1,
            # and a thread-id-only loss would let the stale answer kill the
            # successor. The host drops only on an exact generation match.
            lost.append(beat)
    return HostHeartbeatResponse(
        host=updated["host"],
        status=updated["status"],
        slots_total=updated["slots_total"],
        slots_used=updated["slots_used"],
        residencies_lost=lost,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_S,
        # Rate-limited inside the probe. Heartbeats are this host's liveness
        # signal and must not come to depend on a second service answering, but
        # probing only at enrolment would never catch a credential that was
        # already wrong when the host enrolled — which is how the first remote
        # host was misconfigured, and it stayed invisible for a day.
        broker_error=await _probed_broker_error(updated["host"], body.broker_url),
    )


@router.post("/residencies", response_model=HostResidencyEstablishResponse)
async def establish_thread_residency(
    body: HostResidencyEstablishRequest,
    host: dict[str, Any] = Depends(authenticate_host),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    grants: Any = Depends(get_grant_client),
) -> HostResidencyEstablishResponse:
    """Take (or take over) residency for a thread this host is running.

    Residency is earned, not asserted: the host must hold the thread's
    *running* attempt right now, which is why the runner establishes at turn
    end, before finish. Without the gate, a compromised machine could pull
    other machines' conversations toward itself through claim affinity.
    """
    job_store = _require_store(store)
    row = await job_store.establish_residency(
        thread_id=body.thread_id,
        host=host["host"],
        runtime_id=body.runtime_id,
        runtime_version=body.runtime_version,
        sandbox_id=body.sandbox_id,
        ttl_seconds=RESIDENCY_TTL_S,
    )
    if row is None:
        # The gate lives inside the write's own transaction (store): running
        # attempt, this host, lease unexpired — checked under a shared lock,
        # so this answer cannot be stale by the time the row would exist.
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "not_running_thread",
                    "message": "residency belongs to the host running the thread's turn",
                }
            },
        )
    # A takeover is a credential rotation (design §14): the predecessor's
    # grant dies now, not at its TTL, so a zombie host starves promptly.
    previous_grant = row.get("previous_grant_id")
    if previous_grant and grants is not None:
        await grants.revoke(previous_grant)
    credential = await _mint_residency_grant(job_store, grants, row=row)
    return HostResidencyEstablishResponse(
        thread_id=row["thread_id"],
        generation=row["generation"],
        ttl_seconds=RESIDENCY_TTL_S,
        credential=credential,
        model=row.get("model"),
    )


async def _mint_residency_grant(
    job_store: AgentJobStore, grants: Any, *, row: dict[str, Any]
) -> str | None:
    """The residency's inference credential, or ``None`` for workspace-only.

    Attribution is the *thread* (design §14): the grant's external job id
    names it, so the gateway's ledger bills every resident turn to the
    conversation rather than to whichever turn happened to establish. Every
    failure degrades rather than refuses — a residency without a credential
    still keeps the workspace, and turns ride warm_native on their own
    per-attempt grants.

    A grant that cannot be linked is revoked, not handed over: the link is
    the only thing that will ever renew or revoke it, and the one way linking
    fails is a takeover between mint and link — whose new owner must not be
    starved by an orphan credential it cannot see.
    """
    if grants is None:
        return None
    try:
        grant = await grants.mint(
            user_id=row["user_id"],
            job_id=f"thread:{row['thread_id']}",
            attempt_id=str(row["establishing_attempt_id"]),
            models=[row["model"]],
            ttl_seconds=int(RESIDENCY_TTL_S),
        )
    except Exception:  # degrade to workspace-only residency
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
    if not grant_id or not token:
        return None
    linked = await job_store.link_residency_grant(
        thread_id=row["thread_id"], generation=row["generation"], grant_id=grant_id
    )
    if not linked:
        await grants.revoke(grant_id)
        return None
    return token


@router.post("/residencies/release", response_model=WorkerAckResponse)
async def release_thread_residency(
    body: HostResidencyReleaseRequest,
    host: dict[str, Any] = Depends(authenticate_host),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    grants: Any = Depends(get_grant_client),
) -> WorkerAckResponse:
    """End a residency this host holds (teardown, eviction, drain).

    Generation-scoped like every other residency write: releasing with a
    stale generation is a no-op ``ok=false``, never an error — the successor
    it would otherwise end belongs to someone else now.
    """
    job_store = _require_store(store)
    released = await job_store.release_residency(
        thread_id=body.thread_id, generation=body.generation, host=host["host"]
    )
    if released and grants is not None:
        await grants.revoke(released)
    return WorkerAckResponse(ok=released is not None)


async def _probed_broker_error(
    host: str, broker_url: str | None, *, force: bool = False
) -> str | None:
    """Probe the reported broker, log a failure, and hand it back to the host.

    Two audiences, because they notice different things. The log is where an
    operator looks after something is already wrong. The response is what the
    runner prints on the machine being deployed — which `sandbox_host.sh
    up` tails, so a misconfigured deploy says so while somebody is still
    watching it.

    Never raises and never refuses the registration. A broker that is slow to
    start is normal — compose brings it up beside the runner — and a control
    plane that rejected a host for it would turn a warning into an outage.
    """
    error = await probe_broker(host, broker_url, force=force)
    if error is not None:
        logger.warning(
            "agent_host_broker_unreachable",
            extra={
                "event": "agent_host_broker_unreachable",
                "host": host,
                "broker_error": error,
            },
        )
    return error
