"""The control plane as its own service.

In the gateway this was forty lines inside a much larger ``bootstrap`` that
also built routers, adapters and observability. Here it is the whole process,
and the lifecycle is the same three things it always was: a store, a reaper,
and a publisher.

**The reaper is what makes a vanished sandbox recoverable.** It supersedes
attempts whose lease expired and requeues, fails or cancels the job per the
store's policy. It never dies on an error, because a transient database blip
must not permanently stop reaping — a stopped reaper is invisible until jobs
start hanging.

**The snapshot coordinator** asks an owning workspace broker for short,
quiescent Git captures. It has its own schedule so a slow Docker/Git operation
cannot delay grant renewal or attempt recovery.

**The publisher is the only component holding a git credential**, which is
precisely what keeps the sandbox credential-free. A deployment without one
simply never publishes, rather than failing to start.

All three are background tasks held in a module-level set: a task the loop can
garbage-collect mid-run is a task that stops silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloud_agent.gateway import GatewayError
from cloud_agent.grants import GRANT_TTL_S, RENEWAL_INTERVAL_S
from cloud_agent.host_pool import HEARTBEAT_INTERVAL_S, MISSED_HEARTBEATS_BEFORE_OFFLINE
from cloud_agent.logging_utils import configure_process_logging, get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.ports import (
        AttemptCommands,
        ConversationQueries,
        GrantRepository,
        HostPoolRepository,
        ResidencyRepository,
        SandboxRepository,
        SessionForkRepository,
        SnapshotRepository,
    )
    from cloud_agent.publish_worker import PublishStorage


class _SettledTerminals(Protocol):
    """What reconciling a settled terminal reaches for."""

    conversation_queries: ConversationQueries
    host_pool: HostPoolRepository
    sandbox: SandboxRepository


class _GrantRenewal(Protocol):
    """Renewing a live grant, and abandoning the attempt when it cannot be."""

    attempt_commands: AttemptCommands
    grants: GrantRepository


class _SnapshotCapture(Protocol):
    """Capturing a due shared-workspace snapshot, through its host's broker."""

    host_pool: HostPoolRepository
    snapshot: SnapshotRepository


class _Reaping(Protocol):
    """What one reaper pass reaches for, named rather than inherited.

    The pass is deliberately broad -- hosts, then residencies, then queued
    follow-ups, then attempts, then the forks and snapshots an expiry
    strands -- so this list is long. It is still a list: adding an eighth
    capability is an edit here, not a call that happens to type-check.
    """

    attempt_commands: AttemptCommands
    conversation_queries: ConversationQueries
    grants: GrantRepository
    host_pool: HostPoolRepository
    residency: ResidencyRepository
    sandbox: SandboxRepository
    session_fork: SessionForkRepository
    snapshot: SnapshotRepository


logger = get_logger(__name__)

ENV_DATABASE_URL = "AGENT_DATABASE_URL"
ENV_PUBLISH_BASE_BRANCH = "AGENT_PUBLISH_BASE_BRANCH"
ENV_GITHUB_TOKEN = "AGENT_GITHUB_TOKEN"

#: The admin observation reads. Their bodies are time-varying and describe
#: other users' runs, so no intermediary may store one.
ADMIN_READ_PREFIX = "/admin/agent/runs"


