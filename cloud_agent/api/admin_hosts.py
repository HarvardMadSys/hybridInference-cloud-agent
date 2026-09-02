"""Admin endpoints for the agent runner host pool.

Runners pull work, so there is no gateway-side dial that points jobs at a
machine — the only moment the platform gets to decide "not you" is the claim.
These endpoints therefore do not move anything: they name the host that may
materialize new chat sandboxes. Existing retained groups remain claimable only
on their immutable owning host.

A host joins by redeeming a one-time enrollment token. Its resulting credential
names exactly one machine and is required for registration, heartbeat, and
claim: a compromised host cannot claim as a neighbour, and revoking it removes
exactly one machine's access. Request-body host labels and pool-wide claim
credentials are not accepted.

The pin decides *which* enrolled host may claim a chat with no sandbox; the pool
lifecycle (drain, undrain, remove) decides whether a host is allowed to claim at
all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from cloud_agent.api.deps import (
    get_agent_job_store,
    get_client_ip,
    log_admin_action,
    verify_admin_access,
)
from cloud_agent.host_pool import (
    HOST_DRAINING,
    HOST_ONLINE,
    hash_secret,
    new_enroll_token,
)
from cloud_agent.schemas import (
    AgentRunnerHost,
    IssueHostEnrollTokenRequest,
    IssueHostEnrollTokenResponse,
    ListAgentRunnerHostsResponse,
    SetActiveAgentRunnerHostRequest,
    SetRuntimeVersionRequest,
)

if TYPE_CHECKING:
    from cloud_agent.storage.store import AgentJobStore

router = APIRouter(prefix="/admin")


def _require_store(store: AgentJobStore | None) -> AgentJobStore:
    """Return the store or 503 — agent jobs are an optional deployment part."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Agent jobs are not configured for this deployment.",
        )
    return store


def _to_item(row: dict) -> AgentRunnerHost:
    last_seen: datetime = row["last_seen_at"]
    now = datetime.now(UTC)
    # The column is TIMESTAMPTZ, so asyncpg hands back an aware datetime; a
    # naive one would only come from a hand-built row in a test.
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return AgentRunnerHost(
        host=row["host"],
        active=bool(row["is_active"]),
        last_worker_id=row.get("last_worker_id"),
        first_seen_at=row["first_seen_at"],
        last_seen_at=last_seen,
        seconds_since_seen=max(0.0, (now - last_seen).total_seconds()),
        # Read with ``.get`` and defaulted, not indexed. A row predating the
        # pool columns — and any caller that builds one by hand — must render,
        # because the alternative is that the page an operator uses to fix a
        # bad host is the page that 500s on it.
        status=row.get("status") or HOST_ONLINE,
        enrolled=bool(row.get("enrolled")),
        labels=row.get("labels") or {},
        capabilities=row.get("capabilities") or [],
        slots_total=row.get("slots_total"),
        slots_used=row.get("slots_used") or 0,
        runner_version=row.get("runner_version"),
        broker_url=row.get("broker_url"),
        last_heartbeat_at=row.get("last_heartbeat_at"),
        sandbox_runtime=row.get("sandbox_runtime"),
        sandbox_image_digest=row.get("sandbox_image_digest"),
        runtime_versions=row.get("runtime_versions"),
        runtime_versions_available=row.get("runtime_versions_available"),
    )


async def _snapshot(store: AgentJobStore) -> ListAgentRunnerHostsResponse:
    """The pool as it stands — the response every endpoint here returns.

    Every part comes from one read. ``active_host`` is the policy's own value
    rather than something derived from the list: where the two could disagree —
    a pinned host deleted out from under the policy — the honest answer is the
    name the claim gate is actually enforcing, not "unpinned" while the queue
    sits still. ``runtime_versions`` is the same fact about versions, and is
    read in the same transaction as the menus it must be displayed against.
    """
    rows, active, selected_versions = await store.runner_pool()
    return ListAgentRunnerHostsResponse(
        hosts=[_to_item(row) for row in rows],
        active_host=active,
        runtime_versions=selected_versions,
    )


