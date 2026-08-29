"""Admin endpoints for the agent runner host pool.

Runners pull work, so there is no gateway-side dial that points jobs at a
machine — the only moment the platform gets to decide "not you" is the claim.
These endpoints therefore do not move anything: they name the host whose
runners may claim, and the switch takes effect on the losing host's next poll.

A host joins the pool by polling, never by being registered here. That keeps
the list to machines that actually exist and are actually configured, which is
the difference between switching to a host and switching to a typo.

**A host name is a scheduling label, not a machine identity** — for a host that
joined by polling. The runner reports it, so anything holding the dispatcher
credential can report any name: two machines configured alike are one entry,
and a runner that wants another host's work only has to claim its name. That is
acceptable because the credential is already the boundary — it opens the claim
door for every host — and this switch decides *where our own machines run our
own jobs*. Do not build anything on it that needs to survive a hostile runner.

**Enrolled hosts (phase G) are the exception, and the reason enrollment
exists.** A host that redeemed an enrollment token holds a credential naming
one machine, so for those the name *is* an identity: a compromised host cannot
claim as its neighbour, and revoking it kills exactly one machine's access. The
two kinds live in one table and one list on purpose — an operator should not
have to know which door a machine came through to see it — and the ``enrolled``
flag on each row says which guarantee applies.

The endpoints below are therefore two generations layered: the pin and the
sighting list from #1158, and the pool's own lifecycle (enroll, drain, remove)
from G. They compose rather than compete. The pin decides *which* host may
claim; the pool decides *whether* that host is in a state to.
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
    """Pin agent jobs to one host, or unpin so any runner may claim.

    Non-preemptive, and worth being precise about: the new host starts claiming
    at once, while jobs already running on the old one keep running to the end
    — they hold a lease and their runner reports through without claiming
    again. The two overlap. This is not a drain, which would mean waiting for
    the old host to empty before the new one starts.
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
                "(ops/deploy/agent_runner.sh up) and it will appear here."
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

    Non-retroactive, like the host pin, and for a sharper reason. A thread is a
    live harness session whose checkpoint is refused — silently, into replay —
    when the version moves under it. So this applies to threads that start
    after it; conversations already in flight keep the version they began on.
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
    silently unpin — every other machine would start claiming, which is the
    opposite of what removing a host from a list looks like it does. That
    refusal is decided inside the store's transaction, not by a check here: a
    check here is a read a concurrent switch can outrun.
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
            "leases. Legacy one-shot work may be retried; an indeterminate "
            "retained-chat activation is marked LOST instead of replayed."
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
    rejects later writes from the superseded worker. Legacy one-shot work may
    then follow its normal retry policy; a retained activation is not replayed
    because the platform cannot know whether its prompt ran, so that chat is
    marked LOST when the lease expires.

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
                "force=true to let their leases expire under the applicable "
                "one-shot retry or retained-context fail-closed policy."
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
