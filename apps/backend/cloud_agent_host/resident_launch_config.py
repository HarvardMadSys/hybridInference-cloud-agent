"""The process-fixed launch a sandbox manager is asked to start a member with.

Moved out of ``sandbox_manager`` unchanged but for the owner model connection
fields, which are new: a member running on an owner's own connection is
launched at that connection's exact Base URL, never at this host's Gateway
relay, and with that connection's identity fixed beside its model.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.environment import (  # noqa: TC001 — Pydantic resolves annotations at runtime.
    BoundEnvironment,
)
from cloud_agent_shared.model_connections import MAX_BASE_URL_CHARS
from cloud_agent_shared.plugins import MAX_SANDBOX_PLUGINS, SandboxPlugin


class SharedResidentLaunchConfig(BaseModel):
    """Process-fixed configuration and renewable residency credential."""

    model_config = ConfigDict(extra="forbid")
    environment: BoundEnvironment | None = Field(None, repr=False)
    thread_id: str | None = Field(None, max_length=128)

    runtime_id: str = Field(..., min_length=1, max_length=128)
    runtime_version: str | None = Field(None, max_length=128)
    native_session_id: str | None = Field(None, max_length=512)
    model: str = Field(..., min_length=1, max_length=256)
    reasoning_effort: str | None = Field(None, max_length=64)
    # The sandbox manager replaces this with its host-local relay address when one is
    # configured.  The fallback keeps injected/unit-test sandbox managers usable.
    gateway_base_url: str = Field("", max_length=2048)
    # Never in a repr: a Gateway grant, or an owner connection's own key.
    credential: str | None = Field(None, max_length=16384, repr=False)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=64)
    plugins: list[SandboxPlugin] = Field(default_factory=list, max_length=MAX_SANDBOX_PLUGINS)
    # Replaced, like the gateway's, by this host's own MCP relay address.
    mcp_base_url: str = Field("", max_length=2048)
    mcp_credential: str = Field("", max_length=16384, repr=False)
    github_token: str | None = Field(None, max_length=16384, repr=False)
    github_repo: str = Field("", max_length=512)
    slot_id: int = Field(..., ge=0, le=127)
    residency_generation: int = Field(0, ge=0)
    # An owner model connection: used exactly, never replaced by this host's
    # relay. Empty for a Gateway member, which is every launch from a runner or
    # control plane that predates these fields.
    inference_base_url: str = Field(
        "", max_length=MAX_BASE_URL_CHARS, pattern=r"^(https://[!-~]+)?$"
    )
    inference_protocol: Literal["", "chat_completions", "responses", "anthropic_messages"] = ""
    inference_source: str = Field("", max_length=128, pattern=r"^(mconn_[0-9a-f]{32}@[0-9]+)?$")
    # The CLI's own protocol on a translated plan (Codex or Claude Code on a
    # Chat Completions connection); empty is direct.
    inference_client_protocol: Literal["", "responses", "anthropic_messages"] = ""
    # That member's own loopback token, issued by the control plane; a secret.
    inference_local_token: str = Field("", pattern=r"^([0-9a-f]{64})?$", repr=False)


def resident_launch(
    thread_id: str,
    body: SharedResidentLaunchConfig,
) -> ResidentLaunch:
    """The member launch a request names, at this host's own relay addresses.

    The Gateway and MCP relay addresses are this host's, so the CLI is told
    the ones the host rules admit for its group. An owner connection's Base
    URL is kept as sent: it is a public HTTPS endpoint the group reaches
    through the same egress as any other, not a relay.
    """
    gateway_base_url = (os.environ.get("AGENT_GATEWAY_URL") or body.gateway_base_url).strip()
    if not gateway_base_url:
        raise SandboxError("resident launch has no sandbox-reachable gateway URL")
    return ResidentLaunch(
        thread_id=thread_id,
        runtime_id=body.runtime_id,
        runtime_version=body.runtime_version,
        native_session_id=body.native_session_id,
        model=body.model,
        reasoning_effort=body.reasoning_effort,
        gateway_base_url=gateway_base_url,
        credential=body.credential,
        plugins=tuple(plugin.model_dump(exclude_none=True) for plugin in body.plugins),
        mcp_server_ids=tuple(body.mcp_server_ids),
        # Like the gateway's, the relay is this host's own, so the address
        # a CLI is told is the one the host rules admit for its group. Only
        # a launch with tools has one to name.
        mcp_base_url=(
            (os.environ.get("AGENT_SANDBOX_MCP_URL") or body.mcp_base_url).strip()
            if body.mcp_server_ids
            else ""
        ),
        mcp_credential=body.mcp_credential,
        github_token=body.github_token,
        github_repo=body.github_repo,
        environment=body.environment,
        slot_id=body.slot_id,
        residency_generation=body.residency_generation,
        inference_base_url=body.inference_base_url,
        inference_protocol=body.inference_protocol,
        inference_source=body.inference_source,
        inference_client_protocol=body.inference_client_protocol,
        inference_local_token=body.inference_local_token,
    )
