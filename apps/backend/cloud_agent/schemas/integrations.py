"""Source-control connections, repository creation, and integration status."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.entitlement import REPO_PATTERN
from cloud_agent.schemas.mcp import (
    McpIntegrationResponse,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.source_control_runtime import (
    LoadStatus,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)


class GitHubConnectRequest(BaseModel):
    """The callback values GitHub hands back after the user authorizes."""

    code: str = Field(..., min_length=1, max_length=512)
    state: str = Field(..., min_length=16, max_length=512)


class GitHubConnectionResponse(BaseModel):
    """Which GitHub installations this user has connected."""

    connections: list[dict[str, Any]] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    #: GitHub's install page, when the App is not on the user's own account yet
    #: — whether or not an organization's installation already reached them.
    install_url: str | None = None


class CreateGitHubRepositoryRequest(BaseModel):
    """Create a personal GitHub repository without starting a conversation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    description: str = Field("", max_length=350, pattern=r"^[^\x00]*$")
    private: bool = Field(True, strict=True)


class CreateGitHubRepositoryResponse(BaseModel):
    """The created repository and its separately checked Agent access."""

    repo: str = Field(..., pattern=REPO_PATTERN, max_length=140)
    web_url: str = Field(..., pattern=r"^https?://[^/]+/", max_length=2048)
    default_branch: str = Field(..., min_length=1, max_length=1024)
    accessible: bool


class SourceControlAccount(BaseModel):
    """Public identity metadata for a connected provider account."""

    id: str
    label: str
    web_url: str | None = None


class SourceControlRepository(BaseModel):
    """A provider-attested repository safe to render in the UI."""

    id: str
    name: str
    web_url: str | None = None


class SourceControlProviderResponse(BaseModel):
    """Connection and capability status for one supported provider."""

    provider: Literal["github", "gitlab"]
    configured: bool
    connected: bool
    connect_url: str | None = None
    manage_url: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    accounts: list[SourceControlAccount] = Field(default_factory=list)
    repositories: list[SourceControlRepository] = Field(default_factory=list)
    error: str | None = None
    #: The deployment's side of the provider, not the user's: ``unavailable``
    #: means configured, but this process could not load it and is retrying.
    #: ``connected`` and ``accounts`` still come from the user's own records, so
    #: a connection survives the outage. Required, so that a response built
    #: somewhere new cannot default to a state nobody checked.
    load_status: LoadStatus


class SourceControlIntegrationsResponse(BaseModel):
    """Everything the Integrations page shows the current user.

    Named for source control because that is all it carried first; ``mcp`` is
    the second kind of connection on the same page, kept in one response so the
    page makes one request and cannot render half-refreshed.
    """

    providers: list[SourceControlProviderResponse]
    mcp: list[McpIntegrationResponse] = Field(default_factory=list)


class OAuthConnectRequest(BaseModel):
    """Authorization callback values supplied by GitHub or GitLab."""

    code: str = Field(..., min_length=1, max_length=2048)
    state: str = Field(..., min_length=16, max_length=512)


class RepoBranchesResponse(BaseModel):
    """Branches of one repository the caller is entitled to."""

    default: str | None = None
    branches: list[str] = Field(default_factory=list)
