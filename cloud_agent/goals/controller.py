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
from typing import Any, Protocol, cast

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
    app_credentials: Any | None = None,
    limit: int = 8,
) -> int:
    """Consume what is due, and return how many wakeups were handled.

    One pass is bounded, because it runs both inside the finish request and on
    the reaper's loop: a deployment with hundreds of pending obligations must
    not hold a request open, and the next tick takes the rest.
    """
    handled = 0
    for wakeup in await store.goal_wakeups.claim_wakeups(limit=limit):
        try:
            await consume_wakeup(store=store, wakeup=wakeup, app_credentials=app_credentials)
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
    app_credentials: Any | None = None,
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
    return await run_goal_wakeups(store=store, app_credentials=app_credentials, limit=8)


async def consume_wakeup(
    *,
    store: _GoalStore,
    wakeup: dict[str, Any],
    app_credentials: Any | None = None,
) -> str:
    """Judge one goal and write what follows, atomically.

    Returns the decision's kind, for the log line and for the tests that assert
    on it. Never raises for a business reason: a goal that cannot continue is
    paused, not turned into a failed request.
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
    turn = await _turn_to_judge(store=store, wakeup=wakeup)
    if turn is not None and turn.get("state") in ("queued", "running"):
        # A turn is already live and it is not a settled outcome to judge. This
        # is the ordinary "a person is waiting" answer, and the turn's own
        # finish re-arms this wakeup.
        await store.goal_wakeups.release_wakeup(
            wakeup_id=str(wakeup["id"]),
            waiting_job_id=str(turn["id"]),
            detail="waiting for the turn already in this conversation",
        )
        return "wait"
    context = _context_for(goal=goal, turn=turn)
    # A read that may reach the network happens *before* the decision's
    # transaction opens. Holding the conversation's lock across a GitHub call
    # would serialize every finish in the deployment behind one outage.
    blocked = await _check_access(store, turn)
    if blocked is not None:
        return await _stop(store, wakeup=wakeup, thread_id=thread_id, goal=goal, action=blocked)
    return await _apply(
        store=store,
        wakeup=wakeup,
        thread_id=thread_id,
        goal=goal,
        turn=turn,
        context=context,
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


def _context_for(goal: dict[str, Any], turn: dict[str, Any] | None) -> GoalContext:
    """Everything the decision needs, from the goal and the turn it judges.

    A *person's* turn never carries a report: the completion protocol belongs to
    the automatic rounds the platform asked for. A person's turn is judged for
    what it means for scheduling — usually "carry on" — and the goal is
    completed only by a round that was run to work on it.
    """
    runtime = str((turn or {}).get("runtime") or "")
    if turn is None:
        # Nothing has ever run here: a goal set on a conversation that has not
        # started has to schedule its first round.
        return GoalContext(
            state=cast("GoalState", goal["state"]),
            goal_id=str(goal["goal_id"]),
            revision=int(goal["revision"]),
            max_rounds=int(goal["max_rounds"]),
            rounds_used=int(goal["rounds_used"] or 0),
            turn_job_id="",
            turn_goal_id=None,
            turn_revision=None,
            turn_state="succeeded",
            turn_round_kind=None,
            report_parse=None,
            repairs_used=int(goal.get("repairs_used") or 0),
        )
    is_goal_round = turn.get("goal_round_kind") is not None
    parse_result = cast("GoalReportParse", turn["goal_report_parse"]) if is_goal_round else None
    if turn["state"] == "failed" and parse_result in (None, "absent"):
        # A failed turn's message is not evidence that the model said nothing:
        # it is a turn that did not finish saying it.
        parse_result = "unavailable" if turn["state"] == "failed" else parse_result
    report = None
    if parse_result == "valid":
        outcome = turn.get("goal_report_outcome")
        if outcome in ("complete", "needs_input"):
            report = GoalReport(outcome=outcome, reason=str(turn.get("goal_report_reason") or ""))
    return GoalContext(
        state=cast("GoalState", goal["state"]),
        goal_id=str(goal["goal_id"]),
        revision=int(goal["revision"]),
        max_rounds=int(goal["max_rounds"]),
        rounds_used=int(goal["rounds_used"] or 0),
        turn_job_id=str(turn["id"]),
        turn_goal_id=turn.get("goal_id"),
        turn_revision=turn.get("round_goal_revision"),
        turn_state=str(turn["state"]),
        turn_round_kind=turn.get("goal_round_kind"),
        report_parse=parse_result,
        report=report,
        report_detail=turn.get("goal_report_detail"),
        turn_detail=turn.get("detail"),
        thread_has_queued_turn=False,
        thread_has_newer_input=_has_newer_input(turn),
        repairs_used=int(goal.get("repairs_used") or 0),
        repair_supported=_repair_supported(runtime),
        runtime_supported=runtime in GOAL_REPORT_RUNTIMES,
    )


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


async def _check_access(store: _GoalStore, turn: dict[str, Any] | None) -> GoalAction | None:
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
        await require_entitled_repo(
            turn["repo"],
            turn["user_id"],
            store=store.source_control,
            app_credentials=None,
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
    return await _stop(store, wakeup=wakeup, thread_id=thread_id, goal=goal, action=action)


async def _stop(
    store: _GoalStore,
    *,
    wakeup: dict[str, Any],
    thread_id: str,
    goal: dict[str, Any],
    action: GoalAction,
) -> str:
    """Apply a terminal decision and finish the wakeup in the same commit."""
    detail = (action.message or "")[:MAX_REASON_CHARS] or None
    decision = store.goal_wakeups.transaction()
    async with decision as conn:
        current = await store.goals.lock_goal_state(thread_id=thread_id, conn=conn)
        if (
            current is None
            or str(current.get("goal_id") or "") != str(goal["goal_id"])
            or int(current.get("revision") or 0) != int(goal["revision"])
            or current.get("state") != "active"
        ):
            # The owner got there first. Their write stands, and this decision
            # is stale — recorded as such rather than retried.
            await store.goal_wakeups.finish_wakeup(
                wakeup_id=str(wakeup["id"]), processed=True, detail="superseded", conn=conn
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
            max_rounds=int(goal["max_rounds"]),
            previous=context.turn_detail or (context.report.reason if context.report else None),
        )
    trigger_ref = str(wakeup["trigger_ref"])
    decision = store.goal_wakeups.transaction()
    async with decision as conn:
        # Re-read under the lock this transaction holds: between the read that
        # built the context and this write, the owner can have paused the goal,
        # replaced the objective, or sent a message.
        current = await store.goals.lock_goal_state(thread_id=thread_id, conn=conn)
        if (
            current is None
            or str(current.get("goal_id") or "") != str(goal["goal_id"])
            or int(current.get("revision") or 0) != int(goal["revision"])
            or current.get("state") != "active"
        ):
            await store.goal_wakeups.finish_wakeup(
                wakeup_id=str(wakeup["id"]), processed=True, detail="superseded", conn=conn
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
    )


__all__ = [
    "CLIENT_REQUEST_PREFIX",
    "client_request_id_for",
    "consume_wakeup",
    "continue_after_finish",
    "finish_trigger_ref",
    "run_goal_wakeups",
]
