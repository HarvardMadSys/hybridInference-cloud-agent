"""Authenticated session identity returned to the frontend."""

from __future__ import annotations

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

    ``email``, ``waitlist`` and ``identity`` describe the account; a field
    with no value is left out of the body.
    """

    id: str
    agent_access: AgentAccess
    email: str | None = None
    waitlist: WaitlistEntry | None = None
    identity: AccountIdentity | None = None
    is_local_admin: bool | None = None
