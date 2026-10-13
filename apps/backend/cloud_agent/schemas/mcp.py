"""Public MCP connection, binding, and integration models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class McpConnectionSummary(BaseModel):
    """One MCP connection the composer may offer, and whether it currently works.

    Public fields only: a connection's address and its credential stay on this
    service, so neither has a field here to leak into.

    ``available`` and ``error`` are separate from ``status`` on purpose. A
    connection whose credential expired is still connected, and hiding it from
    the composer would leave its owner with a picker that silently omits the
    server they are looking for; showing it with the reason is what makes the
    Integrations page an answer rather than a mystery.
    """

    connection_id: int
    label: str
    transport: str = ""
    location: str = ""
    status: str = "draft"
    available: bool = False
    error: str | None = None
    account_label: str | None = None
    tool_count: int = 0


class McpBindingInput(BaseModel):
    """One connection's use by a session, as the caller asks for it.

    The policy map is name to ``deny`` / ``confirm`` / ``allow``. A name that
    is absent is denied: a session means what it says, and a capability the
    server adds later is not covered by what its owner chose earlier.
    """

    connection_id: int
    alias: str | None = Field(
        default=None,
        max_length=40,
        description="Stable prefix for the tool names this connection contributes.",
    )
    tools: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class McpBindingSummary(BaseModel):
    """Which connection a job used, by label rather than by address."""

    connection_id: int
    alias: str
    label: str = ""


class McpIntegrationResponse(BaseModel):
    """One catalogue MCP server, and whether this user has connected it.

    Carries no credential and no upstream URL — the same rule as
    :class:`McpConnectionSummary`, for the same reason, and enforced the same way:
    there is no field either could occupy. ``account_label`` is what the
    provider calls the connected account, which is the user's own name for it
    and the only way to tell two connections apart in the UI.
    """

    name: str
    label: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    #: Whether connecting is possible on this deployment right now.
    configured: bool = False
    connected: bool = False
    account_label: str | None = None
    connect_url: str | None = None
    #: Why a connection that exists buys nothing — expired, unreadable, or a
    #: provider withdrawn since. ``connected`` stays true because the row is
    #: really there and disconnecting it is the way out; this says what the
    #: owner has to do about it. Mirrors ``SourceControlProviderResponse``.
    error: str | None = None
