"""What the platform does once a turn has actually ended.

The decision itself is :func:`cloud_agent.goals.decision.decide` — pure, and
tested on its own. This module is the I/O around it: read the facts, write the
answer, and queue the next round if that is what the answer was.

It runs *synchronously* inside ``worker_finish``, after the terminal transition
has committed and before the runner's acknowledgement is returned. That is not
an optimization. The owner's page watches the newest turn; if the next round did
not exist yet when the finish returned, there would be a window in which the
conversation looks finished and the page stops polling — so the round the user
is waiting to see would be the one round nobody ever fetched.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.goals.decision import GoalContext, decide
from cloud_agent.goals.prompts import render_round_prompt

if TYPE_CHECKING:
    from cloud_agent.goals.models import (
        GoalBlockedCode,
        GoalPauseReason,
        GoalReportStatus,
        GoalRoundKind,
    )
    from cloud_agent.ports.conversation_commands import ConversationCommands
    from cloud_agent.ports.conversation_queries import ConversationQueries
    from cloud_agent.ports.goals import GoalRepository
    from cloud_agent.ports.source_control import SourceControlRepository

logger = logging.getLogger(__name__)

#: Every continuation request carries this prefix on its client request id. The
#: unique index over ``(user_id, client_request_id)`` is what makes a replayed
#: finish produce no second round; naming the parent turn in the key is what
#: makes it correct when a round number is reused after a cancelled one.
CLIENT_REQUEST_PREFIX = "goal"

#: Why a round could not be queued, and therefore why the goal stops.
_PAUSE_FOR_ROUND: dict[str, GoalPauseReason] = {
    "session_archived": "archived",
    "sandbox_context_lost": "context-lost",
}


def client_request_id_for(*, thread_id: str, revision: int, kind: str, parent_job_id: str) -> str:
    """The idempotency key of the round a given parent turn would produce.

    Keyed by the *parent job id* rather than by a round number. One finished
    turn makes exactly one continuation decision, so replaying that finish must
    find the round it already made — while a round number can repeat when a
    queued round was cancelled without being counted, and a key built from it
    would then resolve to the cancelled row and silently queue nothing.
    """
    return f"{CLIENT_REQUEST_PREFIX}:{thread_id}:{revision}:{kind}:{parent_job_id}"


async def maybe_continue_goal(
    *,
    conversations: ConversationCommands,
    queries: ConversationQueries,
    source_control: SourceControlRepository,
    job_id: str,
    goals: GoalRepository | None = None,
    app_credentials: Any | None = None,
    assume_report: str | None = None,
) -> str:
    """Decide, write and queue the next step of a conversation's goal.

    Returns the action's ``kind`` — for the log line, and for the tests that
    assert on it. Never raises for a business reason: a goal that cannot
    continue is paused, not turned into a failed finish. The worker's terminal
    transition has already committed by the time this runs, and an error here
    must not tell the runner its finish was rejected.

    ``goals`` is the goal store. A deployment assembled without one — and the
    fakes in tests that predate this feature — simply has no goals to continue,
    which is a successful no-op rather than an error.

    ``assume_report`` replaces the report the named turn made. It exists for one
    caller: an owner resuming a paused goal, where the turn the decision hangs
    from is chosen for its *parentage* — the last turn that actually ran — and
    the outcome being continued is "the owner asked for this", not whatever that
    turn happened to say.
    """
    if goals is None:
        return "none"
    goal = await goals.get_goal_for_job(job_id=job_id)
    if goal is None:
        return "none"
    turn = await queries.get_job(job_id)
    if turn is None:
        return "none"
    context = GoalContext(
        state=goal["state"],
        objective=goal["objective"],
        revision=goal["revision"],
        max_rounds=goal["max_rounds"],
        rounds_used=goal["rounds_used"],
        blocked_streak=goal["blocked_streak"],
        turn_state=str(turn["state"]),
        turn_source=str(turn.get("turn_source") or "human"),
        turn_round_kind=cast("GoalRoundKind | None", turn.get("goal_round_kind")),
        report=cast(
            "GoalReportStatus | None",
            assume_report if assume_report is not None else turn.get("goal_report"),
        ),
        # The turn's own failure detail explains a failed turn; its goal reason
        # explains everything else. Only one of the two is ever present.
        report_reason=turn.get("goal_reason") or turn.get("detail"),
        thread_has_queued_turn=False,
    )
    if context.state == "active":
        context = replace(
            context,
            thread_has_queued_turn=await goals.has_pending_turn(thread_id=str(turn["thread_id"])),
        )
    action = decide(context)
    # The blocked streak is written back before anything else it implies. A
    # ``blocked`` report the platform has not accepted yet still has to be
    # *counted*, or the next turn's report would look like the first one and
    # the third would never arrive. It is the only column a continuing action
    # touches: the revision does not move, because continuing is not a change
    # to what the goal is.
    #
    # The goal is then re-read, so the action below — which, on the third
    # consecutive ``blocked``, is the block itself — is fenced against the
    # revision this write left rather than the one it started from.
    if action.blocked_streak != context.blocked_streak:
        written = await goals.apply_action(
            thread_id=str(turn["thread_id"]),
            revision=goal["revision"],
            action=action,
        )
        if written is None:
            # The owner got there first — paused, edited or cleared the goal
            # while this turn was ending. Their write is the one that stands,
            # and there is nothing left for this turn to decide.
            return "none"
        goal = await goals.get_goal_for_job(job_id=job_id) or written

    if action.kind in ("queue_work_round", "queue_verify_round"):
        created = await _queue_round(
            goals=goals,
            conversations=conversations,
            source_control=source_control,
            job=turn,
            goal=goal,
            rounds_used=context.rounds_used,
            kind="verify" if action.kind == "queue_verify_round" else "work",
            app_credentials=app_credentials,
        )
        if created is not None:
            logger.info(
                "agent_goal_round_queued",
                extra={
                    "event": "agent_goal_round_queued",
                    "thread_id": turn["thread_id"],
                    "job_id": created["id"],
                    "parent_job_id": job_id,
                    "goal_revision": goal["revision"],
                    "round_kind": created.get("goal_round_kind"),
                },
            )
        return action.kind

    if action.kind == "none":
        # Either the goal is not active, or a person's turn is already waiting
        # and will be judged when it ends.
        return action.kind
    written = await goals.apply_action(
        thread_id=str(turn["thread_id"]),
        revision=goal["revision"],
        action=action,
        # The decision's own vocabulary, narrowed here rather than widened in
        # the port: ``decide`` is the only producer of these two codes, and the
        # schema's CHECK constraints are what stop a third from being invented.
        pause_reason=(
            cast("GoalPauseReason", action.reason_code) if action.kind == "pause" else None
        ),
        blocked_code=(
            cast("GoalBlockedCode", action.reason_code) if action.kind == "block" else None
        ),
    )
    if written is not None:
        logger.info(
            f"agent_goal_{action.kind}",
            extra={
                "event": f"agent_goal_{action.kind}",
                "thread_id": turn["thread_id"],
                "job_id": job_id,
                "reason": action.reason_code,
            },
        )
    return action.kind


async def _queue_round(
    *,
    goals: GoalRepository,
    conversations: ConversationCommands,
    source_control: SourceControlRepository,
    job: dict[str, Any],
    goal: dict[str, Any],
    rounds_used: int,
    kind: str,
    app_credentials: Any | None,
) -> dict[str, Any] | None:
    """Create the next automatic round, or pause the goal for a real reason.

    The three checks below are the follow-up route's own, repeated here because
    the platform is now acting as the client: an archived conversation, a
    conversation whose sandbox context is gone, and a repository the owner is no
    longer entitled to are all conditions under which queueing a round would
    create a turn that can never run. Each one pauses instead of retrying, so
    the owner is told which of them happened rather than watching a goal that
    silently stopped.
    """
    thread_id = str(job["thread_id"])
    if job.get("archived_at") is not None:
        await _pause(goals, thread_id=thread_id, revision=goal["revision"], reason="archived")
        return None
    retained = job.get("thread_sandbox")
    if isinstance(retained, dict) and (
        retained.get("context_status") == "lost"
        or retained.get("state") in {"stopped_dirty", "lost"}
    ):
        await _pause(goals, thread_id=thread_id, revision=goal["revision"], reason="context-lost")
        return None
    try:
        await require_entitled_repo(
            job["repo"],
            job["user_id"],
            store=source_control,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        logger.warning(
            "agent_goal_round_repo_not_allowed",
            extra={
                "event": "agent_goal_round_repo_not_allowed",
                "thread_id": thread_id,
                "job_id": job["id"],
                "detail": str(exc),
            },
        )
        await _pause(goals, thread_id=thread_id, revision=goal["revision"], reason="turn-failed")
        return None
    # A work round is numbered by the budget it is about to spend; a
    # verification round is not charged, so it reports the round it is checking.
    round_number = rounds_used + 1 if kind == "work" else rounds_used
    prompt = render_round_prompt(
        objective=goal["objective"],
        kind=kind,
        round_number=round_number,
        max_rounds=goal["max_rounds"],
        previous=job.get("goal_reason"),
        previous_blocked=job.get("goal_report") == "blocked",
    )
    created = await conversations.create_follow_up(
        parent_job_id=str(job["id"]),
        user_id=str(job["user_id"]),
        prompt=prompt,
        # Model, effort and skills are all omitted, exactly as a person's own
        # follow-up omits them: the round inherits the conversation's setup
        # rather than re-deciding it.
        turn_source="goal",
        goal_round=round_number,
        goal_round_kind=kind,
        goal_revision=goal["revision"],
        client_request_id=client_request_id_for(
            thread_id=thread_id,
            revision=goal["revision"],
            kind=kind,
            parent_job_id=str(job["id"]),
        ),
        client_request_fingerprint=f"goal-round:{kind}:{round_number}",
    )
    if created is None:
        # The conversation vanished under us — the store answers ``None`` when
        # the parent turn or its thread no longer exists.
        await _pause(goals, thread_id=thread_id, revision=goal["revision"], reason="archived")
        return None
    error = created.get("error")
    if error is not None:
        await _pause(
            goals,
            thread_id=thread_id,
            revision=goal["revision"],
            reason=_PAUSE_FOR_ROUND.get(error, "turn-failed"),
        )
        return None
    return created


async def _pause(
    goals: GoalRepository,
    *,
    thread_id: str,
    revision: int,
    reason: GoalPauseReason,
) -> None:
    """Stop a goal that cannot continue, recording the reason it could not."""
    applied = await goals.pause_goal(thread_id=thread_id, revision=revision, reason=reason)
    logger.warning(
        "agent_goal_paused",
        extra={
            "event": "agent_goal_paused",
            "thread_id": thread_id,
            "reason": reason,
            "applied": applied is not None,
        },
    )


__all__ = ["CLIENT_REQUEST_PREFIX", "client_request_id_for", "maybe_continue_goal"]
