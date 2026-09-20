"""The Gateway's explicit Agent admission decision, independent of account roles."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentPermission = Literal["agent.use", "agent.admin"]


class AgentAccess(BaseModel):
    """Public admission and permissions returned with an authenticated profile."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    allowed: bool
    permissions: list[AgentPermission]

    @model_validator(mode="after")
    def consistent_permissions(self) -> Self:
        """Reject ambiguous or contradictory grants instead of inferring access."""
        if len(self.permissions) != len(set(self.permissions)):
            raise ValueError("Agent permissions must be unique")
        if self.allowed != ("agent.use" in self.permissions):
            raise ValueError("Agent admission must agree with agent.use")
        if "agent.admin" in self.permissions and "agent.use" not in self.permissions:
            raise ValueError("Agent administration requires agent.use")
        return self

    def permits(self, permission: str) -> bool:
        """Whether the decision explicitly grants the requested permission."""
        return self.allowed and permission in self.permissions


class AgentAccessDecision(AgentAccess):
    """An internal decision bound to the exact Gateway user requested."""

    user_id: str = Field(min_length=1)

    def public(self) -> AgentAccess:
        """Return the decision without repeating the profile's user identifier."""
        return AgentAccess(allowed=self.allowed, permissions=list(self.permissions))
