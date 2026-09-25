"""What the platform does about a goal, driven by durable obligations.

The decision itself is :func:`cloud_agent.goals.decision.decide` — pure, and
tested on its own. This module is everything around it: it takes a wakeup, reads
the facts, runs the decision, and writes the answer *together with* the wakeup's
completion, in one transaction.

That last clause is the design. The previous version decided inline, after a
finish had already committed, and queued the next round in a separate call: a
process that died in between left an active goal with nothing to run it, and the
only recovery was a timer that paused the goal fifteen minutes later. Here the
obligation is a row, it is consumed under the conversation's lock, and the round
it produces commits with it. A crash at any point leaves either a pending wakeup
(so the decision is made again) or a processed one next to the round it created.

There is no second executor here. A round created this way is an ordinary queued
turn: a host claims it the same way, it carries the same lease and fence, and it
appears in the conversation like any other message — marked as automatic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

from cloud_agent.application.pending_messages import settle_queue
from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.goals.decision import GoalAction, GoalContext, decide
from cloud_agent.goals.models import (
    GOAL_REPORT_RUNTIMES,
    MAX_REASON_CHARS,
    REPORT_REPAIR_RUNTIMES,
    GoalState,
)
from cloud_agent.goals.prompts import render_repair_prompt, render_round_prompt
from cloud_agent.goals.report import GoalReport, GoalReportParse
from cloud_agent.source_control_runtime import SourceControlUnavailable

if TYPE_CHECKING:
    from cloud_agent.source_control_runtime import GitHubAppSource

if TYPE_CHECKING:
    from cloud_agent.application.pending_messages import QueueStorage

logger = logging.getLogger(__name__)

#: Every continuation request carries this prefix on its client request id. The
#: unique index over ``(user_id, client_request_id)`` is what makes a replayed
#: wakeup produce no second round; naming the event that triggered the decision
#: is what makes it correct when a round number is reused after a cancellation.
CLIENT_REQUEST_PREFIX = "goal"


class _GoalStore(Protocol):
    """The parts of the store this module uses, named so that it can be faked."""

    @property
    def goals(self) -> Any:
        """Goal state, fencing and derived counters."""
        ...

    @property
    def goal_wakeups(self) -> Any:
        """The durable scheduling queue, and the transactions over it."""
        ...

    @property
    def conversation_commands(self) -> Any:
        """Appending turns, including automatic rounds."""
        ...

    @property
    def conversation_queries(self) -> Any:
        """Reading turns and their snapshots."""
        ...

    @property
    def source_control(self) -> Any:
        """Repository entitlement."""
        ...

    @property
    def pending_messages(self) -> Any:
        """The owner's waiting messages, which go before any automatic round."""
        ...

    @property
    def attachments(self) -> Any:
        """Uploads a waiting message carries, checked before it is sent."""
        ...

    @property
    def skills(self) -> Any:
        """Skills a waiting message names, checked before it is sent."""
        ...


def client_request_id_for(*, thread_id: str, goal_id: str, kind: str, trigger_ref: str) -> str:
    """The idempotency key of the round one decision would produce.

    Keyed by the *event* that triggered the decision — a finished turn, or an
    owner's write — rather than by a round number. A round number can repeat
    when a queued round was cancelled without being counted, and a key built
    from it would then resolve to the cancelled row and silently queue nothing.
    """
    return f"{CLIENT_REQUEST_PREFIX}:{thread_id}:{goal_id}:{kind}:{trigger_ref}"


def finish_trigger_ref(job_id: str) -> str:
    """The event identity of one turn reaching a terminal state."""
    return f"job:{job_id}"


