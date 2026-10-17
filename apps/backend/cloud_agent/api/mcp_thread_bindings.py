"""Thread-level MCP bindings and their revision-fenced writes.

A binding is the narrow bridge between a conversation and one owner-owned
connection.  This module owns thread ownership, current-catalogue validation,
alias assignment and the exact response shape; it never approves a tool call or
touches a provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.api.mcp_connections_shared import (
    _bad_request,
    _capability_payload,
    _capability_view,
    _McpConnectionsStorage,
    _not_found,
    _parse_policy,
    _require_store,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.authorization import binding_from_selection
from cloud_agent.mcp.binding_aliases import binding_aliases
from cloud_agent.mcp.models import ConnectionStatus
from cloud_agent.mcp.registry import resolve_connection

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/agent/mcp", tags=["agent-mcp"])


class BindingRequest(BaseModel):
    """One connection's selection for a session."""

    model_config = ConfigDict(extra="forbid")

    connection_id: int
    alias: str | None = None
    enabled: bool = True
    use_defaults: bool = False
    #: Tool name to policy. ``deny`` is also what an omitted name means.
    tools: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)


class BindingResponse(BaseModel):
    """What one session is currently allowed to use."""

    connection_id: int
    connection_label: str
    alias: str
    enabled: bool
    selection_pending: bool
    available: bool
    error: str | None = None
    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    tools: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)
    config_revision: int = 1
    authorization_epoch: int = 1
    capability_revision: int = 1
    policy_revision: int = 1


class BindingsResponse(BaseModel):
    """Every connection a session may use."""

    bindings: list[BindingResponse] = Field(default_factory=list)


class ReplaceBindingsRequest(BaseModel):
    """Replace a session's selection wholesale."""

    model_config = ConfigDict(extra="forbid")

    bindings: list[BindingRequest] = Field(default_factory=list)


def _default_alias(row: Mapping[str, Any], index: int) -> str:
    """A stable alias derived from the connection, used when none is given."""
    template = str(row.get("template") or "").strip()
    if template:
        return f"{template}{index + 1}" if index else template
    return f"mcp{row['id']}"


async def _require_thread(
    job_store: _McpConnectionsStorage, *, thread_id: str, user_id: str
) -> None:
    """Refuse a thread this user does not own."""
    thread = await job_store.conversation_queries.get_thread(thread_id)
    if thread is None or str(thread.get("user_id")) != user_id:
        raise _not_found("no such conversation")


