"""The attempt reaper, and the grant passes that ride its loop.

One pass closes attempts whose lease expired, abandons the ones the gateway has
definitively refused, renews the grants that are still live, reaps offline hosts
and lapsed residencies, cleans up the residencies a stranded fork left behind,
reconciles the forks and snapshot publishes an expiry stranded, and purges
abandoned staged uploads. It ran from ``app.py`` before this module existed; it
runs from the same place in the same process now, started and stopped by the
same lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.application.pending_messages import settle_waiting_queues
from cloud_agent.application.send_now import recover_handoffs
from cloud_agent.gateway import GatewayError
from cloud_agent.goals.controller import run_goal_wakeups
from cloud_agent.grants import GRANT_TTL_S, RENEWAL_INTERVAL_S
from cloud_agent.host_pool import HEARTBEAT_INTERVAL_S, MISSED_HEARTBEATS_BEFORE_OFFLINE
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import integer_setting

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        AttemptCommands,
        ConversationCommands,
        ConversationQueries,
        GoalRepository,
        GoalWakeupRepository,
        GrantRepository,
        HostPoolRepository,
        PendingMessageRepository,
        ResidencyRepository,
        SandboxRepository,
        SessionForkRepository,
        SkillRepository,
        SnapshotRepository,
        SourceControlRepository,
    )
    from cloud_agent.source_control_runtime import GitHubAppSource

#: The moved loop keeps logging under the module it came from: the deployment's
#: log selection is on ``cloud_agent.app``, and a relocation must not move these
#: records to a logger nobody watches.
logger = get_logger("cloud_agent.app")


class _SettledTerminals(Protocol):
    """What reconciling a settled terminal reaches for."""

    conversation_queries: ConversationQueries
    host_pool: HostPoolRepository
    sandbox: SandboxRepository


class _GrantRenewal(Protocol):
    """Renewing a live grant, and abandoning the attempt when it cannot be."""

    attempt_commands: AttemptCommands
    grants: GrantRepository


class _Reaping(Protocol):
    """What one reaper pass reaches for, named rather than inherited.

    The pass is deliberately broad -- hosts, then residencies, then queued
    follow-ups, then attempts, then the owner's waiting messages, then the
    forks and snapshots an expiry strands -- so this list is long. It is still
    a list: adding an eighth capability is an edit here, not a call that
    happens to type-check.
    """

    attachments: AttachmentRepository
    attempt_commands: AttemptCommands
    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    goal_wakeups: GoalWakeupRepository
    goals: GoalRepository
    grants: GrantRepository
    source_control: SourceControlRepository
    host_pool: HostPoolRepository
    pending_messages: PendingMessageRepository
    residency: ResidencyRepository
    sandbox: SandboxRepository
    session_fork: SessionForkRepository
    skills: SkillRepository
    snapshot: SnapshotRepository


#: How often the reaper looks for expired leases and stalled publishes — and,
#: because the grant passes ride on the same loop, how often a live grant is
#: renewed. It is :data:`cloud_agent.grants.RENEWAL_INTERVAL_S` rather than a
#: number of its own precisely so it cannot drift from the grant TTL that is
#: derived from it; the invariant is stated where those constants live.
REAP_INTERVAL_S = RENEWAL_INTERVAL_S


MAX_ATTEMPTS = 3


#: Abandoned staged uploads count against staging quota until collected.
#: Pending-message and job-claimed rows are kept by the repository predicate.
STAGED_UPLOAD_RETENTION_HOURS = 24


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

    The sandbox manager is resolved per job, because with a host pool there is no
    "the" sandbox manager: each job's retained terminal lives on the machine that ran
    it. A single env-configured client here would resume one host's terminals
    and quietly report success for every other host's.
    """
    from cloud_agent.sandbox_manager_routing import (
        hydrate_shared_sandbox_manager,
        sandbox_manager_for_job,
    )
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
            sandbox_manager = await hydrate_shared_sandbox_manager(
                job, await sandbox_manager_for_job(store.host_pool, job_id)
            )
            if await resume_settled_terminal(
                str(job["thread_id"]),
                job_id=job_id,
                sandbox_manager=sandbox_manager,
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


async def _abandon_unrenewable_attempt(store: AttemptCommands, row: dict[str, Any]) -> bool:
    """Hand back an attempt whose grant the gateway will not renew, if it never started.

    Releasing the claim is the same answer the claim path already gives when a
    grant cannot be minted at all: the attempt is aborted, the worker's next
    heartbeat says ``ok: False`` — stop immediately — and the job goes back to
    the queue **without spending a retry**, where the next claim asks for a
    fresh grant rather than nursing a dead one.

    **Only a turn that never started can be handed back.** ``release_claim``
    refuses once the turn's prompt has been admitted to its CLI, because a
    replayed admitted turn runs the same instruction twice, and by the time
    the refusals have been counted the turn usually has been admitted. An
    admitted turn is left to finish: its member process does not spend this
    grant but the residency's own credential, which its host's heartbeat
    renews and which is revoked with the residency. The caller stops renewing
    this grant instead.

    Nothing revokes the grant here. The release makes the attempt non-current,
    which is exactly what ``take_revocable_grants`` selects, so the revocation
    pass picks it up once and detaches it. Revoking inline would be a second
    call to the gateway that just refused us, on a path that has already
    decided the TTL is the backstop.

    Args:
        store: The agent store.
        row: A ``list_renewable_grants`` row, carrying the attempt's fence.

    Returns:
        Whether the attempt was handed back to the queue.
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
    return released


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
    definitive refusals the grant is given up on and not asked about again: an
    attempt whose turn never started is handed back to the queue, and an
    admitted turn, which runs on its residency's credential rather than on this
    grant, is left to finish; see :func:`_abandon_unrenewable_attempt`.

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
            caller so it survives between passes. A count at the give-up
            threshold marks a grant that was given up on. Pruned here to the
            grants still live, so a finished job leaves nothing behind.
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
        if failures.get(grant_id, 0) >= GRANT_RENEWAL_GIVE_UP_AFTER:
            # Given up on, and its turn could not be handed back: it had been
            # admitted. The gateway has said this grant is dead, so asking again
            # would only repeat the refusal -- and, with a count started over,
            # the futile give-up -- for as long as the turn runs.
            return
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
                    released = await _abandon_unrenewable_attempt(store.attempt_commands, row)
                except asyncio.CancelledError:
                    # The pass deadline cut the release short; see below.
                    failures[grant_id] = GRANT_RENEWAL_GIVE_UP_AFTER - 1
                    raise
                except Exception:
                    # The give-up did not happen, so the count must not say it
                    # did. One short of the threshold, the next pass asks once
                    # more and a definitive refusal retries the release at once,
                    # rather than after a whole new run of refusals. A pass that
                    # merely cannot reach the gateway retries neither: there is
                    # nothing to gain by asking a second time in the same outage.
                    failures[grant_id] = GRANT_RENEWAL_GIVE_UP_AFTER - 1
                    logger.warning("Abandoning an unrenewable attempt failed", exc_info=True)
                else:
                    if released:
                        # No longer current, so never listed again: its count
                        # goes with it. An admitted turn keeps its count at the
                        # threshold, which is what stops the asking above.
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
    limit = asyncio.Semaphore(
        integer_setting("grant_renewal_concurrency", GRANT_RENEWAL_CONCURRENCY)
    )

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

    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

    for action, cleanup in cleanups:
        try:
            sandbox_manager = await sandbox_manager_for_job(store, str(action["job_id"]))
            if sandbox_manager is not None:
                await sandbox_manager.shared_residency_lost(
                    str(cleanup["thread_id"]),
                    generation=int(cleanup["generation"]),
                )
        except Exception:
            # The durable residency generation is already closed. A later
            # host heartbeat echoes the dead generation and retries local
            # process cleanup, so sandbox manager reachability cannot block reaping.
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


async def _cleanup_failed_session_fork(
    store: HostPoolRepository, operation: dict[str, Any], grants: Any | None
) -> None:
    """Fence the member processes and grants one failed Fork left behind.

    The store failed the operation and closed these residencies in its own
    commit, and it never returns that operation again, so this is the only
    cleanup it gets. A sandbox manager that cannot be resolved or reached still leaves
    the revocations to run: a later host heartbeat is told the residency was
    lost and stops its process, but nothing else would end its grant early.
    """
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

    sandbox_manager = None
    try:
        sandbox_manager = await sandbox_manager_for_job(store, operation["source_job_id"])
    except Exception:
        logger.warning(
            "agent_session_fork_cleanup_sandbox_manager_unavailable",
            extra={
                "event": "agent_session_fork_cleanup_sandbox_manager_unavailable",
                "operation_id": operation.get("id"),
            },
            exc_info=True,
        )
    for residency in operation.get("cleanup_residencies") or []:
        if sandbox_manager is not None:
            with contextlib.suppress(Exception):
                await sandbox_manager.shared_residency_lost(
                    str(residency["thread_id"]),
                    generation=int(residency["generation"]),
                )
        grant_id = residency.get("grant_id")
        if grant_id and grants is not None:
            with contextlib.suppress(Exception):
                await grants.revoke(grant_id)


async def _run_attempt_reaper(
    store: _Reaping,
    *,
    interval_seconds: float = REAP_INTERVAL_S,
    max_attempts: int | None = None,
    grants: Any | None = None,
    github_app: GitHubAppSource | None = None,
    gateway: Any | None = None,
) -> None:
    """Periodically close attempts whose lease expired.

    Args:
        store: The agent store.
        interval_seconds: Delay between passes.
        max_attempts: How many attempts a job gets before it fails for good.
        grants: The grant client, when this deployment mints grants. ``None``
            skips both grant passes entirely — the capabilities then expire at
            their own TTL, which is the same backstop revocation accelerates.
        github_app: Where to ask for the GitHub App's credentials, once per
            pass. A goal decision checks the repository's entitlement before it
            schedules, and with no minter that check can only see the
            deployment's allowlist — so a repository the owner installed the App
            on would be reported as access denied. Asked rather than held: the
            credentials this process has at startup are not necessarily the
            ones it has now (issue #47). Sending an owner's waiting message
            checks the same entitlement, and waits for the App the same way.
        gateway: The gateway client. Sending an owner's waiting message checks
            the model and effort it names, exactly as sending it by hand does;
            without one, a message naming either waits for a settlement that
            can check it.
    """
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
        # Goal wakeups before the attempt pass, every conversation's. A finish
        # writes its obligation in the same transaction as the terminal
        # transition and drains its own conversation's synchronously; this pass
        # takes what an owner's write armed, and what a process that died
        # between a finish and its drain left behind. The sweep after it is the
        # backstop for a *missing* obligation, and the only thing that pauses a
        # goal: every ordinary way an active goal stops being able to continue
        # says so where and when it happens.
        try:
            await run_goal_wakeups(store=store, github_app=github_app, gateway=gateway)
        except Exception:
            logger.warning("Goal wakeup consumer pass failed", exc_info=True)
        try:
            swept = await store.goals.sweep_goal_scheduling()
            if swept["rearmed"] or swept["stalled"]:
                logger.warning(
                    "agent_goals_recovered",
                    extra={
                        "event": "agent_goals_recovered",
                        "rearmed": len(swept["rearmed"]),
                        "stalled": len(swept["stalled"]),
                        "stalled_thread_ids": swept["stalled"],
                    },
                )
        except Exception:
            logger.warning("Goal scheduling sweep failed", exc_info=True)
        try:
            environments = getattr(store, "environments", None)
            if environments is not None:
                await environments.maintain_environments()
        except Exception:
            logger.warning("Environment maintenance sweep failed", exc_info=True)
        try:
            actions = await store.attempt_commands.reap_expired(
                max_attempts=(
                    max_attempts
                    if max_attempts is not None
                    else integer_setting("max_attempts", MAX_ATTEMPTS)
                )
            )
            await _cleanup_reaped_shared_residencies(store.host_pool, actions, grants)
            if actions:
                logger.info(
                    "agent_attempts_reaped",
                    extra={"event": "agent_attempts_reaped", "count": len(actions)},
                )
        except Exception:
            logger.warning("Agent attempt reaper pass failed", exc_info=True)
        # Stalled captures, publishes and Forks each in a try of their own: they
        # recover different workspaces, so one that fails on every pass must
        # not stop the others -- or the attempt pass above -- from running.
        try:
            stalled_captures = await store.snapshot.reap_stalled_sandbox_snapshot_captures()
            if stalled_captures:
                logger.warning(
                    "agent_snapshot_captures_stalled",
                    extra={
                        "event": "agent_snapshot_captures_stalled",
                        "snapshot_ids": stalled_captures,
                    },
                )
        except Exception:
            logger.warning("Stalled snapshot capture reaper pass failed", exc_info=True)
        try:
            stalled_snapshot_publishes = await store.snapshot.reap_stalled_snapshot_publishes()
            if stalled_snapshot_publishes:
                logger.warning(
                    "agent_snapshot_publishes_stalled",
                    extra={
                        "event": "agent_snapshot_publishes_stalled",
                        "snapshot_ids": stalled_snapshot_publishes,
                    },
                )
        except Exception:
            logger.warning("Stalled snapshot publish reaper pass failed", exc_info=True)
        try:
            stalled_session_forks = await store.session_fork.reap_stalled_session_forks()
            if stalled_session_forks:
                recovered: list[str] = []
                cleaned: list[str] = []
                for operation in stalled_session_forks:
                    operation_id = str(operation["id"])
                    # Each operation in a try of its own. The failed ones are
                    # never returned again, so an error on one must not leave
                    # the processes and grants of the ones after it running.
                    try:
                        if operation.get("recovery_action") == "finalize":
                            if await store.session_fork.finalize_session_fork(
                                operation_id=operation_id
                            ):
                                recovered.append(operation_id)
                            continue
                        await _cleanup_failed_session_fork(store.host_pool, operation, grants)
                    except Exception:
                        logger.warning(
                            "agent_session_fork_reconcile_failed",
                            extra={
                                "event": "agent_session_fork_reconcile_failed",
                                "operation_id": operation_id,
                            },
                            exc_info=True,
                        )
                        continue
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
            logger.warning("Stalled Fork reaper pass failed", exc_info=True)
        # Abandoned staged uploads count against staging quota until collected.
        # Pending-message and job-claimed rows are kept by the repository
        # predicate. Own try: a failure here must not skip later reaping.
        try:
            await store.attachments.purge_stale_staged(
                older_than_hours=integer_setting(
                    "attachments.staged_retention_hours",
                    STAGED_UPLOAD_RETENTION_HOURS,
                )
            )
        except Exception:
            logger.warning("Staged upload purge failed", exc_info=True)
        # The owner's waiting messages after the attempt pass. A turn the
        # reaper just ended — failed or cancelled — reached no finish route, so
        # nothing else would act on it: this pauses its queue, and sends what a
        # settlement missed, a control plane restarted between a turn's end and
        # its settlement included. The backstop, not the mechanism; in its own
        # try so a queue cannot stop attempts being recovered.
        try:
            await settle_waiting_queues(store, gateway=gateway, github_app=github_app)
        except Exception:
            logger.warning("Pending message queue sweep failed", exc_info=True)
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


async def _recover_send_now_handoffs(store: _Reaping, *, interval_seconds: float) -> None:
    """Recover durable handoffs without delaying attempt or grant safety passes."""
    while True:
        try:
            await recover_handoffs(store)
        except Exception:
            logger.warning("Send-now handoff recovery pass failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def _replay_settled_terminals(store: _SettledTerminals, *, interval_seconds: float) -> None:
    """Replay settled-terminal markers without delaying attempt or grant safety passes.

    Every marker is a round trip to the sandbox manager of the host that ran its turn,
    and a marker stays set until that sandbox manager confirms. A host that stopped
    answering therefore costs each of its markers a full request timeout on
    every pass, and the turns the reaper ends because their host died are
    among the ones that leave markers behind. Inside the reaper's pass that
    wait held up lease reaping, goal scheduling, queue settlement and grant
    renewal for the whole pool.
    """
    while True:
        try:
            await _reconcile_settled_terminals(store)
        except Exception:
            logger.warning("Settled terminal reconciliation pass failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def reap_expired_attempts(
    store: _Reaping,
    *,
    interval_seconds: float = REAP_INTERVAL_S,
    max_attempts: int | None = None,
    grants: Any | None = None,
    github_app: GitHubAppSource | None = None,
    gateway: Any | None = None,
) -> None:
    """Run attempt maintenance alongside independently bounded handoff recovery.

    Send-now handoff recovery and settled-terminal replay each run in an owned
    task, once at startup and then on the same cadence, so a slow host sandbox
    manager cannot hold up attempt reaping, queue settlement, or grant
    revocation and renewal. Cancelling this worker also cancels and joins both
    tasks, including any sandbox manager calls they currently own.
    """
    # Created first, so a restarted control plane still replays settled
    # terminals before anything else, as it did when that replay ran ahead of
    # the first pass.
    terminals = asyncio.create_task(
        _replay_settled_terminals(store, interval_seconds=interval_seconds),
        name="agent-settled-terminal-replay",
    )
    recovery = asyncio.create_task(
        _recover_send_now_handoffs(store, interval_seconds=interval_seconds),
        name="agent-send-now-recovery",
    )
    try:
        await _run_attempt_reaper(
            store,
            interval_seconds=interval_seconds,
            max_attempts=max_attempts,
            grants=grants,
            github_app=github_app,
            gateway=gateway,
        )
    finally:
        recovery.cancel()
        terminals.cancel()
        await asyncio.gather(recovery, terminals, return_exceptions=True)