@router.get("/agent/runner-hosts", response_model=ListAgentRunnerHostsResponse)
async def list_agent_runner_hosts(
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ListAgentRunnerHostsResponse:
    """List every machine that has polled for agent work, and the pinned one."""
    return await _snapshot(_require_store(store))


@router.put("/agent/runner-hosts/active", response_model=ListAgentRunnerHostsResponse)
async def set_active_agent_runner_host(
    request: Request,
    payload: SetActiveAgentRunnerHostRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ListAgentRunnerHostsResponse:
    """Pin new chat placement to one host, or let any host materialize it.

    Non-preemptive, and worth being precise about: the new host starts claiming
    new chats at once, while jobs already running on the old one keep running to
    the end. Existing retained chats also remain hard-pinned to their owning
    host for later follow-ups because their Docker writable layer is not
    portable in this phase. The two hosts can therefore overlap. This is not a
    drain, which would mean waiting for the old host to empty before the new one
    starts.
    """
    job_store = _require_store(store)
    previous = await job_store.active_runner_host()

    if not await job_store.set_active_runner_host(host=payload.host):
        # Pinning to a machine that has never polled parks the queue on a host
        # that may not exist, and it presents as "every job hangs" with nothing
        # in the logs to say why.
        raise HTTPException(
            status_code=404,
            detail=(
                f"No runner on {payload.host!r} has ever polled. Start one there "
                "(deploy/host/sandbox_host.sh up) and it will appear here."
            ),
        )

    await log_admin_action(
        job_store,
        get_client_ip(request),
        "agent.runner_host.set_active",
        None,
        {"old_host": previous, "new_host": payload.host},
    )
    return await _snapshot(job_store)


@router.put("/agent/runtime-versions", response_model=ListAgentRunnerHostsResponse)
async def set_agent_runtime_version(
    request: Request,
    payload: SetRuntimeVersionRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ListAgentRunnerHostsResponse:
    """Choose the agent CLI version this deployment runs for one runtime.

    Bounded by what the pool's images actually contain: build time decides the
    menu, this decides which item on it is active. A version no host offers is
    refused rather than stored, because the claim would fall back to the
    default and the page would then display a version that never runs.

    Non-retroactive, like the host pin: a chat's native CLI state is owned by
    the exact runtime version in its retained container. This applies to new
    groups; existing conversations keep the version they began on.
    """
    job_store = _require_store(store)
    _, _, previous = await job_store.runner_pool()

    if not await job_store.set_runtime_version(runtime=payload.runtime, version=payload.version):
        raise HTTPException(
            status_code=404,
            detail=(
                f"No host in the pool offers {payload.runtime} {payload.version!r}. "
                "A version has to be built into the sandbox image before it can be "
                "selected — add it to that host's *_EXTRA_VERSIONS and redeploy."
            ),
        )

    await log_admin_action(
        job_store,
        get_client_ip(request),
        "agent.runtime_version.set",
        None,
        {
            "runtime": payload.runtime,
            "old_version": previous.get(payload.runtime),
            "new_version": payload.version,
        },
    )
    return await _snapshot(job_store)


@router.delete("/agent/runner-hosts/{host}", response_model=ListAgentRunnerHostsResponse)
async def forget_agent_runner_host(
    request: Request,
    host: str,
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ListAgentRunnerHostsResponse:
    """Drop a decommissioned host from the list.

    Housekeeping only: a runner still polling on that machine re-adds itself
    within seconds. The active host cannot be dropped, because doing so would
    silently unpin new-chat placement, which is the opposite of what removing a
    host from a list looks like it does. Existing retained owners are not moved
    by forgetting their pool row; their broker routing continues to fail closed
    until the same named host is enrolled again. The active-host refusal is
    decided inside the store's transaction, not by a check here: a check here
    is a read a concurrent switch can outrun.
    """
    job_store = _require_store(store)
    outcome = await job_store.forget_runner_host(host=host)
    if outcome == "active":
        raise HTTPException(
            status_code=409,
            detail=(
                f"{host!r} is the active host. Switch to another host (or unpin) "
                "before removing it."
            ),
        )
    if outcome == "unknown":
        raise HTTPException(status_code=404, detail=f"Unknown runner host: {host}")

    await log_admin_action(
        job_store,
        get_client_ip(request),
        "agent.runner_host.forget",
        None,
        {"host": host},
    )
    return await _snapshot(job_store)


# ── Pool lifecycle (phase G) ───────────────────────────────────────────


@router.post("/agent/runner-hosts/enroll-token", response_model=IssueHostEnrollTokenResponse)
async def issue_host_enroll_token(
    request: Request,
    payload: IssueHostEnrollTokenRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> IssueHostEnrollTokenResponse:
    """Mint a one-time token that lets one named machine join the pool.

    Returned in full here and **nowhere else** — only its hash is stored, so
    there is no endpoint that can show it again and no database read that can
    recover it. An operator who loses it issues another.

    **The token names its host, and the caller here is the one who names it.**
    It did not, once, and the name was therefore whatever the registering
    machine typed — while registration upserts, so a name that already belongs
    to a live host revokes that host's credential and drops it back to
    ``enrolling``. A mistyped name evicted a working, busy machine from the
    claim pool. Refusing names already in use was the other candidate, but it
    breaks the case the upsert exists for (a rebuilt machine coming back under
    its own name) and leaves the naming decision on the unauthenticated side
    anyway. Registration always needs a freshly minted token, so binding the
    name costs the operator nothing and moves the decision to the party this
    endpoint has already authenticated.

    The TTL stays short for the reason it always was: the window is one
    machine, once, for as long as it takes to finish a setup already under way.
    """
    job_store = _require_store(store)
    body = payload
    # A body is now required: it carries the host name, and there is no
    # sensible default for "which machine may this enrol".
    token = new_enroll_token()
    issued = await job_store.issue_enroll_token(
        token_hash=hash_secret(token),
        host=body.host,
        ttl_seconds=body.ttl_seconds,
        issued_by=_admin_id,
    )
    await log_admin_action(
        job_store,
        get_client_ip(request),
        "agent.runner_host.enroll_token_issued",
        None,
        # The hash, never the token: this trail is read by more people than
        # hold the credential it would otherwise contain. The host name is in
        # it deliberately — "who authorized this name" is the question the
        # binding exists to answer.
        {
            "host": body.host,
            "expires_at": issued["expires_at"].isoformat(),
            "ttl_seconds": body.ttl_seconds,
        },
    )
    return IssueHostEnrollTokenResponse(
        enroll_token=token, host=body.host, expires_at=issued["expires_at"]
    )


async def _set_status(
    request: Request,
    *,
    host: str,
    status: str,
    action: str,
    job_store: AgentJobStore,
) -> ListAgentRunnerHostsResponse:
    """Apply one table-driven status change, or say why it was refused."""
    outcome = await job_store.set_host_status(host=host, status=status)
    if outcome == "unknown":
        raise HTTPException(status_code=404, detail=f"Unknown runner host: {host}")
    if outcome == "invalid":
        # 409, not 400: the request is well-formed and would have been legal
        # from another state. Naming the current state is what tells an
        # operator whether to wait or to do something else.
        current = await job_store.get_host(host=host)
        raise HTTPException(
            status_code=409,
            detail=(
                f"{host!r} is {current['status'] if current else 'unknown'!r} and cannot "
                f"become {status!r}."
            ),
        )
    await log_admin_action(job_store, get_client_ip(request), action, None, {"host": host})
    return await _snapshot(job_store)


@router.post("/agent/runner-hosts/{host}/drain", response_model=ListAgentRunnerHostsResponse)
async def drain_agent_runner_host(
    request: Request,
    host: str,
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ListAgentRunnerHostsResponse:
    """Stop this host taking new work; let what it has finish.

    The counterpart to the pin, and the thing the pin is not: switching moves
    new claims elsewhere *and* leaves the old host running its jobs with no way
    to tell when it is empty. Draining is the same first half plus a slot
    counter that walks down to zero, which is the number ``remove`` refuses to
    act before.
    """
    return await _set_status(
        request,
        host=host,
        status=HOST_DRAINING,
        action="agent.runner_host.drain",
        job_store=_require_store(store),
    )


@router.post("/agent/runner-hosts/{host}/undrain", response_model=ListAgentRunnerHostsResponse)
async def undrain_agent_runner_host(
    request: Request,
    host: str,
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ListAgentRunnerHostsResponse:
    """Put a drained host back to work.

    Exists because a drain that cannot be undone is a decision an operator has
    to be sure about before making, and the whole value of draining is that it
    is the safe thing to try first.
    """
    return await _set_status(
        request,
        host=host,
        status=HOST_ONLINE,
        action="agent.runner_host.undrain",
        job_store=_require_store(store),
    )


@router.post("/agent/runner-hosts/{host}/remove", response_model=ListAgentRunnerHostsResponse)
async def remove_agent_runner_host(
    request: Request,
    host: str,
    force: bool = Query(
        False,
        description=(
            "Remove a host that is still running attempts. Those attempts are "
            "not stopped — nothing here can reach them — they are left to their "
            "leases. An indeterminate retained-chat activation is marked LOST "
            "instead of replayed."
        ),
    ),
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ListAgentRunnerHostsResponse:
    """Revoke this host's credential and take it out of the pool.

    Refused while the host still holds slots, unless forced. The refusal is not
    bureaucracy: the running attempts belong to a machine the platform has just
    declared gone, and nobody would learn that until their leases lapsed
    minutes later. Being told "it is still busy" is the difference between
    draining first and finding out afterwards.

    Forced removal is the deliberate version of the same thing. Lease fencing
    rejects later writes from the superseded worker. A retained activation is
    not replayed because the platform cannot know whether its prompt ran, so
    that chat is marked LOST when the lease expires.

    The row survives, disabled. ``agent_attempts.host_id`` points at it, and
    "where did this job run" should not become unanswerable because the machine
    was retired. ``DELETE`` forgets that administrative row; neither action is
    evidence that the retained container or Docker data-root was deleted.
    """
    job_store = _require_store(store)
    outcome = await job_store.remove_host(host=host, force=force)
    if outcome == "unknown":
        raise HTTPException(status_code=404, detail=f"Unknown runner host: {host}")
    if outcome == "busy":
        raise HTTPException(
            status_code=409,
            detail=(
                f"{host!r} is still running attempts. Drain it first, or repeat with "
                "force=true to let their leases expire under the retained-context "
                "fail-closed policy."
            ),
        )
    await log_admin_action(
        job_store,
        get_client_ip(request),
        "agent.runner_host.remove",
        None,
        {"host": host, "force": force},
    )
    return await _snapshot(job_store)