def install_admin_no_store(app: FastAPI, *, prefix: str = ADMIN_READ_PREFIX) -> None:
    """Keep admin read responses, refusals included, out of every cache.

    A route dependency can set a header on a response the route returns, but
    FastAPI builds a fresh response for a raised refusal and for its own
    validation errors. These handlers wrap the defaults instead of replacing
    them, and are registered here rather than as middleware: middleware would
    also sit in front of the event streams, which are long-lived by design.
    """
    prefix = prefix.rstrip("/")

    async def with_no_store(request: Request, exc: Exception) -> Response:
        response: Response
        if isinstance(exc, RequestValidationError):
            response = await request_validation_exception_handler(request, exc)
        else:
            response = await http_exception_handler(request, exc)  # type: ignore[arg-type]
        if request.url.path == prefix or request.url.path.startswith(f"{prefix}/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_exception_handler(StarletteHTTPException, with_no_store)
    app.add_exception_handler(RequestValidationError, with_no_store)


#: Held so the event loop cannot collect a running background task. Without
#: this the reaper and publisher are eligible for garbage collection the moment
#: nothing else references them, and they stop without a word.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

#: How often the reaper looks for expired leases and stalled publishes — and,
#: because the grant passes ride on the same loop, how often a live grant is
#: renewed. It is :data:`cloud_agent.grants.RENEWAL_INTERVAL_S` rather than a
#: number of its own precisely so it cannot drift from the grant TTL that is
#: derived from it; the invariant is stated where those constants live.
REAP_INTERVAL_S = RENEWAL_INTERVAL_S
SNAPSHOT_CAPTURE_INTERVAL_S = 5.0
MAX_ATTEMPTS = 3

#: The gateway refusals that are a verdict on the grant itself.
#:
#: Everything else it can say — a wrong dispatch token, an internal API it was
#: never configured to expose, a 500, a connection it never accepted — is a
#: statement about the gateway or about this service, and abandoning a user's
#: running job over it would be answering our own outage by destroying their
#: work. Only these three name the grant or its subject:
#:
#: * ``unknown_grant`` — the gateway has no record of it.
#: * ``grant_not_live`` — revoked out of band, or expired past renewal.
#: * ``subject_unavailable`` — the user was suspended mid-job.
#:
#: An unrecognised type is therefore *not* definitive, and that direction is
#: deliberate: the cost of not giving up is a few more renewal attempts against
#: a capability the TTL will retire anyway, while the cost of giving up wrongly
#: is a destroyed attempt. Adding a type here is a decision to destroy work on
#: it. Mirrored from the gateway's ``routers/agent_grants.py``.
DEAD_GRANT_REFUSALS = frozenset({"unknown_grant", "grant_not_live", "subject_unavailable"})

#: Consecutive *definitive* refusals of one grant before the control plane
#: stops asking.
#:
#: Renewal needs a give-up for the same reason revocation detaches as it hands
#: over: a grant the gateway has permanently refused — revoked out of band, its
#: subject suspended — would otherwise be retried every pass for the life of the
#: attempt, and the attempt would run on toward the moment its capability dies
#: with nothing anywhere saying so.
#:
#: Definitive is the whole of it. A refusal that names *this grant* counts; a
#: gateway that is unreachable, timing out, or 500ing has said nothing about
#: this grant and must count for nothing. Conflating the two makes this
#: threshold a fleet-wide demolition charge: at three passes, a gateway
#: restart lasting ``GRANT_RENEWAL_GIVE_UP_AFTER * REAP_INTERVAL_S`` seconds
#: would abandon every running attempt at once and throw away work that was
#: about to finish. See :meth:`GatewayError.definitive`.
#:
#: Small on purpose. ``GRANT_RENEWAL_GIVE_UP_AFTER * REAP_INTERVAL_S`` must stay
#: below the gateway's renewal step, so that giving up happens while the
#: capability is still alive and the attempt can be stopped cleanly instead of
#: discovering its token dead mid-call. ``test_grant_lifecycle.py`` pins that.
GRANT_RENEWAL_GIVE_UP_AFTER = 3

#: Rows one renewal pass will take. Matches the store's own default; named
#: here so the truncation warning cannot drift from the query's limit.
GRANT_RENEWAL_PAGE = 200

#: Renewals in flight at once. The pass has to finish well inside a grant's
#: lifetime, and serially it cost the sum of every round trip — so at the page
#: cap a merely slow gateway let the last grants expire waiting their turn.
#: Bounded rather than unlimited: a full page arriving at once, every interval,
#: is a burst the gateway has to absorb, and fixing our latency by overwhelming
#: a dependency moves the problem instead of solving it.
GRANT_RENEWAL_CONCURRENCY = 16

#: How long one renewal pass may take before what is left of it is abandoned
#: to the next one. **Derived, never typed**, for the same reason
#: :data:`cloud_agent.grants.GRANT_TTL_S` is.
#:
#: Concurrency alone does not bound this. A full page at
#: ``GRANT_RENEWAL_CONCURRENCY`` is ``ceil(200/16) = 13`` waves, and a gateway
#: that answers slowly rather than failing makes each wave cost the full
#: request timeout — so a pass could still run past the lifetime of the grants
#: it was renewing, which is the exact failure the concurrency was added to
#: fix, just further out.
#:
#: The bound: a grant renewed at the end of one pass is next reached at the end
#: of the following one, so the worst gap between two renewals of the same
#: grant is ``duration + interval + duration``. Keeping that under the TTL
#: gives ``duration < (TTL - interval) / 2`` — 45s at today's numbers.
#:
#: Cutting the pass short is safe and self-correcting: the rows come back
#: ordered by soonest expiry, so what gets dropped is the least urgent, and it
#: is retried a whole interval before it could expire. A pass that keeps
#: hitting this says the page is too large or the gateway too slow, which is
#: why it logs rather than passing quietly.
GRANT_RENEWAL_PASS_DEADLINE_S = (GRANT_TTL_S - RENEWAL_INTERVAL_S) / 2


async def _reconcile_settled_terminals(store: _SettledTerminals) -> None:
    """Replay durable settled-terminal recovery markers after restarts.

    The broker is resolved per job, because with a host pool there is no
    "the" broker: each job's retained terminal lives on the machine that ran
    it. A single env-configured client here would resume one host's terminals
    and quietly report success for every other host's.
    """
    from cloud_agent.broker_routing import broker_for_job, hydrate_shared_broker
    from cloud_agent.terminal_coordination import resume_settled_terminal

    for job_id in await store.sandbox.list_terminal_resumes_pending():
        try:
            job = await store.conversation_queries.get_job(job_id)
            if job is None or job.get("thread_sandbox") is None:
                # No retained group exists to resume (for example, a queued
                # job cancelled before its first claim). This marker has no
                # external side effect behind it and can be retired.
                await store.sandbox.mark_terminal_resume_complete(job_id=job_id)
                continue
            broker = await hydrate_shared_broker(job, await broker_for_job(store.host_pool, job_id))
            if await resume_settled_terminal(
                str(job["thread_id"]),
                job_id=job_id,
                broker=broker,
            ):
                await store.sandbox.mark_terminal_resume_complete(job_id=job_id)
        except Exception:
            # One unavailable host must not prevent reconciliation of markers
            # owned by every other host in this pass. The marker remains set,
            # so this exact job is retried on the next pass.
            logger.warning(
                "Settled terminal reconciliation failed",
                exc_info=True,
                extra={"event": "agent_terminal_reconcile_failed", "job_id": job_id},
            )


async def _revoke_dead_grants(store: GrantRepository, grants: Any) -> None:
    """Withdraw the grants of attempts that are no longer current.

    Runs on the reaper's schedule rather than at the moment an attempt ends,
    and covers every way an attempt can stop being current with one query: a
    worker reporting a terminal state, the reaper superseding a lapsed lease,
    or a control plane that was restarted between the two. A revoke call at
    each of those call sites would have to be written three times, would put a
    gateway round trip inside the fenced terminal transition, and would still
    miss the restart.

    Revocation is an acceleration of the TTL, never the mechanism — the store
    detaches each grant as it hands it over, so a failure here costs lateness
    and not a retry every pass. ``GrantClient.revoke`` reports rather than
    raises for the same reason.
    """
    for row in await store.take_revocable_grants():
        if not await grants.revoke(row["grant_id"]):
            logger.warning(
                "agent_grant_not_revoked",
                extra={
                    "event": "agent_grant_not_revoked",
                    "job_id": row["job_id"],
                    "attempt_id": row["attempt_id"],
                },
            )


async def _abandon_unrenewable_attempt(store: AttemptCommands, row: dict[str, Any]) -> None:
    """Stop an attempt whose grant the gateway will not renew.

    **Giving up has to be something the attempt notices.** Simply stopping the
    renewal calls would leave the sandbox running until its capability expired
    and then failing at a model call, with the cause several minutes and one
    process away. Releasing the claim is the same answer the claim path already
    gives when a grant cannot be minted at all: the attempt is aborted, the
    worker's next heartbeat says ``ok: False`` — stop immediately — and the job
    goes back to the queue **without spending a retry**, where the next claim
    asks for a fresh grant rather than nursing a dead one.

    Nothing revokes the grant here. The release makes the attempt non-current,
    which is exactly what ``take_revocable_grants`` selects, so the revocation
    pass picks it up once and detaches it. Revoking inline would be a second
    call to the gateway that just refused us, on a path that has already
    decided the TTL is the backstop.

    Args:
        store: The agent store.
        row: A ``list_renewable_grants`` row, carrying the attempt's fence.
    """
    released = await store.release_claim(
        job_id=row["job_id"],
        attempt_id=row["attempt_id"],
        lease_generation=row["lease_generation"],
    )
    logger.warning(
        "agent_grant_renewal_given_up",
        extra={
            "event": "agent_grant_renewal_given_up",
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "requeued": released,
        },
    )


async def _renew_live_grants(
    store: _GrantRenewal, grants: Any, *, failures: dict[str, int]
) -> None:
    """Extend the grants of attempts that are still current — and only those.

    **The store is consulted every pass, and that is the fence.** The gateway
    holds no attempt state after the split, so it cannot tell a live attempt
    from a superseded one; a renewal loop that remembered what it minted and
    renewed on a timer would keep a zombie sandbox spending, and nothing
    anywhere would notice. ``list_renewable_grants`` answers only for attempts
    that are ``running`` with an unexpired lease, so a superseded attempt stops
    being renewed on the very next pass and its capability dies at its own TTL.

    A failed renewal is retried on the next pass — one bad minute at the
    gateway must not end a job — but a *definitively refused* one is not
    retried forever. After :data:`GRANT_RENEWAL_GIVE_UP_AFTER` consecutive
    definitive refusals of the same grant the attempt is abandoned rather than
    left running toward a capability that is about to expire; see
    :func:`_abandon_unrenewable_attempt`.

    **Only a refusal that names this grant counts toward that** — see
    :data:`DEAD_GRANT_REFUSALS`. An unreachable gateway, a rotated dispatch
    token and a revoked grant all arrive as the same exception type, and
    treating them alike would mean a brief outage or one bad environment
    variable abandoned every running attempt in the fleet at once: the failure
    modes where giving up is most expensive and least justified. Anything
    other than a verdict on the grant therefore leaves the count untouched. It
    is neither evidence against the grant nor evidence for it, so it neither
    advances the give-up nor forgives refusals already counted; only a
    successful renewal clears those.

    Args:
        store: The agent store.
        grants: The grant client.
        failures: Consecutive definitive refusals per grant id, owned by the
            caller so it survives between passes. Pruned here to the grants
            still live, so a finished job leaves nothing behind.
    """
    seen: set[str] = set()
    renewed: list[str] = []
    renewable = await store.grants.list_renewable_grants()
    if len(renewable) >= GRANT_RENEWAL_PAGE:
        # The pass was capped, so some live attempt was not renewed this time
        # round. The ordering makes the window rotate, but a pool this size
        # needs a bigger cap or a faster loop — and the failure it produces is
        # a job dying at a model call, which points nowhere near here.
        logger.warning(
            "agent_grant_renewal_truncated",
            extra={"event": "agent_grant_renewal_truncated", "count": len(renewable)},
        )

    async def _renew_one(row: dict[str, Any]) -> None:
        grant_id = row["grant_id"]
        seen.add(grant_id)
        try:
            await grants.renew(grant_id)
        except Exception as exc:
            definitive = (
                isinstance(exc, GatewayError)
                and exc.definitive
                and exc.error_type in DEAD_GRANT_REFUSALS
            )
            refusals = failures.get(grant_id, 0) + (1 if definitive else 0)
            if definitive:
                failures[grant_id] = refusals
            logger.warning(
                "agent_grant_renewal_failed",
                exc_info=True,
                extra={
                    "event": "agent_grant_renewal_failed",
                    "job_id": row["job_id"],
                    "attempt_id": row["attempt_id"],
                    "definitive": definitive,
                    "consecutive_refusals": refusals,
                },
            )
            if definitive and refusals >= GRANT_RENEWAL_GIVE_UP_AFTER:
                try:
                    await _abandon_unrenewable_attempt(store.attempt_commands, row)
                except Exception:
                    # Leave the count at or above the threshold, so the next
                    # pass that is again definitively refused retries the
                    # release rather than resuming renewals this one has
                    # already decided are hopeless. A pass that merely cannot
                    # reach the gateway retries neither: releasing an attempt
                    # requires the gateway too, and there is nothing to gain by
                    # asking a second time in the same outage.
                    logger.warning("Abandoning an unrenewable attempt failed", exc_info=True)
                else:
                    seen.discard(grant_id)
            return
        # Consecutive, not cumulative: one refused pass followed by a good one
        # is a gateway blip, and must not accumulate toward a give-up over the
        # hours a long job runs.
        failures.pop(grant_id, None)

    # **Concurrently, because a pass must finish inside a grant's lifetime.**
    # Serially, one pass cost the sum of every renewal's round trip: at the
    # page cap, with a gateway answering slowly but not failing, the last
    # grants in the list expired waiting their turn — and the symptom was a
    # sandbox losing authorization mid-run, which points nowhere near here.
    # Ordering by soonest expiry made the starvation rotate; it did not make
    # the pass fit.
    #
    # Bounded, though. Two hundred simultaneous renewals is a burst the
    # gateway would have to absorb every interval, and a control plane that
    # fixes its own latency by overwhelming its dependency has moved the
    # problem rather than solved it.
    #
    # Each coroutine touches only its own grant's entry in `failures` and its
    # own id in `seen`, so the shared state needs no lock — the rows come from
    # one query and a grant id appears once.
    limit = asyncio.Semaphore(GRANT_RENEWAL_CONCURRENCY)

    async def _renew_bounded(row: dict[str, Any]) -> None:
        async with limit:
            await _renew_one(row)
            renewed.append(row["grant_id"])

    # **And time-boxed**, because bounded concurrency is not the same as a
    # bounded pass: thirteen waves of a slow-but-not-failing gateway still
    # outruns the lifetime being renewed. What is cut is the tail of a list
    # ordered by soonest expiry — the least urgent rows — and it is retried a
    # full interval before it could expire.
    try:
        await asyncio.wait_for(
            asyncio.gather(*(_renew_bounded(row) for row in renewable)),
            timeout=GRANT_RENEWAL_PASS_DEADLINE_S,
        )
    except (TimeoutError, asyncio.TimeoutError):
        # Loud, and with both numbers. A pass that keeps running out of time is
        # a page too large or a gateway too slow, and the failure it eventually
        # produces — a sandbox losing authorization mid-run — points nowhere
        # near this loop.
        logger.warning(
            "agent_grant_renewal_pass_timed_out",
            extra={
                "event": "agent_grant_renewal_pass_timed_out",
                "renewed": len(renewed),
                "outstanding": len(renewable) - len(renewed),
                "deadline_seconds": GRANT_RENEWAL_PASS_DEADLINE_S,
            },
        )
        # `seen` is what prunes `failures`, and a grant whose renewal was
        # cancelled is still live — dropping it here would delete a refusal
        # count that the give-up rule depends on being consecutive.
        seen.update(row["grant_id"] for row in renewable)

    for grant_id in [known for known in failures if known not in seen]:
        del failures[grant_id]


async def _reap_offline_hosts(store: HostPoolRepository) -> None:
    """Move hosts that stopped heartbeating to ``offline``.

    Runs on the same loop as the attempt reaper rather than on its own, because
    the two are one recovery story told in order: a host disappears, it stops
    being claimable, and then the work it was holding comes back. Splitting
    them into two schedules would make the interval between them a race that
    only shows up under load.
    """
    offline = await store.reap_offline_hosts(
        interval_seconds=HEARTBEAT_INTERVAL_S,
        missed_intervals=MISSED_HEARTBEATS_BEFORE_OFFLINE,
    )
    if offline:
        logger.warning(
            "agent_hosts_offline",
            extra={"event": "agent_hosts_offline", "hosts": offline},
        )


async def _reap_lapsed_residencies(store: ResidencyRepository, grants: Any | None) -> None:
    """Lapse residencies whose lease ran out — and kill their credentials.

    A lapsed residency's grant dies with it (retained-session design §6): the credential
    lives exactly as long as the lease, so a host that stopped beating must
    starve promptly rather than keep spending until the grant's own TTL.
    ``revoke()`` never raises, and a deployment without a grant client simply
    has nothing to revoke — the lapse itself already happened in the store.
    """
    lapsed = await store.reap_expired_residencies()
    if not lapsed:
        return
    logger.info(
        "agent_residencies_lapsed",
        extra={
            "event": "agent_residencies_lapsed",
            "threads": [row["thread_id"] for row in lapsed],
        },
    )
    if grants is None:
        return
    for row in lapsed:
        if row.get("grant_id"):
            await grants.revoke(row["grant_id"])


async def _cleanup_reaped_shared_residencies(
    store: HostPoolRepository,
    actions: list[dict[str, Any]],
    grants: Any | None,
) -> None:
    """Fence exact shared member processes orphaned by attempt expiry."""
    cleanups: list[tuple[Any, Any]] = [
        (action, residency_cleanup)
        for action in actions
        if (residency_cleanup := action.get("cleanup_residency"))
    ]
    if not cleanups:
        return

    from cloud_agent.broker_routing import broker_for_job

    for action, cleanup in cleanups:
        try:
            broker = await broker_for_job(store, str(action["job_id"]))
            if broker is not None:
                await broker.shared_residency_lost(
                    str(cleanup["thread_id"]),
                    generation=int(cleanup["generation"]),
                )
        except Exception:
            # The durable residency generation is already closed. A later
            # host heartbeat echoes the dead generation and retries local
            # process cleanup, so broker reachability cannot block reaping.
            logger.warning(
                "agent_reaped_shared_residency_cleanup_failed",
                extra={
                    "event": "agent_reaped_shared_residency_cleanup_failed",
                    "thread_id": cleanup["thread_id"],
                    "generation": cleanup["generation"],
                },
                exc_info=True,
            )
        grant_id = cleanup.get("grant_id")
        if grant_id and grants is not None:
            with contextlib.suppress(Exception):
                await grants.revoke(grant_id)


async def _capture_due_shared_snapshot(store: _SnapshotCapture) -> None:
    """Capture at most one quiescent shared workspace without blocking turns."""
    snapshot = await store.snapshot.claim_due_sandbox_snapshot()
    if snapshot is None:
        return

    from cloud_agent.broker_routing import broker_for_job
    from cloud_agent_host.workspace_broker_client import WorkspaceBrokerError

    try:
        broker = await broker_for_job(store.host_pool, snapshot["anchor_job_id"])
        if broker is None:
            await store.snapshot.defer_sandbox_snapshot_capture(
                snapshot_id=int(snapshot["snapshot_id"]),
                sandbox_id=str(snapshot["sandbox_id"]),
                sandbox_generation=int(snapshot["generation"]),
                container_id=str(snapshot["container_id"]),
                detail="the owning workspace broker is unavailable",
            )
            return
        receipt = await broker.capture_shared_snapshot(snapshot)
        expected = {
            "snapshot_id": int(snapshot["snapshot_id"]),
            "sandbox_id": snapshot["sandbox_id"],
            "sandbox_generation": int(snapshot["generation"]),
            "container_id": snapshot["container_id"],
            "workspace_epoch": int(snapshot["workspace_epoch"]),
            "base_sha": snapshot["base_sha"],
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError("shared snapshot host receipt identity did not match the claim")
        patch = receipt.get("patch")
        digest = receipt.get("content_sha256")
        if not isinstance(patch, str) or not isinstance(digest, str):
            raise ValueError("shared snapshot host receipt omitted its patch or hash")
        recorded = await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=int(snapshot["snapshot_id"]),
            sandbox_id=str(snapshot["sandbox_id"]),
            sandbox_generation=int(snapshot["generation"]),
            container_id=str(snapshot["container_id"]),
            workspace_epoch=int(snapshot["workspace_epoch"]),
            base_sha=str(snapshot["base_sha"]),
            patch=patch,
            content_sha256=digest,
        )
        if recorded is None:
            raise ValueError("shared snapshot receipt lost its durable generation fence")
    except WorkspaceBrokerError as exc:
        await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=int(snapshot["snapshot_id"]),
            sandbox_id=str(snapshot["sandbox_id"]),
            sandbox_generation=int(snapshot["generation"]),
            container_id=str(snapshot["container_id"]),
            detail=str(exc),
            dirty=exc.status_code in {410, 422, 502},
        )
    except ValueError as exc:
        await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=int(snapshot["snapshot_id"]),
            sandbox_id=str(snapshot["sandbox_id"]),
            sandbox_generation=int(snapshot["generation"]),
            container_id=str(snapshot["container_id"]),
            detail=str(exc),
            dirty=True,
        )
    except asyncio.CancelledError:
        # A graceful control-plane stop must not leave this claim invisible
        # until the much longer stalled-capture timeout elapses.
        with contextlib.suppress(Exception):
            await store.snapshot.defer_sandbox_snapshot_capture(
                snapshot_id=int(snapshot["snapshot_id"]),
                sandbox_id=str(snapshot["sandbox_id"]),
                sandbox_generation=int(snapshot["generation"]),
                container_id=str(snapshot["container_id"]),
                detail="snapshot coordinator stopped during capture",
            )
        raise
    except Exception as exc:
        # Routing/database surprises are retryable infrastructure failures.
        # Release the durable claim before surfacing it to the coordinator's
        # log-and-continue boundary.
        with contextlib.suppress(Exception):
            await store.snapshot.defer_sandbox_snapshot_capture(
                snapshot_id=int(snapshot["snapshot_id"]),
                sandbox_id=str(snapshot["sandbox_id"]),
                sandbox_generation=int(snapshot["generation"]),
                container_id=str(snapshot["container_id"]),
                detail=str(exc) or type(exc).__name__,
            )
        raise


