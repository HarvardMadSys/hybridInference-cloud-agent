"""Conversation goal writes and revision-aware responses."""

from __future__ import annotations

from pydantic import BaseModel, Field

from cloud_agent.goals.models import (  # noqa: TC001 — Pydantic resolves annotations at runtime.
    GoalInput,
    GoalView,
)


class GoalRequest(BaseModel):
    """A goal write, with the revision the caller read it at.

    ``revision`` is what makes two edits from two tabs resolvable: the second
    one is refused rather than silently overwriting the first. Omitted means
    "write it whatever it currently says", which is what a first-time set from
    the composer wants.
    """

    goal: GoalInput
    revision: int | None = Field(None, ge=0)


class GoalRevisionRequest(BaseModel):
    """An optional compare-and-set guard on a goal operation.

    Omitted means "act on whatever the goal currently is", which is what the
    pause and resume buttons send: they are idempotent by nature, and refusing
    to press one because a round advanced a counter would be a worse answer than
    doing what was asked. Clear sends it: the owner confirmed removing the goal
    they saw, not whatever replaced it since.
    """

    revision: int | None = Field(None, ge=0)


class GoalResponse(BaseModel):
    """The conversation's goal after a write that changed it.

    Answered from the *thread*, not from a turn: a goal belongs to the
    conversation, and a conversation whose newest turn has been deleted still
    has one. ``revision`` is echoed even when there is no goal, so a client can
    compare and set without a separate read.
    """

    thread_id: str
    goal: GoalView | None = None
    revision: int


class GoalClearedResponse(BaseModel):
    """The conversation after removing its goal."""

    thread_id: str
    cleared: bool = True
    revision: int
