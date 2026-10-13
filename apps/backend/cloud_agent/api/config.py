"""The deployment's own configuration, as the composer reads it.

One endpoint: which runtimes and models this deployment offers. It is
owner-authenticated because it describes the account's choices rather than the
deployment's secrets, and it reads MCP connections because the composer offers
them beside the model.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Query

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import (
    get_gateway_client,
    get_github_app_state,
    get_storage,
)
from cloud_agent.entitlement import repos_for_user
from cloud_agent.gateway import GatewayError
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.registry import resolve_connection
from cloud_agent.schemas import AgentConfigResponse, McpConnectionSummary
from cloud_agent.source_control import github_connect_configured
from cloud_agent.source_control_runtime import GitHubAppState  # noqa: TC001 — FastAPI resolves it.
from cloud_agent_host.runtimes import (
    registered_runtimes,
    runtime_reasoning_efforts,
    runtimes_supporting_mcp,
)
from cloud_agent_shared.plugins import supported_plugin_runtimes

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


@router.get("/config", response_model=AgentConfigResponse)
async def get_agent_config(
    fresh: bool = Query(
        False,
        description=(
            "Ask GitHub again for this user's repositories instead of reusing the "
            "last minute's answer — for a repository just given to the App."
        ),
    ),
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
    if fresh and github.credentials is not None:
        github.credentials.forget_user(user["user_id"])
    # Independent sources can be read together. The model fields come from
    # one current Gateway response, without caching the owner's admission.
    (repos, github_account), (models, model_efforts), mcp_connections = await asyncio.gather(
        _repositories_and_account(store, github, user["user_id"]),
        _model_configuration_for_user(gateway, user["user_id"]),
        _mcp_connections_for_user(store, user["user_id"]),
    )
    if github.load_status == "unavailable":
        # The create endpoint answers 503 until the App is back, so a form that
        # looked ready would only fail on submit — whatever else is true.
        github_connected = False
    elif github_connect_configured(github.credentials):
        # Where users can connect their own GitHub, connecting comes first —
        # an operator-allowlisted repository does not stand in for it. It used
        # to: "connected" was computed as "has any repository", so the
        # allowlist let a user skip connecting, and a user who had connected
        # but whose installations covered no repository was asked to connect
        # again. Repository access is its own state: ``repos`` is empty.
        #
        # And a connection is one GitHub can still be asked about as the user:
        # one from before users' tokens were kept, or whose token GitHub has
        # refused, has to be made again — the composer's Connect does it.
        github_connected = bool(github_account and github_account.get("access_token_ciphertext"))
    else:
        # Nothing to connect to: the allowlist is the whole entitlement, and an
        # allowlisted public repository runs without a GitHub App at all (the
        # runner clones it anonymously).
        github_connected = bool(repos)
    return AgentConfigResponse(
        repos=repos,
        runtimes=registered_runtimes(),
        plugin_runtimes=supported_plugin_runtimes(),
        # Same predicate the create endpoint enforces and the sandbox's calls
        # will hit — not /v1/models, which answers for the *browsing* user and
        # offered models whose first agent call then 404ed.
        models=models,
        # What each runtime can express, and which models take the parameter at
        # all. The composer offers the platform's levels narrowed to the first
        # and gated on the second, and no control when either is empty — which
        # is also what an older gateway, answering no domains, produces.
        runtime_reasoning_efforts=runtime_reasoning_efforts(),
        model_reasoning_efforts=model_efforts,
        # This user's connected servers, not the deployment's: same reason the
        # repository list above is theirs. Public fields only — McpConnectionSummary
        # has nowhere to put a URL or a credential, which is how it stays that
        # way.
        mcp_connections=[McpConnectionSummary(**entry) for entry in mcp_connections],
        mcp_runtimes=runtimes_supporting_mcp(),
        github_connected=github_connected,
        # OAuth must start on the authenticated integrations page so the
        # server can issue state bound to this user. Never expose the old raw
        # operator URL here: it had no CSRF binding.
        github_install_url="/agents/integrations" if github.credentials is not None else None,
        github_load_status=github.load_status,
    )


async def _repositories_and_account(
    store: _ConfigStorage | None, github: GitHubAppState, user_id: str
) -> tuple[list[str], dict[str, Any] | None]:
    """Read repository entitlement before the GitHub account it can invalidate."""
    repos = await repos_for_user(
        user_id,
        store=None if store is None else store.source_control,
        app_credentials=github.credentials,
    )
    # Entitlement can clear a revoked user token. Reading the account after it
    # preserves the reconnect answer for that same request.
    account = (
        await store.source_control.get_github_account(user_id=user_id)
        if store is not None
        else None
    )
    return repos, account


async def _model_configuration_for_user(
    gateway: Any, user_id: str
) -> tuple[list[str], dict[str, list[str]]]:
    """Return one model configuration, or no options when the Gateway fails."""
    try:
        return await gateway.model_catalog_with_reasoning_efforts(user_id)
    except GatewayError:
        logger.warning(
            "agent_model_catalog_unavailable",
            exc_info=True,
            extra={"event": "agent_model_catalog_unavailable", "user_id": user_id},
        )
        return [], {}


async def _mcp_connections_for_user(
    store: _ConfigStorage | None, user_id: str
) -> list[dict[str, Any]]:
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
    if store is None:
        return []
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