async def run_goal_wakeups(
    *,
    store: _GoalStore,
    github_app: GitHubAppSource | None = None,
    gateway: Any | None = None,
    limit: int = 8,
) -> int:
    """Consume what is due, and return how many wakeups were handled.

    One pass is bounded, because it runs both inside the finish request and on
    the reaper's loop: a deployment with hundreds of pending obligations must
    not hold a request open, and the next tick takes the rest.

    **A GitHub App this process cannot load stops the pass before it claims
    anything** (issue #47). Claiming spends one of a wakeup's five attempts, and
    with no credentials the entitlement check sees only the allowlist and pauses
    the goal as ``access_denied``; either way a deployment fault would end the
    owner's goal. Unclaimed, the wakeup waits — the goal stays active, and the
    stall sweep leaves a goal with a pending wakeup alone — until a pass finds
    the App loaded.
    """
    try:
        app_credentials = github_app.get() if github_app is not None else None
    except SourceControlUnavailable:
        return 0
    handled = 0
    for wakeup in await store.goal_wakeups.claim_wakeups(limit=limit):
        try:
            await consume_wakeup(
                store=store, wakeup=wakeup, app_credentials=app_credentials, gateway=gateway
            )
        except Exception:
            logger.warning(
                "agent_goal_wakeup_failed",
                extra={
                    "event": "agent_goal_wakeup_failed",
                    "thread_id": wakeup.get("thread_id"),
                    "wakeup_id": wakeup.get("id"),
                },
                exc_info=True,
            )
            await _retry_or_pause(store, wakeup)
        handled += 1
    return handled


async def continue_after_finish(
    *,
    store: _GoalStore,
    job_id: str,
    github_app: GitHubAppSource | None = None,
    gateway: Any | None = None,
) -> int:
    """Drain what a finished turn made due, before the runner is acknowledged.

    The obligation itself is already durable — ``arm_finish_wakeup`` wrote it
    inside the terminal transition, and released every wakeup that was parked
    behind this turn. This is the part that cannot wait for the next tick: the
    owner's page watches the newest turn, and a window in which the conversation
    looks finished would stop it polling, so the round the user is waiting to
    see would be the one round nobody ever fetched.

    ``job_id`` names the turn for the log line; the wakeups themselves carry
    their own identity, which is what makes a replayed finish harmless.
    """
    logger.info(
        "agent_goal_finish_drain",
        extra={"event": "agent_goal_finish_drain", "job_id": job_id},
    )
    return await run_goal_wakeups(store=store, github_app=github_app, gateway=gateway, limit=8)


