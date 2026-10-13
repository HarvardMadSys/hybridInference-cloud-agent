"""Deployment capabilities returned to the task composer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from cloud_agent.attachments.models import AttachmentLimits
from cloud_agent.goals.models import GoalLimits
from cloud_agent.schemas.mcp import (
    McpConnectionSummary,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.settings import setting_env
from cloud_agent.skills.models import SkillLimits
from cloud_agent.source_control_runtime import (
    LoadStatus,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent_shared.environment import environment_limits


class AgentConfigResponse(BaseModel):
    """What this deployment will actually accept, for the task composer.

    The composer used to show a repository, a branch, a runtime and a model as
    static labels while submitting different hardcoded values — so the UI
    described a job nobody was running. These are the real answers.
    """

    environment_limits: dict[str, Any] | None = Field(
        default_factory=lambda: (
            environment_limits()
            if setting_env("AGENT_REPOSITORY_ENVIRONMENTS_ENABLED", "false") == "true"
            else None
        )
    )
    skill_limits: SkillLimits = Field(default_factory=SkillLimits)
    attachment_limits: AttachmentLimits = Field(default_factory=AttachmentLimits)
    goal_limits: GoalLimits = Field(default_factory=GoalLimits)

    repos: list[str] = Field(
        default_factory=list,
        description="Repositories this deployment is entitled to work on. Empty means none.",
    )
    runtimes: list[str] = Field(default_factory=list, description="Runtime ids that can run here.")
    plugin_runtimes: list[str] = Field(
        default_factory=list,
        description="Runtime ids that support plugins installed at sandbox initialization.",
    )
    models: list[str] = Field(
        default_factory=list,
        description=(
            "Model ids an agent job can actually call for this user — the same "
            "predicate the create endpoint enforces, not the /v1/models list."
        ),
    )
    runtime_reasoning_efforts: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Effort levels each runtime can express, in its own vocabulary. A "
            "runtime absent here cannot express one at all."
        ),
    )
    model_reasoning_efforts: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Effort levels each model accepts, from the gateway's registry. The "
            "composer offers the intersection with the chosen runtime's, and no "
            "control at all when that is empty — which is also what it shows "
            "against a gateway too old to answer this."
        ),
    )
    mcp_connections: list[McpConnectionSummary] = Field(
        default_factory=list,
        description="MCP servers this deployment offers. Empty means none are configured.",
    )
    mcp_runtimes: list[str] = Field(
        default_factory=list,
        description=(
            "Runtime ids that can be given MCP servers. A runtime outside this list "
            "refuses a job that names any, rather than dropping them silently."
        ),
    )
    # Source control connection state. The composer is gated on this rather
    # than offering pickers that submit something else: with no App installed
    # there is no repository to work on, and a task box that looks ready is a
    # worse answer than one that says what is missing.
    #: Whether this user has done what this deployment asks before choosing a
    #: repository: connected GitHub where users can, or — with no connectable
    #: App — been given one by the allowlist. ``repos`` may still be empty; that
    #: is a connection without repository access, not a missing connection.
    github_connected: bool = False
    github_install_url: str | None = None
    #: Whether this deployment's GitHub App is working in this process. While it
    #: is ``unavailable`` — configured, but not loaded, and being retried — the
    #: composer says so instead of asking the user to connect or an
    #: administrator to set GitHub up, and ``github_connected`` is false.
    github_load_status: LoadStatus
