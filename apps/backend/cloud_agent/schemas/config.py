"""Deployment capabilities returned to the task composer."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Any, Literal

from pydantic import BaseModel, Field

from cloud_agent.attachments.models import AttachmentLimits
from cloud_agent.goals.models import GoalLimits
from cloud_agent.schemas.mcp import (
    McpConnectionSummary,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.skills.models import SkillLimits
from cloud_agent.source_control.source_control_runtime import (
    LoadStatus,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent_shared.environment import environment_limits


class ModelEffortOffer(BaseModel):
    """Which reasoning levels one agent may run one connection model at.

    ``status`` is what the model's provider declared for the API this agent's
    plan calls: ``supported`` (``levels`` are the declared ones this agent's
    CLI can send, weakest first), ``unsupported`` (the provider declares none,
    or none this agent can send) or ``unknown`` (nothing the provider said
    settles it). Auto -- no level -- is always available and is not listed.
    """

    status: Literal["supported", "unsupported", "unknown"]
    levels: list[str] = Field(default_factory=list)
    #: Why, for ``unknown`` or an agent that cannot send a declared level:
    #: ``not_read``, ``listing_unavailable``, ``not_listed``, ``not_declared``,
    #: ``conflicting_declarations`` or ``agent_cannot_express``.
    reason: str = ""
    #: Which declaration it came from: ``anthropic_models_api``,
    #: ``models_listing_reasoning`` or ``models_listing_effort``; empty when none.
    source: str = ""


class ModelConnectionOption(BaseModel):
    """One of the owner's enabled model connections, as the composer offers it.

    Everything needed to offer its models, and which agents can run each one
    now; nothing about its key or its APIs.
    """

    id: str
    name: str
    #: Every listed model, in the owner's order.
    models: list[str]
    #: The listed models a task can run now, each with the agents that can run
    #: it: exactly what the execution gates accept. A model missing here is not
    #: verified, or no agent can use it.
    model_runtimes: dict[str, list[str]] = Field(default_factory=dict)
    #: For each model in ``model_runtimes``, per agent: the reasoning levels a
    #: task may choose besides Auto, and whether the provider declared them.
    model_efforts: dict[str, dict[str, ModelEffortOffer]] = Field(default_factory=dict)


class ProvidedModelAllowance(BaseModel):
    """One owner's allowance on one provided model, for the current UTC day."""

    #: Requests the owner may send today; ``None`` is unlimited.
    daily_requests: int | None = None
    #: Requests the model relay has accepted from this owner today.
    used_today: int = 0


class ProvidedModelSource(BaseModel):
    """One connection the deployment provides, as the composer offers it to this owner.

    The same shape as an owner's own connection option, plus the owner's
    allowances. Only the models this owner may use are listed: one whose
    allowance is zero is absent, as if it were not provided.
    """

    id: str
    name: str
    #: Whether a new owner with no history starts here.
    is_default: bool = False
    #: The model a new owner starts on, when this owner may use it.
    default_model: str | None = None
    models: list[str]
    model_runtimes: dict[str, list[str]] = Field(default_factory=dict)
    model_efforts: dict[str, dict[str, ModelEffortOffer]] = Field(default_factory=dict)
    #: Per listed model: this owner's allowance and today's use.
    allowances: dict[str, ProvidedModelAllowance] = Field(default_factory=dict)
    #: When today's counts start over: the next midnight, UTC.
    resets_at: datetime


class AgentConfigResponse(BaseModel):
    """What this deployment will actually accept, for the task composer.

    The composer used to show a repository, a branch, a runtime and a model as
    static labels while submitting different hardcoded values — so the UI
    described a job nobody was running. These are the real answers.
    """

    environment_limits: dict[str, Any] = Field(default_factory=environment_limits)
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
    model_connection_required: bool = Field(
        default=False,
        description=(
            "True while neither one of this owner's enabled model connections nor a "
            "connection the deployment provides has a model this owner can run: "
            "nothing can run until one is set up."
        ),
    )
    runtime_reasoning_efforts: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Effort levels each runtime can express, in its own vocabulary. A "
            "runtime absent here cannot express one at all."
        ),
    )
    mcp_connections: list[McpConnectionSummary] = Field(
        default_factory=list,
        description="MCP servers this deployment offers. Empty means none are configured.",
    )
    default_model: str | None = Field(
        default=None,
        description=(
            "The model a new task should start on: the one this owner's newest turn "
            "used, while its connection is enabled and still lists it. Null means "
            "the composer leaves its model control empty and asks."
        ),
    )
    model_connections: list[ModelConnectionOption] = Field(
        default_factory=list,
        description=(
            "This owner's enabled model connections, the only inference source. A "
            "model chosen from one is submitted with its `model_connection_id`."
        ),
    )
    provided_sources: list[ProvidedModelSource] = Field(
        default_factory=list,
        description=(
            "Connections this deployment provides to every admitted owner, each with "
            "this owner's daily request allowance per model. A model chosen from one "
            "is submitted with the source's id as `model_connection_id`."
        ),
    )
    model_connections_enabled: bool = Field(
        default=False,
        description="Whether this deployment lets owners add and run on their own connections.",
    )
    default_model_connection_id: str | None = Field(
        default=None,
        description=(
            "The connection `default_model` last ran on; set exactly when `default_model` is."
        ),
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