async def coordinate_shared_snapshots(
    store: _SnapshotCapture,
    *,
    interval_seconds: float = SNAPSHOT_CAPTURE_INTERVAL_S,
) -> None:
    """Capture shared-workspace publication snapshots on an independent clock."""
    if interval_seconds <= 0:
        raise ValueError("snapshot capture interval must be positive")
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _capture_due_shared_snapshot(store)
        except Exception:
            logger.warning("Shared workspace snapshot pass failed", exc_info=True)


async def reap_expired_attempts(
    store: _Reaping,
    *,
    interval_seconds: float = REAP_INTERVAL_S,
    max_attempts: int = MAX_ATTEMPTS,
    grants: Any | None = None,
) -> None:
    """Periodically close attempts whose lease expired.

    Args:
        store: The agent store.
        interval_seconds: Delay between passes.
        max_attempts: How many attempts a job gets before it fails for good.
        grants: The grant client, when this deployment mints grants. ``None``
            skips both grant passes entirely — the capabilities then expire at
            their own TTL, which is the same backstop revocation accelerates.
    """
    try:
        await _reconcile_settled_terminals(store)
    except Exception:
        logger.warning("Initial settled terminal reconciliation failed", exc_info=True)
    # Consecutive renewal refusals, per grant. Held by the loop rather than by
    # the module, so two reapers (a test's and a running app's) cannot count
    # each other's failures toward a give-up — and so a restart forgets, which
    # is the right way round: a fresh process should ask the gateway again
    # before abandoning anyone's attempt.
    renewal_failures: dict[str, int] = {}
    while True:
        await asyncio.sleep(interval_seconds)
        # Hosts first. A machine that stopped heartbeating should be out of the
        # claim gate before the pass that requeues its work, otherwise the
        # requeued job can be handed straight back to the host that just lost
        # it — the retry budget burns down while nothing moves.
        #
        # In its own try, though. Sharing one with the attempt reaper below
        # would let a host-reaping failure skip lease reaping and stalled
        # publishes for that pass, which is a strictly worse outage than the
        # one it was reporting: jobs stop being recovered at all.
        try:
            await _reap_offline_hosts(store.host_pool)
        except Exception:
            logger.warning("Host reaper pass failed", exc_info=True)
        # Residencies after hosts, before attempts: a lapsed hot sandbox must
        # lose its generation (heartbeats, credentials, affinity) before the
        # attempt pass requeues anything that might route back to it. Its own
        # try, for the same reason as the host pass above.
        try:
            await _reap_lapsed_residencies(store.residency, grants)
        except Exception:
            logger.warning("Residency reaper pass failed", exc_info=True)
        # Queued follow-ups after residencies, before the attempt pass: give
        # legacy turns a terminal answer if their native context is missing.
        # Normal ordering is enforced at claim time; this cleanup uses the
        # partial queued indexes instead of running on every runner poll.
        try:
            await store.attempt_commands.sweep_queued_follow_ups()
        except Exception:
            logger.warning("Queued follow-up sweep failed", exc_info=True)
        try:
            actions = await store.attempt_commands.reap_expired(max_attempts=max_attempts)
            await _cleanup_reaped_shared_residencies(store.host_pool, actions, grants)
            await _reconcile_settled_terminals(store)
            if actions:
                logger.info(
                    "agent_attempts_reaped",
                    extra={"event": "agent_attempts_reaped", "count": len(actions)},
                )
            stalled_captures = await store.snapshot.reap_stalled_sandbox_snapshot_captures()
            if stalled_captures:
                logger.warning(
                    "agent_snapshot_captures_stalled",
                    extra={
                        "event": "agent_snapshot_captures_stalled",
                        "snapshot_ids": stalled_captures,
                    },
                )
            stalled_snapshot_publishes = await store.snapshot.reap_stalled_snapshot_publishes()
            if stalled_snapshot_publishes:
                logger.warning(
                    "agent_snapshot_publishes_stalled",
                    extra={
                        "event": "agent_snapshot_publishes_stalled",
                        "snapshot_ids": stalled_snapshot_publishes,
                    },
                )
            stalled_session_forks = await store.session_fork.reap_stalled_session_forks()
            if stalled_session_forks:
                from cloud_agent.broker_routing import broker_for_job

                recovered: list[str] = []
                cleaned: list[str] = []
                for operation in stalled_session_forks:
                    operation_id = str(operation["id"])
                    if operation.get("recovery_action") == "finalize":
                        if await store.session_fork.finalize_session_fork(
                            operation_id=operation_id
                        ):
                            recovered.append(operation_id)
                        continue
                    broker = await broker_for_job(store.host_pool, operation["source_job_id"])
                    for residency in operation.get("cleanup_residencies") or []:
                        if broker is not None:
                            with contextlib.suppress(Exception):
                                await broker.shared_residency_lost(
                                    str(residency["thread_id"]),
                                    generation=int(residency["generation"]),
                                )
                        grant_id = residency.get("grant_id")
                        if grant_id and grants is not None:
                            with contextlib.suppress(Exception):
                                await grants.revoke(grant_id)
                    cleaned.append(operation_id)
                logger.warning(
                    "agent_session_forks_reconciled",
                    extra={
                        "event": "agent_session_forks_reconciled",
                        "recovered_operation_ids": recovered,
                        "cleaned_operation_ids": cleaned,
                    },
                )
        except Exception:
            logger.warning("Agent attempt reaper pass failed", exc_info=True)
        # After the reaper, so an attempt superseded in this very pass has its
        # grant withdrawn in the same one — and in its own try for the reason
        # the host reaper has one: a gateway having a bad minute must not stop
        # attempts being recovered.
        if grants is not None:
            try:
                await _revoke_dead_grants(store.grants, grants)
            except Exception:
                logger.warning("Grant revocation pass failed", exc_info=True)
            try:
                await _renew_live_grants(store, grants, failures=renewal_failures)
            except Exception:
                logger.warning("Grant renewal pass failed", exc_info=True)


