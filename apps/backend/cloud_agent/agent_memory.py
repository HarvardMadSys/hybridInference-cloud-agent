"""Durable memories: lasting rules agents follow for an owner, one row each."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves the view schema.
from typing import Literal

from pydantic import BaseModel, Field

from cloud_agent.user_memory import (
    _TEMPORARY_SCOPE,
    normalize_memory_edit,
)

#: Active memories kept per scope (the user, or one repository).
ACTIVE_LIMIT = 30
DESCRIPTION_LIMIT = 80
#: Archived memories are deleted after this many days.
ARCHIVE_RETENTION_DAYS = 90
#: ``agent``: saved by an agent during a task. ``manual``: written or edited by the owner.
Origin = Literal["agent", "manual"]


class AgentMemoryView(BaseModel):
    """One durable memory as its owner sees it."""

    id: str
    repo: str | None
    content: str
    description: str
    status: Literal["active", "archived"]
    origin: Origin
    source_job_ids: list[str] = Field(default_factory=list)
    version: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    archived_at: datetime | None = None


class AgentMemoryScopeView(BaseModel):
    """The memories of one scope: the user (``repo`` is null) or a repository."""

    repo: str | None
    enabled: bool
    memories: list[AgentMemoryView] = Field(default_factory=list)
    archived: list[AgentMemoryView] = Field(default_factory=list)


class AgentMemoryConflict(ValueError):
    """The memory changed after the version its owner examined."""


class AgentMemoryNotFound(ValueError):
    """The owner has no such memory."""


class AgentMemoryFull(ValueError):
    """The scope already holds its limit of memories its owner wrote."""


def describe(content: str) -> str:
    """Summarize a memory in one line for lists and indexes."""
    if len(content) <= DESCRIPTION_LIMIT:
        return content
    return content[: DESCRIPTION_LIMIT - 1].rstrip() + "…"


def normalize_memory(content: str) -> str:
    """Accept one lasting rule without credentials, within the size limit."""
    content = normalize_memory_edit(content)
    if _TEMPORARY_SCOPE.search(content.replace("`", "")):
        raise ValueError("Memory must not be limited to a temporary task.")
    return content


def memory_key(content: str) -> str:
    """Compare memories regardless of case and trailing punctuation."""
    return content.casefold().rstrip(".!?…\u3002\uff01\uff1f ")
