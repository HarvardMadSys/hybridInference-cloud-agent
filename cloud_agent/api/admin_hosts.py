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
all; the requested slot count decides how many turns it runs at once, which
the host applies to itself on its next heartbeat.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    log_admin_action,
    verify_admin_access,
)
from cloud_agent.capacity_protocol import (
    DEFAULT_HISTORY_RANGE,
    HISTORY_RANGES,
    AgentHostCapacityHistoryResponse,
    admin_capacity_view,
    history_point_grid,
    resolve_history_range,
)
from cloud_agent.execution_profiles import default_execution_profile
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
    SetHostSlotsRequest,
    SetRuntimeVersionRequest,
)

if TYPE_CHECKING:
    from cloud_agent.ports import (
        HostCapacityRepository,
        HostPoolRepository,
    )


class _AdminHostsStorage(Protocol):
    """The capabilities this module's handlers reach the store for.

    Named here so that reaching for one more is a deliberate edit to
    this list rather than a call that happens to type-check.
    """

    host_capacity: HostCapacityRepository
    host_pool: HostPoolRepository


def _no_store(response: Response) -> None:
    """Keep an operator's view of the pool out of every cache on the way.

    These responses carry live placement and load for the whole fleet, and a
    stale one is worse than a slow one: the numbers look current and are not.
    """
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/admin", dependencies=[Depends(_no_store)])