def _build_source_control(app: FastAPI) -> None:
    """Attach GitHub App and GitLab OAuth clients, where configured.

    Each is optional and each failure is contained: a partial config disables
    that provider rather than booting with a credential path that cannot be
    secured.
    """
    app.state.app_credentials = None
    app.state.gitlab_oauth = None

    try:
        from cloud_agent.github_app import AppConfig, GitHubAppCredentials

        app_config = AppConfig.from_env(dict(os.environ))
        if app_config is not None:
            app.state.app_credentials = GitHubAppCredentials(app_config)
    except Exception:
        logger.warning("GitHub App config present but unusable", exc_info=True)

    try:
        from cloud_agent.source_control import (
            GitLabOAuthClient,
            GitLabOAuthConfig,
            SourceControlCipher,
        )

        gitlab_config = GitLabOAuthConfig.from_env(dict(os.environ))
        if gitlab_config is not None:
            app.state.gitlab_oauth = GitLabOAuthClient(gitlab_config, cipher=SourceControlCipher())
    except Exception:
        logger.warning("GitLab OAuth config present but unusable", exc_info=True)


def _start_publisher(app: FastAPI, store: PublishStorage) -> None:
    """Start the publish loop, idle if no git credential is configured."""
    from cloud_agent.publish_worker import GitHubCredential, publish_loop

    static_token = os.getenv(ENV_GITHUB_TOKEN, "")

    def credential_provider() -> GitHubCredential | None:
        """Mint the credential for one publish, or None if unconfigured.

        Read per call rather than captured, so rotating the token does not
        need a restart.
        """
        token = os.getenv(ENV_GITHUB_TOKEN, static_token)
        return GitHubCredential(token) if token else None

    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=credential_provider,
            app_credentials=app.state.app_credentials,
            base_branch=os.getenv(ENV_PUBLISH_BASE_BRANCH, "dev"),
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    app.state.publish_task = task