async def consume_wakeup(
    *,
    store: _GoalStore,
    wakeup: dict[str, Any],
    app_credentials: Any | None = None,
    gateway: Any | None = None,
) -> str:
    """Judge one goal and write what follows, atomically.

    Returns the decision's kind, for the log line and for the tests that assert
    on it. Never raises for a business reason: a goal that cannot continue is
    paused, not turned into a failed request.

    The owner's waiting messages are sent before anything is decided: a
    message the owner queued goes before a round the goal would add, and the
    goal is judged again when that message's turn has ended. ``gateway`` is
    what checking such a message against the turn it follows asks.
    """
    thread_id = str(wakeup["thread_id"])
    goal = await store.goals.get_goal(thread_id=thread_id)
    if goal is None:
        # Cleared, or never really there. Nothing to decide, and nothing that
        # could authorize work.
        await store.goal_wakeups.finish_wakeup(
            wakeup_id=str(wakeup["id"]), processed=True, detail="no goal"
        )
        return "none"
    if str(wakeup.get("goal_id") or "") != str(goal.get("goal_id") or ""):
        # The objective was replaced under this obligation: the wakeup belonged
        # to a goal that no longer exists.
        await store.goal_wakeups.finish_wakeup(
            wakeup_id=str(wakeup["id"]), processed=True, detail="superseded goal"
        )
        return "none"
    if int(wakeup.get("revision") or 0) != int(goal.get("revision") or 0):
        # The goal moved on — edited, paused, resumed or completed. Whatever
        # this wakeup was going to say has already been said by that write.
        await store.goal_wakeups.finish_wakeup(
            wakeup_id=str(wakeup["id"]), processed=True, detail="superseded revision"
        )
        return "none"
    if goal.get("state") != "active":
        await store.goal_wakeups.finish_wakeup(
            wakeup_id=str(wakeup["id"]), processed=True, detail=f"goal {goal.get('state')}"
        )
        return "none"
    # A person's waiting message goes first. The settlement is the same one
    # every other entry point runs; when it sends a message, this obligation
    # waits behind that turn exactly as it waits behind any person's turn.
    settled = await settle_queue(
        cast("QueueStorage", store),
        thread_id=thread_id,
        gateway=gateway,
        app_credentials=app_credentials,
    )
    if settled.job is not None:
        await _park(store, wakeup=wakeup, job_id=str(settled.job["id"]))
        return "wait"
    owner_triggered = wakeup.get("trigger") == "owner"
    if owner_triggered:
        # The owner asked for this. Their authorization is the trigger, and the
        # last turn's *outcome* is not judged again: resuming a goal paused by a
        # cancelled round would otherwise read that cancelled round and pause it
        # straight back, which makes the only Resume button in the product a
        # no-op.
        #
        # The conversation is still read, because whether work may start at all
        # is a fact about the conversation and not about the last turn's result:
        # an archived conversation, a lost session and a repository the owner
        # can no longer reach all stop a resume too, and each has to say so.
        queued = await store.goals.live_turn_id(thread_id=thread_id)
        if queued is not None:
            await _park(store, wakeup=wakeup, job_id=queued)
            return "wait"
        turn = await store.conversation_queries.latest_turn(thread_id=thread_id)
        context = _context_for(goal=goal, turn=turn, owner_triggered=True)
    else:
        turn = await _turn_to_judge(store=store, wakeup=wakeup)
        if turn is not None and turn.get("state") in ("queued", "running"):
            # A turn is already live and it is not a settled outcome to judge.
            # This is the ordinary "a person is waiting" answer, and the turn's
            # own finish re-arms this wakeup.
            await _park(store, wakeup=wakeup, job_id=str(turn["id"]))
            return "wait"
        context = _context_for(goal=goal, turn=turn)
    # A read that may reach the network happens *before* the decision's
    # transaction opens. Holding the conversation's lock across a GitHub call
    # would serialize every finish in the deployment behind one outage.
    blocked = await _check_access(store, turn, app_credentials=app_credentials)
    if blocked is not None:
        return await _stop(
            store,
            wakeup=wakeup,
            thread_id=thread_id,
            goal=goal,
            action=blocked,
            context=context,
        )
    return await _apply(
        store=store,
        wakeup=wakeup,
        thread_id=thread_id,
        goal=goal,
        turn=turn,
        context=context,
    )


async def _park(store: _GoalStore, *, wakeup: dict[str, Any], job_id: str) -> None:
    """Wait behind a turn, and make sure the wait is registered exactly once.

    The registration is a second write, so it can lose a race with the very
    turn it names: read "still running", the turn finishes, its finish finds
    nothing parked to release, and this then attaches the wakeup to a job that
    has already ended. The sweep repairs exactly that shape — it re-arms a
    pending wakeup whose waiting turn has settled — and it runs every reaper
    tick, so the worst case is a delay rather than a lost goal. Parking is still
    worth doing: it is what makes the common case immediate.
    """
    live = await store.goals.live_turn_id(thread_id=str(wakeup["thread_id"]), conn=None)
    if live is None or live != job_id:
        # The turn ended between the read that chose it and this write. Nothing
        # is left to wait behind, so the decision is made again rather than
        # parked on a dead job.
        await store.goal_wakeups.release_wakeup(
            wakeup_id=str(wakeup["id"]),
            detail="the turn it was waiting for had already ended",
        )
        return
    await store.goal_wakeups.release_wakeup(
        wakeup_id=str(wakeup["id"]),
        waiting_job_id=job_id,
        detail="waiting for the turn already in this conversation",
    )


async def _turn_to_judge(*, store: _GoalStore, wakeup: dict[str, Any]) -> dict[str, Any] | None:
    """The turn this decision is about.

    A wakeup parked behind a turn is about that turn. Everything else is about
    the newest turn of the conversation, whatever state it is in — a running
    turn means wait, and the newest finished one is what the owner is looking
    at.
    """
    waiting_job_id = wakeup.get("waiting_job_id")
    if waiting_job_id is not None:
        return await store.conversation_queries.get_job(str(waiting_job_id))
    return await store.conversation_queries.latest_turn(thread_id=str(wakeup["thread_id"]))