@router.get("/threads/{thread_id}/bindings", response_model=BindingsResponse)
async def get_thread_bindings(
    thread_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> BindingsResponse:
    """What one session may use, and what each connection offers."""
    job_store = _require_store(store)
    await _require_thread(job_store, thread_id=thread_id, user_id=user["user_id"])
    return BindingsResponse(
        bindings=await _binding_views(job_store, thread_id=thread_id, user_id=user["user_id"])
    )


@router.put("/threads/{thread_id}/bindings", response_model=BindingsResponse)
async def replace_thread_bindings(
    thread_id: str,
    body: ReplaceBindingsRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> BindingsResponse:
    """Replace explicit conversation overrides."""
    return await _save_thread_bindings(thread_id, body, user, store, merge=False)


@router.put("/threads/{thread_id}/bindings/{connection_id}", response_model=BindingsResponse)
async def update_thread_binding(
    thread_id: str,
    connection_id: int,
    body: BindingRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> BindingsResponse:
    """Change one server without reapproving or overwriting another server's rules."""
    if body.connection_id != connection_id:
        raise _bad_request("the connection id does not match the requested server")
    return await _save_thread_bindings(
        thread_id, ReplaceBindingsRequest(bindings=[body]), user, store, merge=True
    )


async def _save_thread_bindings(
    thread_id: str,
    body: ReplaceBindingsRequest,
    user: dict[str, Any],
    store: _McpConnectionsStorage | None,
    *,
    merge: bool,
) -> BindingsResponse:
    """Replace a session's selection.

    The selection is validated against the connections' *current* revisions, so
    a binding can never be written that already fails its own fence. Every
    connection the user names must be theirs, which is what makes the
    connection id safe to accept from a client.
    """
    job_store = _require_store(store)
    await _require_thread(job_store, thread_id=thread_id, user_id=user["user_id"])
    prepared: list[dict[str, Any]] = []
    aliases: list[tuple[int, str | None, str]] = []
    for index, item in enumerate(body.bindings):
        row = await job_store.mcp.get_mcp_connection(
            connection_id=item.connection_id, user_id=user["user_id"]
        )
        if row is None:
            raise _not_found(f"no MCP connection with id {item.connection_id}")
        resolved = await resolve_connection(job_store.mcp, row=row)
        connection = resolved.connection
        snapshot = resolved.snapshot
        capabilities = snapshot.tools if snapshot else ()
        rules = {name: _parse_policy(value) for name, value in item.tools.items()}
        if snapshot is not None:
            known = {capability.name for capability in capabilities}
            unknown = sorted(set(rules) - known)
            if unknown:
                # A rule for a tool the server does not offer is either a stale
                # client or a typo; either way storing it would grant nothing
                # now and something unexpected after the next discovery.
                raise _bad_request(f"{connection.label} does not offer: {', '.join(unknown)}")
        binding = binding_from_selection(
            thread_id=thread_id,
            connection=connection,
            alias=item.alias or _default_alias(row, index),
            rules=rules,
            resources={name: _parse_policy(value) for name, value in item.resources.items()},
            prompts={name: _parse_policy(value) for name, value in item.prompts.items()},
            capabilities=tuple(capabilities),
        )
        prepared.append(
            {
                "connection_id": int(row["id"]),
                "alias": binding.alias,
                "enabled": item.enabled and (binding.enabled or item.use_defaults),
                "selection_pending": item.use_defaults,
                "config_revision": binding.config_revision,
                "authorization_epoch": binding.authorization_epoch,
                "capability_revision": binding.capability_revision,
                "policy_revision": binding.policy_revision,
                "rules": {
                    name: {"policy": rule.policy.value, "fingerprint": rule.fingerprint}
                    for name, rule in binding.rules.items()
                },
                "resource_rules": {k: v.value for k, v in binding.resource_rules.items()},
                "prompt_rules": {k: v.value for k, v in binding.prompt_rules.items()},
            }
        )
        aliases.append((item.connection_id, item.alias, _default_alias(row, index)))
    try:
        reserved = []
        if merge:
            changed_ids = {item.connection_id for item in body.bindings}
            reserved = [
                (int(row["connection_id"]), str(row["alias"]), str(row["alias"]))
                for row in await job_store.mcp.list_mcp_bindings(thread_id=thread_id)
                if int(row["connection_id"]) not in changed_ids
            ]
        assigned = binding_aliases([*reserved, *aliases])[len(reserved) :]
        for binding_row, alias in zip(prepared, assigned, strict=True):
            binding_row["alias"] = alias
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    await job_store.mcp.replace_mcp_bindings(thread_id=thread_id, bindings=prepared, merge=merge)
    logger.info(
        "agent_mcp_bindings_replaced",
        extra={"event": "agent_mcp_bindings_replaced", "thread_id": thread_id},
    )
    return BindingsResponse(
        bindings=await _binding_views(job_store, thread_id=thread_id, user_id=user["user_id"])
    )


async def _binding_views(
    job_store: _McpConnectionsStorage, *, thread_id: str, user_id: str
) -> list[BindingResponse]:
    """Render every binding on one thread, with what each connection offers."""
    rows = await job_store.mcp.list_mcp_bindings(thread_id=thread_id)
    views: list[BindingResponse] = []
    for row in rows:
        connection_row = await job_store.mcp.get_mcp_connection(
            connection_id=int(row["connection_id"]), user_id=user_id
        )
        if connection_row is None or connection_row.get("deleted_at") is not None:
            continue
        resolved = await resolve_connection(job_store.mcp, row=connection_row)
        snapshot = resolved.snapshot
        capabilities = (
            [
                _capability_view(_capability_payload(item), item.kind)
                for item in snapshot.all_capabilities()
            ]
            if snapshot
            else []
        )
        views.append(
            BindingResponse(
                connection_id=int(row["connection_id"]),
                connection_label=resolved.connection.label,
                alias=str(row["alias"]),
                enabled=bool(row["enabled"]),
                selection_pending=bool(row.get("selection_pending", False)),
                available=resolved.usable
                and resolved.connection.status is not ConnectionStatus.ERROR,
                error=resolved.error or resolved.connection.error_detail,
                capabilities=capabilities,
                tools={
                    str(name): str(rule.get("policy") if isinstance(rule, Mapping) else rule)
                    for name, rule in (row.get("rules") or {}).items()
                },
                resources={str(k): str(v) for k, v in (row.get("resource_rules") or {}).items()},
                prompts={str(k): str(v) for k, v in (row.get("prompt_rules") or {}).items()},
                config_revision=int(row["config_revision"]),
                authorization_epoch=int(row["authorization_epoch"]),
                capability_revision=int(row["capability_revision"]),
                policy_revision=int(row["policy_revision"]),
            )
        )
    return views