@asynccontextmanager
async def lifespan(app: FastAPI, *, background: bool = True) -> AsyncIterator[None]:
    """Open the store, optionally start the reaper and publisher, tear down.

    Args:
        app: The application whose state is being built.
        background: Whether this process owns the reaper and the publisher.
            False for a second listener sharing the database — see the comment
            below for why that is not merely wasteful.

    The database is required. A control plane without one cannot claim, fence
    or record anything, so it refuses to start rather than serving an API that
    fails at every call.
    """
    import asyncpg
    import httpx

    from cloud_agent.api.mcp_relay import close_host, maintain_host_sessions
    from cloud_agent.broker_routing import close_broker_clients
    from cloud_agent.capacity_history import collect_host_capacity_history
    from cloud_agent.gateway import GatewayClient
    from cloud_agent.grants import GrantClient
    from cloud_agent.ledger import collect_usage_snapshots
    from cloud_agent.mcp.catalog import McpCatalog
    from cloud_agent.operational_health import monitor_queue
    from cloud_agent.storage.assembly import Storage

    dsn = os.environ.get(ENV_DATABASE_URL, "")
    if not dsn:
        raise RuntimeError(f"{ENV_DATABASE_URL} must be set")

    pool = await asyncpg.create_pool(dsn)
    storage = Storage(pool)
    # Schema is Alembic's. There is no initialize step to run: the store's
    # no-op one went with the store, and the DDL and backfills it used to
    # carry have been migrations since 0001.
    try:
        from cloud_agent.identity import ENV_ISSUER, gateway_issuer
        from cloud_agent.identity_namespace import ensure_gateway_namespace

        configured = bool(os.environ.get("GATEWAY_BASE_URL") or os.environ.get(ENV_ISSUER))
        issuer = gateway_issuer() if configured else None
        app.state.gateway_issuer = await ensure_gateway_namespace(pool, issuer)
    except Exception:
        await pool.close()
        raise

    app.state.storage = storage
    # One pool, shared by both gateway clients. Each call used to build its own
    # client and pay a connect — and a TLS handshake, where the gateway is
    # behind TLS — and admission is not an occasional call: `require_agent_owner`
    # is a dependency of every owner route, terminal input and resize included.
    # Both clients pass their own deadline per request, so this one carries no
    # default timeout of its own.
    http = httpx.AsyncClient()
    app.state.http = http
    app.state.gateway = GatewayClient(client=http)
    # The sandbox's credential is minted at the gateway now (E9), so the claim
    # path and the renewal/revocation passes share that pool rather than each
    # building their own per call. They take the same seam; until this was
    # wired, the comment here described sharing that was not happening.
    app.state.grants = GrantClient(client=http)
    _build_source_control(app)

    # **The background work belongs to exactly one process.** The reaper
    # renews and revokes grants and supersedes lapsed attempts; the publisher
    # opens pull requests. A second listener on this same database — the MCP
    # relay is one — running its own copies would renew every grant twice per
    # interval, double the load it puts on the gateway, and have two workers
    # racing for the same publish.
    #
    # So this is a parameter of the lifespan rather than a property of it, and
    # `create_mcp_app` says no.
    app.state.mcp_catalog = None
    app.state.mcp_catalog_task = None
    if background:
        app.state.mcp_catalog = McpCatalog(storage.mcp_catalog, http)
        app.state.mcp_catalog_task = asyncio.create_task(app.state.mcp_catalog.run())
        reaper = asyncio.create_task(reap_expired_attempts(storage, grants=app.state.grants))
        snapshot_coordinator = asyncio.create_task(coordinate_shared_snapshots(storage))
        ledger_collector = asyncio.create_task(
            collect_usage_snapshots(storage.ledger, app.state.grants)
        )
        # Host capacity history keeps filling whether or not anybody has the
        # admin page open — that is the whole reason it lives here rather than
        # in the browser. Its own task and its own budgets, so a slow write
        # cannot delay a heartbeat or the reaper.
        capacity_history = asyncio.create_task(collect_host_capacity_history(storage.host_capacity))
        _BACKGROUND_TASKS.add(reaper)
        _BACKGROUND_TASKS.add(snapshot_coordinator)
        _BACKGROUND_TASKS.add(ledger_collector)
        _BACKGROUND_TASKS.add(capacity_history)
        reaper.add_done_callback(_BACKGROUND_TASKS.discard)
        snapshot_coordinator.add_done_callback(_BACKGROUND_TASKS.discard)
        ledger_collector.add_done_callback(_BACKGROUND_TASKS.discard)
        capacity_history.add_done_callback(_BACKGROUND_TASKS.discard)
        app.state.reaper_task = reaper
        app.state.snapshot_task = snapshot_coordinator
        app.state.ledger_task = ledger_collector
        app.state.capacity_task = capacity_history
        app.state.queue_monitor_task = asyncio.create_task(monitor_queue(storage.host_pool))
        _BACKGROUND_TASKS.add(app.state.queue_monitor_task)
        app.state.queue_monitor_task.add_done_callback(_BACKGROUND_TASKS.discard)
        _start_publisher(app, storage)
        logger.info("cloud agent control plane started")
    else:
        app.state.reaper_task = None
        app.state.snapshot_task = None
        app.state.ledger_task = None
        app.state.capacity_task = None
        app.state.queue_monitor_task = None
        app.state.publish_task = None
        logger.info("cloud agent relay listener started")

    # Each listener owns upstream sockets in its own process, even when it
    # does not run the control plane's grant reaper or publisher.
    app.state.mcp_maintenance_task = asyncio.create_task(maintain_host_sessions(storage))
    try:
        yield
    finally:
        for task in (
            app.state.mcp_maintenance_task,
            app.state.mcp_catalog_task,
            app.state.reaper_task,
            app.state.snapshot_task,
            app.state.ledger_task,
            app.state.capacity_task,
            app.state.queue_monitor_task,
            app.state.publish_task,
        ):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await close_host()
        # Close what was opened, innermost first: the per-broker pools, the
        # GitHub App's own client, then the shared gateway pool.
        await close_broker_clients()
        credentials = getattr(app.state, "app_credentials", None)
        if credentials is not None:
            await credentials.aclose()
        await http.aclose()
        await pool.close()
        logger.info("cloud agent control plane stopped")


