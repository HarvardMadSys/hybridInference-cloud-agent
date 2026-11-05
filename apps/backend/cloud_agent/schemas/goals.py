"""Conversation goal writes and revision-aware responses."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.goals.models import (
    MAX_OBJECTIVE_CHARS,
    GoalView,
)


class GoalInput(BaseModel):
    """An owner's completion condition for one conversation.

    ``max_rounds`` is **tri-state**, and the three states mean three different
    things. Omitted — the ordinary case, and what every client sends — leaves
    whatever bound the goal already carries exactly as it is, so a caller that
    only changes the words cannot silently delete a limit its owner stated.
    A number states a bound; an explicit ``null`` lifts one. That is why no
    writer reads the field directly: what the request *said* is
    :attr:`states_max_rounds`, and absence is not the same fact as ``None``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)
    #: An *optional* bound the owner states. Omitted means "leave the bound
    #: alone" — never "no limit", which would make every edit that does not
    #: mention one a silent removal. An explicit ``null`` is how a bound is
    #: lifted; a number is how one is set, and it is honored exactly, with no
    #: maximum, because there is no longer a ceiling for the platform to impose.
    max_rounds: int | None = Field(
        None,
        ge=1,
        description=(
            "An explicit maximum number of automatic rounds. A number sets it "
            "and an explicit null lifts it; omitting the field leaves whatever "
            "bound the goal already carries unchanged. Omitting it on a goal "
            "that has none means no limit: the goal continues until the "
            "objective is met, paused, cleared, blocked on the owner, or "
            "stopped by an existing run-time limit. There is no default and no "
            "maximum."
        ),
    )

    @property
    def states_max_rounds(self) -> bool:
        """Whether the request said anything about the bound at all.

        Pydantic keeps the keys the caller actually sent, which is the only
        place the difference between "omitted" and "explicitly null" survives
        validation — both arrive as ``None``. Every writer of the column reads
        this rather than the field, or an edit that mentions nothing about a
        bound would clear one.
        """
        return "max_rounds" in self.model_fields_set


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
