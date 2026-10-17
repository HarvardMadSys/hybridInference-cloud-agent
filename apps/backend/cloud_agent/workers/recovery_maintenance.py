"""Run queue and goal recovery outside the attempt lease reaper."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.goals.controller import run_goal_wakeups
from cloud_agent.grants import RENEWAL_INTERVAL_S
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cloud_agent.ports import (
        AttachmentRepository,
        AttemptCommands,
        ConversationCommands,
        ConversationQueries,
        GoalRepository,
        GoalWakeupRepository,
        PendingMessageRepository,
        SkillRepository,
        SourceControlRepository,
    )
    from cloud_agent.source_control_runtime import GitHubAppSource

logger = get_logger("cloud_agent.app")

QUEUED_FOLLOW_UP_DEADLINE_S = 30.0
GOAL_WAKEUP_DEADLINE_S = 90.0
GOAL_SCHEDULING_DEADLINE_S = 30.0


class _FollowUpMaintenanceStore(Protocol):
    """The capability used to settle legacy queued follow-ups."""

    attempt_commands: AttemptCommands


class _GoalSchedulingStore(Protocol):
    """The capability used to recover missing goal scheduling obligations."""

    goals: GoalRepository


class _GoalWakeupStore(_GoalSchedulingStore, Protocol):
    """The capabilities used to consume durable goal wakeups."""

    goal_wakeups: GoalWakeupRepository
    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    source_control: SourceControlRepository
    pending_messages: PendingMessageRepository
    attachments: AttachmentRepository
    skills: SkillRepository


async def _run_periodic_loop(
    name: str,
    operation: Callable[[], Awaitable[Any]],
    *,
    interval_seconds: float,
    deadline_seconds: float,
) -> None:
    """Run one non-overlapping maintenance pass with its own timeout boundary."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with asyncio.timeout(deadline_seconds):
                await operation()
        except TimeoutError:
            logger.warning(
                "%s recovery pass timed out",
                name.replace("_", " ").capitalize(),
                extra={
                    "event": f"agent_{name}_timeout",
                    "deadline_seconds": deadline_seconds,
                },
            )
        except Exception:
            logger.warning(
                "%s recovery pass failed",
                name.replace("_", " ").capitalize(),
                exc_info=True,
                extra={"event": f"agent_{name}_failed"},
            )


async def run_queued_follow_up_maintenance(
    store: _FollowUpMaintenanceStore,
    *,
    interval_seconds: float = RENEWAL_INTERVAL_S,
    deadline_seconds: float = QUEUED_FOLLOW_UP_DEADLINE_S,
) -> None:
    """Fail discontinuous legacy follow-ups without delaying lease recovery."""
    await _run_periodic_loop(
        "queued_follow_up_maintenance",
        store.attempt_commands.sweep_queued_follow_ups,
        interval_seconds=interval_seconds,
        deadline_seconds=deadline_seconds,
    )


async def run_goal_wakeup_maintenance(
    store: _GoalWakeupStore,
    *,
    github_app: GitHubAppSource | None = None,
    gateway: Any | None = None,
    interval_seconds: float = RENEWAL_INTERVAL_S,
    deadline_seconds: float = GOAL_WAKEUP_DEADLINE_S,
) -> None:
    """Consume durable goal wakeups independently of attempt lease recovery."""

    async def consume() -> None:
        await run_goal_wakeups(store=store, github_app=github_app, gateway=gateway)

    await _run_periodic_loop(
        "goal_wakeup_maintenance",
        consume,
        interval_seconds=interval_seconds,
        deadline_seconds=deadline_seconds,
    )


async def run_goal_scheduling_maintenance(
    store: _GoalSchedulingStore,
    *,
    interval_seconds: float = RENEWAL_INTERVAL_S,
    deadline_seconds: float = GOAL_SCHEDULING_DEADLINE_S,
) -> None:
    """Recover or pause goals whose scheduling obligation is missing."""

    async def sweep() -> None:
        result = await store.goals.sweep_goal_scheduling()
        if result["rearmed"] or result["stalled"]:
            logger.warning(
                "agent_goals_recovered",
                extra={
                    "event": "agent_goals_recovered",
                    "rearmed": len(result["rearmed"]),
                    "stalled": len(result["stalled"]),
                    "stalled_thread_ids": result["stalled"],
                },
            )

    await _run_periodic_loop(
        "goal_scheduling_maintenance",
        sweep,
        interval_seconds=interval_seconds,
        deadline_seconds=deadline_seconds,
    )