def create_app() -> FastAPI:
    """Build the control plane application."""
    configure_process_logging()

    from cloud_agent.api import (
        admin_conversations,
        admin_hosts,
        admin_llm_requests,
        admin_runs,
        admin_usage,
        hosts,
        mcp_connections,
        mcp_relay,
        routes,
        service_jobs,
        session,
        skills,
        usage,
    )

    app = FastAPI(
        title="Cloud Agent",
        description="Cloud coding agents on sandboxed hosts",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(routes.router)
    app.include_router(usage.router)
    # Registered before the relay, and never on the relay's own listener. The
    # order matters: the relay's ``/{server_name}`` catch-all would otherwise
    # match a connection-management path and answer it as an MCP request. The
    # listener separation matters more: adding a connection or approving a call
    # is owner-facing work, and a sandbox must not be able to reach it at all.
    app.include_router(skills.router)
    app.include_router(mcp_connections.router)
    app.include_router(mcp_relay.router)
    app.include_router(hosts.router)
    app.include_router(admin_hosts.router)
    app.include_router(admin_conversations.router)
    app.include_router(admin_runs.router)
    app.include_router(admin_usage.router)
    # Per-request LLM detail reads a second service and stays in its own module
    # for the same reason it is a separate page load: the default run detail
    # must not acquire a gateway I/O path.
    app.include_router(admin_llm_requests.router)
    install_admin_no_store(app)
    # The gateway alert relay's way in: machine-to-machine on-call jobs, pinned
    # to one configured service account. Its own module because its caller is a
    # service holding a dispatch token, not a browser holding a session.
    app.include_router(service_jobs.router)
    # The web app's way in. Mounted alongside the others rather than inside
    # ``routes.py`` because it authenticates nothing on entry — it is the route
    # that *creates* the credential every other route requires.
    app.include_router(session.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only — deliberately not readiness.

        A readiness probe here would have to decide what a degraded control
        plane is (no gateway? no git credential?), and every answer to that
        belongs to whoever operates it.
        """
        return {"status": "ok"}

    return app


def create_mcp_app() -> FastAPI:
    """Build a listener that serves the MCP relay and nothing else.

    **A sandbox is the least trusted thing in this system, and this listener is
    one of only two fixed agent-phase endpoints it may reach.** Pointed at the
    main application, its tunnel also reaches jobs, sessions, host registration
    and admin — every route the control plane has. Those refuse an
    ``mcp``-scoped token, so
    nothing was exploitable through them today; but the entire trusted control
    plane sat one authorization bug away from code nobody in this repository
    wrote, and that is a reachability decision rather than an authorization
    one.

    So the separation is structural. This application does not *contain* the
    other routes, which is a stronger statement than any of them refusing:
    there is no path prefix to get wrong in a proxy, no rule to leave off a new
    router, and a route added to ``create_app`` tomorrow is not silently
    exposed here.

    Same process image, same database, same lifespan — it is a second
    listener, not a second service. The cost is one more port; the alternative
    was a path-restricted proxy, which is the same policy written where it can
    be misconfigured and where nothing tests it.
    """
    configure_process_logging()

    from cloud_agent.api import mcp_relay

    app = FastAPI(
        title="Cloud Agent — MCP relay",
        description="The only surface a sandbox may reach",
        version="0.1.0",
        # No reaper and no publisher: they belong to the control-plane process,
        # and a second copy would renew every grant twice and race the same
        # publish. This listener still maintains its own upstream MCP sockets.
        lifespan=lambda app: lifespan(app, background=False),
    )
    app.include_router(mcp_relay.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness, so the tunnel's health check has something to ask."""
        return {"status": "ok"}

    return app