def _context_for(
    goal: dict[str, Any],
    turn: dict[str, Any] | None,
    *,
    owner_triggered: bool = False,
) -> GoalContext:
    """Everything the decision needs, from the goal and the turn it judges.

    A *person's* turn never carries a report: the completion protocol belongs to
    the automatic rounds the platform asked for. A person's turn is judged for
    what it means for scheduling — usually "carry on" — and the goal is
    completed only by a round that was run to work on it.

    ``owner_triggered`` goes further and neutralises the turn's *result* as
    well: a resume is authorized by the owner rather than by how the last turn
    ended, so that turn cannot pause, stop or continue anything. Everything the
    conversation itself contributes — the runtime that will run the round, and
    the eligibility the caller checks separately — still applies.
    """
    runtime = str((turn or {}).get("runtime") or "")
    if turn is None:
        # Nothing has ever run here: a goal set on a conversation that has not
        # started has to schedule its first round.
        return GoalContext(
            state=cast("GoalState", goal["state"]),
            goal_id=str(goal["goal_id"]),
            revision=int(goal["revision"]),
            max_rounds=_max_rounds(goal),
            rounds_used=int(goal["rounds_used"] or 0),
            turn_job_id="",
            turn_goal_id=None,
            turn_revision=None,
            turn_state="succeeded",
            turn_round_kind=None,
            report_parse=None,
            repairs_used=int(goal.get("repairs_used") or 0),
        )
    is_goal_round = turn.get("goal_round_kind") is not None and not owner_triggered
    parse_result = cast("GoalReportParse", turn["goal_report_parse"]) if is_goal_round else None
    if not owner_triggered and turn["state"] == "failed" and parse_result in (None, "absent"):
        # A failed turn's message is not evidence that the model said nothing:
        # it is a turn that did not finish saying it.
        #
        # An owner trigger is the exception, and it is the same exception as the
        # report above: "the last turn failed" is a result, and a resume is not
        # a verdict on it. Without this the goal paused on the very turn the
        # owner resumed from, which is the no-op Resume button again, one
        # failure mode over.
        parse_result = "unavailable"
    report = None
    if parse_result == "valid":
        outcome = turn.get("goal_report_outcome")
        if outcome in ("complete", "needs_input"):
            report = GoalReport(outcome=outcome, reason=str(turn.get("goal_report_reason") or ""))
    return GoalContext(
        state=cast("GoalState", goal["state"]),
        goal_id=str(goal["goal_id"]),
        revision=int(goal["revision"]),
        max_rounds=_max_rounds(goal),
        rounds_used=int(goal["rounds_used"] or 0),
        turn_job_id=str(turn["id"]),
        turn_goal_id=turn.get("goal_id"),
        turn_revision=turn.get("round_goal_revision"),
        # The owner's authorization stands in for the turn's outcome; only the
        # turn's report and result are ignored, never the conversation's own
        # eligibility, which the caller checks separately.
        turn_state="succeeded" if owner_triggered else str(turn["state"]),
        turn_round_kind=None if owner_triggered else turn.get("goal_round_kind"),
        report_parse=parse_result,
        report=report,
        report_detail=turn.get("goal_report_detail"),
        turn_detail=turn.get("detail"),
        thread_has_queued_turn=False,
        # A person's message since this turn was created means the turn's own
        # outcome is no longer current — that is what stops a completion from
        # finishing a goal the owner has moved past. An *owner trigger* is the
        # opposite: the newer input it sees is the authorization it is acting
        # on, and there is no report to judge, so it does not stand in the way
        # of scheduling the work that authorization asked for.
        thread_has_newer_input=_has_newer_input(turn) and not owner_triggered,
        repairs_used=int(goal.get("repairs_used") or 0),
        repair_supported=_repair_supported(runtime),
        runtime_supported=runtime in GOAL_REPORT_RUNTIMES,
    )


