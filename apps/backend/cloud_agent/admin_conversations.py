"""Metadata-only conversation projections and scoped admin pagination."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from cloud_agent.admin_observability import AdminRun


class AdminConversation(BaseModel):
    """A conversation identity, with current state separate from turn results."""

    thread_id: str
    title: str | None = None
    user_id: str
    #: Null for a temporary workspace.
    repo: str | None
    created_at: datetime
    last_activity_at: datetime
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    state: str
    turn_count: int
    latest_job_id: str
    latest_turn_no: int
    latest_turn_state: str
    latest_runtime: str
    latest_model: str


class AdminConversationMatch(AdminConversation):
    """A distinct conversation containing turns in the selected run cohort."""

    last_matching_turn_at: datetime


class AdminConversationList(BaseModel):
    """Conversations ordered by their newest matching turn, then identity."""

    conversations: list[AdminConversationMatch]
    since: datetime
    until: datetime
    next_cursor: str | None = None


class ConversationCursor(BaseModel):
    """A creation-time boundary tied to one conversation selection."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    since: AwareDatetime
    until: AwareDatetime
    created_at: AwareDatetime
    thread_id: str = Field(min_length=1, max_length=256)


class ConversationTurnsCursor(BaseModel):
    """Page backwards through a conversation without mixing filter contexts."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    before_turn_no: int = Field(ge=1)


class AdminConversationTurn(AdminRun):
    """One turn, including whether it matches the originating usage selection."""

    matches_filters: bool
    fork_source_job_id: str | None = None


class AdminConversationDetail(BaseModel):
    """Conversation metadata and a bounded page of turns, newest first."""

    conversation: AdminConversation
    turns: list[AdminConversationTurn]
    next_cursor: str | None = None
