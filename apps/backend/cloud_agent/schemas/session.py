"""Authenticated session identity returned to the frontend."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from cloud_agent.agent_access import (
    AgentAccess,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.schemas.accounts import (
    AccountIdentity,  # noqa: TC001 — Pydantic resolves annotations at runtime.
    WaitlistEntry,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)


class SessionUserResponse(BaseModel):
    """Who the session says the caller is.

    ``id`` rather than ``user_id``: this is the shape ``apps/frontend/src/lib/api/
    session.ts`` was written against, and it is the contract for all three
    session routes.

    ``auth_mode`` says which identity source answered. ``email`` and
    ``waitlist`` are present for a local account only, and left out of the
    body otherwise; the Gateway owns those for its accounts.
    """

    id: str
    agent_access: AgentAccess
    auth_mode: Literal["local", "gateway"] | None = None
    email: str | None = None
    waitlist: WaitlistEntry | None = None
    identity: AccountIdentity | None = None
    is_local_admin: bool | None = None
