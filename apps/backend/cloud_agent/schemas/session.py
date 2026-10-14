"""Authenticated session identity returned to the frontend."""

from __future__ import annotations

from pydantic import BaseModel

from cloud_agent.agent_access import (
    AgentAccess,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)


class SessionUserResponse(BaseModel):
    """Who the session says the caller is.

    ``id`` rather than ``user_id``: this is the shape ``apps/frontend/src/lib/api/
    session.ts`` was written against, and it is the contract for all three
    session routes.
    """

    id: str
    agent_access: AgentAccess
