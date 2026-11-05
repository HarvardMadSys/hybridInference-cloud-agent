"""The tools a sandbox MCP server listed, as its sandbox reports them.

The control plane cannot reach a server that runs in a chat's sandbox, so it
cannot discover its tools the way it discovers a remote server's. The
sandbox's MCP gateway reports them instead, the first time a chat's CLI lists
them and whenever they change (``deploy/sandbox/cloud-agent-mcp``), so the
owner's settings show the same tools the agent sees.

The report is one more JSON-RPC method on the relay's own endpoint, not a
route of its own: the relay listener serves the bare relay and nothing beneath
it (``tests/unit/test_mcp_listener_isolation.py``). It arrives already
authenticated, with the member's own MCP credential, which the gateway
forwards from the CLI's request, and it is applied only to the chat that
credential is running a turn for, and only to that chat's enabled bindings of
the owner's own sandbox connections. It can write a tool list and nothing
else: no policy, no approval, no other connection.

A report is stored only when it differs from what is stored. Storing a list
moves the connection's capability revision, and that marks every binding
approved against the earlier revision stale, so an unchanged list must not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.jsonrpc import INVALID_PARAMS
from cloud_agent.mcp.models import ConnectionErrorKind, ConnectionStatus, ExecutionLocation
from cloud_agent.mcp.registry import resolve_for_thread
from cloud_agent_shared.sandbox_mcp import MAX_SANDBOX_MCP_SERVERS

if TYPE_CHECKING:
    from cloud_agent.mcp.models import Capability

logger = get_logger(__name__)

#: The relay method the sandbox's gateway reports with. Namespaced, so no MCP
#: method can ever mean it.
METHOD = "cloud_agent/sandbox_tools"
MAX_TOOLS = 500


class SandboxTool(BaseModel):
    """One tool as an MCP server lists it; anything else it carries is ignored."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=256)
    description: str = Field("", max_length=16384)
    inputSchema: dict[str, Any] | None = None
    annotations: dict[str, Any] = Field(default_factory=dict)


class SandboxToolReport(BaseModel):
    """Each server's tools, by the alias the chat binds it under."""

    model_config = ConfigDict(extra="forbid")

    servers: dict[str, list[SandboxTool]] = Field(max_length=MAX_SANDBOX_MCP_SERVERS)


def _payload(tool: SandboxTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.inputSchema,
        "annotations": tool.annotations,
    }


def _stored(capability: Capability) -> dict[str, Any]:
    return {
        "name": capability.name,
        "description": capability.description,
        "input_schema": dict(capability.input_schema) if capability.input_schema else None,
        "annotations": dict(capability.annotations),
    }


async def store_report(
    report: SandboxToolReport, identity: dict[str, Any], store: Any
) -> list[str]:
    """Store the tools a chat's sandbox servers listed, where they changed; the aliases stored."""
    stored: list[str] = []
    for resolved, binding in await resolve_for_thread(
        store.mcp, thread_id=str(identity["thread_id"])
    ):
        connection = resolved.connection
        reported = report.servers.get(binding.alias)
        if (
            reported is None
            or not binding.enabled
            or connection.user_id != identity["user_id"]
            or connection.config.location is not ExecutionLocation.SANDBOX
        ):
            continue
        tools = [_payload(tool) for tool in reported[:MAX_TOOLS]]
        current = [_stored(item) for item in resolved.snapshot.tools] if resolved.snapshot else None
        if current == tools:
            continue
        revision = await store.mcp.save_mcp_capabilities(
            connection_id=int(connection.connection_id),
            config_revision=connection.config_revision,
            protocol_version="",
            server_name=binding.alias,
            server_version="",
            tools=tools,
            resources=[],
            resource_templates=[],
            prompts=[],
            support={},
            expected_authorization_epoch=connection.authorization_epoch,
            expected_capability_revision=connection.capability_revision,
            expected_credential_version=connection.credential_version,
        )
        if not revision:
            continue  # the connection changed meanwhile; the next report brings it
        await store.mcp.set_mcp_connection_status(
            connection_id=int(connection.connection_id),
            status=ConnectionStatus.ACTIVE.value,
            error_kind=ConnectionErrorKind.NONE.value,
            expected_config_revision=connection.config_revision,
            expected_authorization_epoch=connection.authorization_epoch,
            expected_credential_version=connection.credential_version,
            expected_capability_revision=revision,
        )
        stored.append(binding.alias)
        logger.info(
            "agent_mcp_sandbox_tools_reported",
            extra={
                "event": "agent_mcp_sandbox_tools_reported",
                "connection_id": connection.connection_id,
                "tools": len(tools),
            },
        )
    return stored


async def reply(payload: dict[str, Any], identity: dict[str, Any], store: Any) -> dict[str, Any]:
    """The relay's JSON-RPC reply to one report, from an authenticated member."""
    request_id = payload.get("id")
    try:
        report = SandboxToolReport.model_validate(payload.get("params"))
    except ValidationError as exc:
        error = {
            "code": INVALID_PARAMS,
            "message": f"invalid tool report: {exc.error_count()} errors",
        }
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
    stored = await store_report(report, identity, store)
    return {"jsonrpc": "2.0", "id": request_id, "result": {"stored": stored}}


__all__ = ["METHOD", "SandboxToolReport", "reply", "store_report"]