def _require_store(store: _AdminHostsStorage | None) -> _AdminHostsStorage:
    """Return the store or 503 — agent jobs are an optional deployment part."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Agent jobs are not configured for this deployment.",
        )
    return store


def _to_item(row: dict[str, Any], *, now: datetime | None = None) -> AgentRunnerHost:
    last_seen: datetime = row["last_seen_at"]
    read_at = datetime.now(UTC) if now is None else now
    # The column is TIMESTAMPTZ, so asyncpg hands back an aware datetime; a
    # naive one would only come from a hand-built row in a test.
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    epoch = row.get("capacity_epoch")
    return AgentRunnerHost(
        host=row["host"],
        active=bool(row["is_active"]),
        last_worker_id=row.get("last_worker_id"),
        first_seen_at=row["first_seen_at"],
        last_seen_at=last_seen,
        seconds_since_seen=max(0.0, (read_at - last_seen).total_seconds()),
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
        slots_desired=row.get("slots_desired"),
        launch_error=row.get("launch_error"),
        runner_version=row.get("runner_version"),
        broker_url=row.get("broker_url"),
        last_heartbeat_at=row.get("last_heartbeat_at"),
        sandbox_runtime=row.get("sandbox_runtime"),
        sandbox_image_digest=row.get("sandbox_image_digest"),
        runtime_versions=row.get("runtime_versions"),
        runtime_versions_available=row.get("runtime_versions_available"),
        capacity_epoch=None if epoch is None else str(epoch),
        # The stored block is re-validated rather than trusted: one corrupt
        # document costs that host its capacity numbers, not the whole page.
        capacity=admin_capacity_view(
            snapshot=row.get("capacity_snapshot"),
            received_at=row.get("capacity_received_at"),
            sampled_at=row.get("capacity_sampled_at"),
            now=read_at,
        ),
    )


async def _snapshot(store: HostPoolRepository) -> ListAgentRunnerHostsResponse:
    """The pool as it stands — the response every endpoint here returns.

    Every part comes from one read. ``active_host`` is the policy's own value
    rather than something derived from the list: where the two could disagree —
    a pinned host deleted out from under the policy — the honest answer is the
    name the claim gate is actually enforcing, not "unpinned" while the queue
    sits still. ``runtime_versions`` is the same fact about versions, and is
    read in the same transaction as the menus it must be displayed against.

    ``observed_at`` is that transaction's own instant. Every age on the page is
    measured from it rather than from whenever the response happened to be
    serialized, so a slow request cannot make the whole fleet look older than
    it is.
    """
    rows, active, selected_versions, observed_at = await store.runner_pool()
    return ListAgentRunnerHostsResponse(
        execution_profile=default_execution_profile().value,
        hosts=[_to_item(row, now=observed_at) for row in rows],
        active_host=active,
        runtime_versions=selected_versions,
        observed_at=observed_at,
    )


@router.get("/agent/runner-hosts", response_model=ListAgentRunnerHostsResponse)
async def list_agent_runner_hosts(
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
) -> ListAgentRunnerHostsResponse:
    """List every machine that has polled for agent work, and the pinned one."""
    return await _snapshot(_require_store(store).host_pool)


@router.put("/agent/runner-hosts/active", response_model=ListAgentRunnerHostsResponse)
async def set_active_agent_runner_host(
    request: Request,
    payload: SetActiveAgentRunnerHostRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
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
    previous = await job_store.host_pool.active_runner_host()

    if not await job_store.host_pool.set_active_runner_host(host=payload.host):
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
        actor_id=_admin_id,
    )
    return await _snapshot(job_store.host_pool)


@router.put("/agent/runtime-versions", response_model=ListAgentRunnerHostsResponse)
async def set_agent_runtime_version(
    request: Request,
    payload: SetRuntimeVersionRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
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
    _, _, previous, _ = await job_store.host_pool.runner_pool()

    if not await job_store.host_pool.set_runtime_version(
        runtime=payload.runtime, version=payload.version
    ):
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
        actor_id=_admin_id,
    )
    return await _snapshot(job_store.host_pool)


@router.delete("/agent/runner-hosts/{host}", response_model=ListAgentRunnerHostsResponse)
async def forget_agent_runner_host(
    request: Request,
    host: str,
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
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
    outcome = await job_store.host_pool.forget_runner_host(host=host)
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
        actor_id=_admin_id,
    )
    return await _snapshot(job_store.host_pool)


# ── Pool lifecycle (phase G) ───────────────────────────────────────────


@router.post("/agent/runner-hosts/enroll-token", response_model=IssueHostEnrollTokenResponse)
async def issue_host_enroll_token(
    request: Request,
    payload: IssueHostEnrollTokenRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
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
    issued = await job_store.host_pool.issue_enroll_token(
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
        actor_id=_admin_id,
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
    job_store: _AdminHostsStorage,
    actor_id: str,
) -> ListAgentRunnerHostsResponse:
    """Apply one table-driven status change, or say why it was refused."""
    outcome = await job_store.host_pool.set_host_status(host=host, status=status)
    if outcome == "unknown":
        raise HTTPException(status_code=404, detail=f"Unknown runner host: {host}")
    if outcome == "invalid":
        # 409, not 400: the request is well-formed and would have been legal
        # from another state. Naming the current state is what tells an
        # operator whether to wait or to do something else.
        current = await job_store.host_pool.get_host(host=host)
        raise HTTPException(
            status_code=409,
            detail=(
                f"{host!r} is {current['status'] if current else 'unknown'!r} and cannot "
                f"become {status!r}."
            ),
        )
    await log_admin_action(
        job_store, get_client_ip(request), action, None, {"host": host}, actor_id=actor_id
    )
    return await _snapshot(job_store.host_pool)


@router.post("/agent/runner-hosts/{host}/drain", response_model=ListAgentRunnerHostsResponse)
async def drain_agent_runner_host(
    request: Request,
    host: str,
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
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
        actor_id=_admin_id,
    )


@router.post("/agent/runner-hosts/{host}/undrain", response_model=ListAgentRunnerHostsResponse)
async def undrain_agent_runner_host(
    request: Request,
    host: str,
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
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
        actor_id=_admin_id,
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
    store: _AdminHostsStorage | None = Depends(get_storage),
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
    outcome = await job_store.host_pool.remove_host(host=host, force=force)
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
        actor_id=_admin_id,
    )
    return await _snapshot(job_store.host_pool)


@router.put("/agent/runner-hosts/{host}/slots", response_model=ListAgentRunnerHostsResponse)
async def set_agent_runner_host_slots(
    request: Request,
    host: str,
    payload: SetHostSlotsRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
) -> ListAgentRunnerHostsResponse:
    """Ask one host to run this many turns at once, without touching its deployment.

    The host runs one long-lived service and starts a process per turn as it
    claims one, so this is a limit rather than a fleet size: nothing is started
    or stopped when it changes. The host reads it off its next heartbeat answer
    and serves it from then on, reporting it back as ``slots_total``.

    The claim admits against the lower of the requested and reported numbers.
    Lowering therefore takes effect immediately, before the host has even heard
    about it, and turns already running are untouched — they finish, and the
    host simply claims nothing more until it is under the new limit. Raising
    takes effect when the host confirms, which is one heartbeat away.

    **Saving is not applying.** This stores the number and says so; the page
    shows whether the host has confirmed it, and says when a host is offline or
    when its runner predates remote sizing and needs a redeploy to change.
    ``null`` returns the host to its own ``AGENT_HOST_SLOTS`` default.
    """
    job_store = _require_store(store)
    before = await job_store.host_pool.get_host(host=host)
    if before is None:
        raise HTTPException(status_code=404, detail=f"Unknown runner host: {host}")

    outcome = await job_store.host_pool.set_host_slots_desired(host=host, slots=payload.slots)
    if outcome == "unknown":
        # Forgotten between the read above and the write: a name that no
        # longer exists cannot be sized, and saying so beats a snapshot that
        # silently omits it.
        raise HTTPException(status_code=404, detail=f"Unknown runner host: {host}")

    await log_admin_action(
        job_store,
        get_client_ip(request),
        "agent.runner_host.set_slots",
        None,
        {
            "host": host,
            "old_slots": before.get("slots_desired"),
            "new_slots": payload.slots,
            "reported": before.get("slots_total"),
        },
        actor_id=_admin_id,
    )
    return await _snapshot(job_store.host_pool)


# ── Capacity history ───────────────────────────────────────────────────


@router.get(
    "/agent/runner-hosts/{host}/capacity-history",
    response_model=AgentHostCapacityHistoryResponse,
)
async def get_agent_runner_host_capacity_history(
    host: str,
    window: str = Query(
        DEFAULT_HISTORY_RANGE,
        alias="range",
        description="How far back to read: 1h, 6h or 24h. Anything else is refused.",
    ),
    _admin_id: str = Depends(verify_admin_access),
    store: _AdminHostsStorage | None = Depends(get_storage),
) -> AgentHostCapacityHistoryResponse:
    """Read one host's stored CPU and memory history.

    Separate from the pool endpoint on purpose. The table is the page's main
    information and it loads on every poll; a 24-hour curve is wanted only when
    an operator opens one machine, and folding it into the poll would make
    every thirty seconds cost a day of rows per host.

    Only the current epoch is served. A rebuilt machine inherits its name, so
    an older generation's points describe hardware that is no longer there —
    the response carries the epoch so a client can tell that its curve is
    stale rather than let it appear to continue into the new machine.

    Every point is one observation of one minute, and a minute nobody measured
    comes back as an explicit null rather than as an interpolated value: the
    chart exists to show when a machine was in trouble, and a filled-in gap is
    the one thing that would hide it.
    """
    seconds = resolve_history_range(window)
    if seconds is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported range {window!r}. Use one of: {', '.join(sorted(HISTORY_RANGES))}."
            ),
        )
    history = await _require_store(store).host_capacity.host_capacity_history(
        host=host, range_seconds=seconds
    )
    if history is None:
        raise HTTPException(status_code=404, detail=f"Unknown runner host: {host}")
    epoch = history["capacity_epoch"]
    return AgentHostCapacityHistoryResponse(
        host=host,
        capacity_epoch=None if epoch is None else str(epoch),
        since=history["since"],
        until=history["until"],
        observed_at=history["observed_at"],
        points=history_point_grid(
            rows=history["rows"], since=history["since"], until=history["until"]
        ),
    )