def _max_rounds(goal: dict[str, Any]) -> int | None:
    """The owner's own round bound, or ``None`` when they stated none.

    One reader, so that "no limit" cannot be spelled two ways. The store hands
    the column back as ``None`` for a goal nobody bounded — the ordinary case —
    and that ``None`` travels to the decision unchanged. A malformed value is
    treated as absent rather than guessed at: the alternative is inventing a
    bound the owner never set.
    """
    value = goal.get("max_rounds")
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _has_newer_input(turn: dict[str, Any]) -> bool:
    """Whether a person has spoken since this turn was created.

    The thread's counter only a person's message advances; every turn stamps
    the value it saw. This is the check that stops a completion claim decided
    before the owner's message from finishing the goal after it.
    """
    stamped = int(turn.get("input_seq_at_create") or 0)
    current = int(turn.get("thread_user_input_seq") or 0)
    return current > stamped


def _repair_supported(runtime: str) -> bool:
    """Whether this runtime can be held to a repair round that does no work.

    A verified-version list, not an assumption: a runtime that has not been
    shown to accept a tool-free turn gets no repair round, and a malformed
    report pauses instead. An unverified repair is not a repair, it is another
    work round with a nicer name.
    """
    return runtime in REPORT_REPAIR_RUNTIMES


async def _check_access(
    store: _GoalStore,
    turn: dict[str, Any] | None,
    *,
    app_credentials: Any | None = None,
) -> GoalAction | None:
    """The one eligibility read that may reach outside the database.

    ``turn`` arrives in one of two shapes — ``latest_turn``'s job-summary
    projection, which carries the thread's ``archived_at``, or the parked
    path's ``get_job``, which carries that *and* the retained sandbox — so the
    two conditions below are live in both, and the sandbox one is the belt to
    the archive route's braces.
    """
    if turn is None:
        return None
    if turn.get("archived_at") is not None:
        return GoalAction(
            kind="pause",
            reason="archived",
            message="the conversation was archived; resume the goal to continue it",
        )
    retained = turn.get("thread_sandbox")
    if isinstance(retained, dict) and (
        retained.get("context_status") == "lost"
        or retained.get("state") in {"stopped_dirty", "lost"}
    ):
        return GoalAction(
            kind="pause",
            reason="context_lost",
            message=("the conversation's session is gone; it cannot be resumed from a transcript"),
        )
    try:
        # The caller's own credential minter, not ``None``: with no App
        # credentials the check can only see the deployment's allowlist, so a
        # repository the owner legitimately installed the App on is reported as
        # access denied. This runs on the request path and on the background
        # consumer, and both have the same minter the rest of the service uses.
        await require_entitled_repo(
            turn["repo"],
            turn["user_id"],
            store=store.source_control,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        logger.warning(
            "agent_goal_repo_not_allowed",
            extra={
                "event": "agent_goal_repo_not_allowed",
                "thread_id": turn["thread_id"],
                "job_id": turn["id"],
                "detail": str(exc),
            },
        )
        return GoalAction(
            kind="pause",
            reason="access_denied",
            message="this conversation no longer has access to its repository",
        )
    return None


async def _apply(
    *,
    store: _GoalStore,
    wakeup: dict[str, Any],
    thread_id: str,
    goal: dict[str, Any],
    turn: dict[str, Any] | None,
    context: GoalContext,
) -> str:
    """Run the decision and write what it says, with the wakeup, in one commit."""
    action = decide(context)
    if action.kind == "none":
        await store.goal_wakeups.finish_wakeup(
            wakeup_id=str(wakeup["id"]), processed=True, detail="nothing to do"
        )
        return action.kind
    if action.kind in ("queue_work_round", "queue_repair_round"):
        return await _queue_round(
            store=store,
            wakeup=wakeup,
            thread_id=thread_id,
            goal=goal,
            context=context,
            repair=action.kind == "queue_repair_round",
        )
    return await _stop(
        store,
        wakeup=wakeup,
        thread_id=thread_id,
        goal=goal,
        action=action,
        context=context,
    )


async def _stop(
    store: _GoalStore,
    *,
    wakeup: dict[str, Any],
    thread_id: str,
    goal: dict[str, Any],
    action: GoalAction,
    context: GoalContext,
) -> str:
    """Apply a terminal decision and finish the wakeup in the same commit.

    The decision was made outside this transaction, so everything it depended on
    is checked again inside it — including the things an owner's message changes,
    which the goal's revision does not. A completion in particular: a person who
    writes while the decision is being made has *moved on* from the round it
    would finish, and finishing the goal over their message is the one outcome
    the whole "user input wins" rule exists to prevent.
    """
    detail = (action.message or "")[:MAX_REASON_CHARS] or None
    decision = store.goal_wakeups.transaction()
    async with decision as conn:
        stale = await _superseded_in_transaction(store, conn=conn, thread_id=thread_id, goal=goal)
        if stale is None and action.kind == "complete":
            stale = await _completion_superseded(
                store, conn=conn, thread_id=thread_id, context=context
            )
        if stale is not None:
            # The owner got there first. Their write stands, and this decision
            # is stale — recorded as such rather than retried.
            await store.goal_wakeups.finish_wakeup(
                wakeup_id=str(wakeup["id"]), processed=True, detail=stale, conn=conn
            )
            return "none"
        written = await store.goals.apply_action(
            thread_id=thread_id,
            revision=int(goal["revision"]),
            action=action,
            pause_reason=action.reason,
            pause_detail=detail,
            completed_by_job_id=_completed_by(wakeup),
            conn=conn,
        )
        if written is None:
            await store.goal_wakeups.finish_wakeup(
                wakeup_id=str(wakeup["id"]), processed=True, detail="superseded", conn=conn
            )
            return "none"
        await store.goal_wakeups.finish_wakeup(
            wakeup_id=str(wakeup["id"]), processed=True, detail=action.kind, conn=conn
        )
    logger.info(
        f"agent_goal_{action.kind}",
        extra={
            "event": f"agent_goal_{action.kind}",
            "thread_id": thread_id,
            "reason": action.reason or action.completion_basis,
        },
    )
    return action.kind


async def _superseded_in_transaction(
    store: _GoalStore, *, conn: Any, thread_id: str, goal: dict[str, Any]
) -> str | None:
    """Why this decision no longer applies, or ``None`` when it still does.

    The goal's identity, its revision and its state are the three things an
    owner write moves; a check that finds all three unchanged has proved the
    goal did not move under the decision.
    """
    current = await store.goals.lock_goal_state(thread_id=thread_id, conn=conn)
    if current is None:
        return "goal gone"
    if str(current.get("goal_id") or "") != str(goal["goal_id"]):
        return "superseded goal"
    if int(current.get("revision") or 0) != int(goal["revision"]):
        return "superseded revision"
    if current.get("state") != "active":
        return f"goal {current.get('state')}"
    return None


async def _completion_superseded(
    store: _GoalStore, *, conn: Any, thread_id: str, context: GoalContext
) -> str | None:
    """Whether a person has spoken since the completed round was created.

    A message does not move the goal's revision, so the fence above cannot see
    it. The conversation's own counter can: every person's turn advances it, and
    every turn records the value it saw. A completion whose round is older than
    the counter is a completion of a conversation that no longer exists.
    """
    if context.turn_job_id == "":
        return "no round to complete"
    turn = await store.conversation_queries.get_job_for_update(
        conn=conn, job_id=context.turn_job_id
    )
    if turn is None:
        return "the round is gone"
    if turn.get("state") != "succeeded":
        return f"the round ended {turn.get('state')}"
    if str(turn.get("goal_report_outcome") or "") != "complete":
        return "the round no longer reports completion"
    if _has_newer_input(turn):
        return "the owner has spoken since"
    if await store.goals.has_pending_turn(thread_id=thread_id, conn=conn):
        return "a turn is waiting"
    return None


def _completed_by(wakeup: dict[str, Any]) -> str | None:
    """Which turn earned a completion.

    Only a *finish* wakeup can produce one: an owner's write is an authorization
    to work, never the evidence that work finished.
    """
    if wakeup.get("trigger") != "finish":
        return None
    ref = str(wakeup.get("trigger_ref") or "")
    return ref[len("job:") :] if ref.startswith("job:") else None


async def _queue_round(
    *,
    store: _GoalStore,
    wakeup: dict[str, Any],
    thread_id: str,
    goal: dict[str, Any],
    context: GoalContext,
    repair: bool,
) -> str:
    """Create the next automatic round inside the wakeup's own transaction."""
    kind = "repair" if repair else "work"
    round_number = context.rounds_used + 1
    if repair:
        prompt = render_repair_prompt(
            objective=str(goal["objective"]), detail=context.report_detail
        )
    else:
        prompt = render_round_prompt(
            objective=str(goal["objective"]),
            round_number=round_number,
            max_rounds=_max_rounds(goal),
            previous=context.turn_detail or (context.report.reason if context.report else None),
        )
    trigger_ref = str(wakeup["trigger_ref"])
    decision = store.goal_wakeups.transaction()
    async with decision as conn:
        # Re-read under the lock this transaction holds: between the read that
        # built the context and this write, the owner can have paused the goal,
        # replaced the objective, or sent a message.
        stale = await _superseded_in_transaction(store, conn=conn, thread_id=thread_id, goal=goal)
        if stale is not None:
            await store.goal_wakeups.finish_wakeup(
                wakeup_id=str(wakeup["id"]), processed=True, detail=stale, conn=conn
            )
            return "none"
        if await store.goals.has_pending_turn(thread_id=thread_id, conn=conn):
            # A person's message arrived while this decision was being made. It
            # goes first; this wakeup waits behind it.
            await store.goal_wakeups.release_wakeup(
                wakeup_id=str(wakeup["id"]),
                waiting_job_id=await store.goals.live_turn_id(thread_id=thread_id, conn=conn),
                detail="a person's turn arrived first",
                conn=conn,
            )
            return "wait"
        created = await store.conversation_commands.create_goal_round(
            conn=conn,
            thread_id=thread_id,
            goal_id=str(goal["goal_id"]),
            revision=int(goal["revision"]),
            kind=kind,
            round_number=round_number,
            prompt=prompt,
            client_request_id=client_request_id_for(
                thread_id=thread_id,
                goal_id=str(goal["goal_id"]),
                kind=kind,
                trigger_ref=trigger_ref,
            ),
            client_request_fingerprint=f"goal-round:{kind}:{round_number}",
            wakeup_id=str(wakeup["id"]),
        )
    if created is None:
        # This event already produced a round. Nothing to log as new work.
        logger.info(
            "agent_goal_round_replayed",
            extra={"event": "agent_goal_round_replayed", "thread_id": thread_id},
        )
        return "none"
    logger.info(
        "agent_goal_round_queued",
        extra={
            "event": "agent_goal_round_queued",
            "thread_id": thread_id,
            "job_id": created["id"],
            "goal_id": goal["goal_id"],
            "goal_revision": goal["revision"],
            "round_kind": kind,
        },
    )
    return f"queue_{kind}_round"


async def _retry_or_pause(store: _GoalStore, wakeup: dict[str, Any]) -> None:
    """After a scheduling failure: back off, or stop the goal and say so.

    A goal that cannot be scheduled must not sit behind a green "active" badge
    while the platform retries forever. It gets a bounded number of attempts,
    and then the owner is told.
    """
    from cloud_agent.storage.repositories.goal_wakeups import is_exhausted, wakeup_backoff

    attempts = int(wakeup.get("attempts") or 0)
    if not is_exhausted(attempts):
        await store.goal_wakeups.release_wakeup(
            wakeup_id=str(wakeup["id"]),
            retry_in_seconds=wakeup_backoff(attempts),
            detail="scheduling failed; retrying",
        )
        return
    goal = await store.goals.get_goal(thread_id=str(wakeup["thread_id"]))
    if goal is None or goal.get("state") != "active":
        await store.goal_wakeups.finish_wakeup(
            wakeup_id=str(wakeup["id"]), processed=True, detail="scheduling failed"
        )
        return
    await _stop(
        store,
        wakeup=wakeup,
        thread_id=str(wakeup["thread_id"]),
        goal=goal,
        action=GoalAction(
            kind="pause",
            reason="scheduling_failed",
            message="the platform could not schedule the next round; resume to try again",
        ),
        context=_context_for(goal=goal, turn=None),
    )


__all__ = [
    "CLIENT_REQUEST_PREFIX",
    "client_request_id_for",
    "consume_wakeup",
    "continue_after_finish",
    "finish_trigger_ref",
    "run_goal_wakeups",
]
