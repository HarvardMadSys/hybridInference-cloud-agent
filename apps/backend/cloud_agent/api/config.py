"""The deployment's own configuration, as the composer reads it.

One endpoint: which runtimes and models this deployment offers. It is
owner-authenticated because it describes the account's choices rather than the
deployment's secrets, and it reads MCP connections because the composer offers
them beside the model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import (
    get_gateway_client,
    get_github_app_state,
    get_storage,
    require_store,
)
from cloud_agent.entitlement import repos_for_user
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.registry import resolve_connection
from cloud_agent.schemas import AgentConfigResponse, McpConnectionSummary
from cloud_agent.source_control_runtime import GitHubAppState  # noqa: TC001 — FastAPI resolves it.
from cloud_agent.visible_models import agent_model_reasoning_efforts, agent_visible_models
from cloud_agent_host.runtimes import (
    registered_runtimes,
    runtime_reasoning_efforts,
    runtimes_supporting_mcp,
)

if TYPE_CHECKING:
    from cloud_agent.ports import (
        ConversationCommands,
        ConversationQueries,
        McpConnectionRepository,
        SourceControlRepository as SourceControlPort,
    )


router = APIRouter(prefix="/v1/agent")

#: The moved handlers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _ConfigStorage(Protocol):
    """The two capabilities the config endpoint reaches for."""

    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    mcp: McpConnectionRepository
    source_control: SourceControlPort


def _require_store(store: _ConfigStorage | None) -> _ConfigStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


@router.get("/config", response_model=AgentConfigResponse)
async def get_agent_config(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _ConfigStorage | None = Depends(get_storage),
    github: GitHubAppState = Depends(get_github_app_state),
    gateway: Any = Depends(get_gateway_client),
) -> AgentConfigResponse:
    """What this deployment will actually accept.

    The composer showed a repository, a branch, a runtime and a model as static
    labels while submitting different hardcoded values, so the UI described a
    job nobody was running. A picker has to be built from the same answers the
    create endpoint enforces, or it is decoration.

    Answers while the GitHub App is unavailable — the models, runtimes and MCP
    connections are all still true — and says so in ``github_load_status``.
    """
    # This user's repositories, not the deployment's: what the picker offers
    # has to be what the create endpoint will accept for *them*.
    repos = await repos_for_user(
        user["user_id"],
        store=None if store is None else store.source_control,
        app_credentials=github.credentials,
    )
    return AgentConfigResponse(
        repos=repos,
        runtimes=registered_runtimes(),
        # Same predicate the create endpoint enforces and the sandbox's calls
        # will hit — not /v1/models, which answers for the *browsing* user and
        # offered models whose first agent call then 404ed.
        models=await agent_visible_models(gateway, user_id=user["user_id"]),
        # What each runtime can express, and which models take the parameter at
        # all. The composer offers the platform's levels narrowed to the first
        # and gated on the second, and no control when either is empty — which
        # is also what an older gateway, answering no domains, produces.
        runtime_reasoning_efforts=runtime_reasoning_efforts(),
        model_reasoning_efforts=await agent_model_reasoning_efforts(
            gateway, user_id=user["user_id"]
        ),
        # This user's connected servers, not the deployment's: same reason the
        # repository list above is theirs. Public fields only — McpServerSummary
        # has nowhere to put a URL or a credential, which is how it stays that
        # way.
        mcp_connections=[
            McpConnectionSummary(**entry)
            for entry in (
                await _mcp_connections_for_user(store, user["user_id"]) if store is not None else []
            )
        ],
        mcp_runtimes=runtimes_supporting_mcp(),
        # Both halves, and read from what is actually wired rather than from
        # the environment: an App the platform can mint tokens from, and at
        # least one repository this user may work on. Either alone leaves a
        # composer that cannot produce a runnable job.
        # The historical field name is narrower than its meaning in the UI:
        # it gates whether the composer can create a runnable job. An
        # operator-allowlisted public repository is runnable without a GitHub
        # App (the runner clones it anonymously), so requiring App credentials
        # here made the API accept the job while the browser disabled the form.
        #
        # False while the App is unavailable, whatever the allowlist offers: the
        # create endpoint answers 503 until it is back, so a form that looked
        # ready would only fail on submit.
        github_connected=bool(repos) and github.load_status != "unavailable",
        # OAuth must start on the authenticated integrations page so the
        # server can issue state bound to this user. Never expose the old raw
        # operator URL here: it had no CSRF binding.
        github_install_url="/agents/integrations" if github.credentials is not None else None,
        github_load_status=github.load_status,
    )


async def _mcp_connections_for_user(store: _ConfigStorage, user_id: str) -> list[dict[str, Any]]:
    """The MCP connections *this user* has added, for the composer.

    Display metadata only. A connection's address and its credential never
    leave this process: the sandbox reaches a server through the relay, which
    is the entire reason resolution lives beside the relay rather than beside
    the picker. :class:`McpConnectionSummary` has no field either could land
    in, so that stays true without a filter at this call site.

    Unusable connections are listed with their reason rather than omitted. A
    picker that silently drops a server its owner is looking for is a bug
    report waiting to happen; one that says *this credential expired* is an
    instruction.
    """
    rows = await store.mcp.list_mcp_connections(user_id=user_id)
    summaries: list[dict[str, Any]] = []
    for row in rows:
        resolved = await resolve_connection(store.mcp, row=row)
        snapshot = resolved.snapshot
        summaries.append(
            {
                "connection_id": int(row["id"]),
                "label": resolved.connection.label,
                "transport": resolved.connection.config.transport.value,
                "location": resolved.connection.config.location.value,
                "status": resolved.connection.status.value,
                "available": resolved.usable,
                "error": resolved.error,
                "account_label": row.get("account_label"),
                "tool_count": len(snapshot.tools) if snapshot else 0,
            }
        )
    return summaries
