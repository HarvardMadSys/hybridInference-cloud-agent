"""The process-fixed launch a sandbox manager is asked to start a member with.

Every member runs on a model connection, with the connection's identity fixed
beside its model. On its owner's own connection it is launched at that
connection's exact Base URL. On one the deployment provides it is launched at
the same path on this host's model relay, which is where the deployment's key
is added.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_shared.environment import (  # noqa: TC001 — Pydantic resolves annotations at runtime.
    BoundEnvironment,
)
from cloud_agent_shared.model_connections import (
    MAX_BASE_URL_CHARS,
    MODEL_RELAY_URL_ENV,
    relayed_base_url,
)
from cloud_agent_shared.plugins import MAX_SANDBOX_PLUGINS, SandboxPlugin
from cloud_agent_shared.sandbox_mcp import MAX_SANDBOX_MCP_SERVERS, SandboxMcpServer


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
    # Never in a repr: the owner connection's own key.
    credential: str | None = Field(None, max_length=16384, repr=False)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=64)
    plugins: list[SandboxPlugin] = Field(default_factory=list, max_length=MAX_SANDBOX_PLUGINS)
    # Replaced by this host's own MCP relay address.
    mcp_base_url: str = Field("", max_length=2048)
    mcp_credential: str = Field("", max_length=16384, repr=False)
    sandbox_mcp_servers: list[SandboxMcpServer] = Field(
        default_factory=list, max_length=MAX_SANDBOX_MCP_SERVERS
    )
    github_credential: str = Field("", max_length=16384, repr=False)
    github_repo: str = Field("", max_length=512)
    git_author_name: str = Field("", max_length=256)
    git_author_email: str = Field("", max_length=512)
    git_coauthor: str = Field("", max_length=1024)
    slot_id: int = Field(..., ge=0, le=127)
    residency_generation: int = Field(0, ge=0)
    # The owner model connection: used exactly, never replaced by a relay.
    inference_base_url: str = Field(
        "", max_length=MAX_BASE_URL_CHARS, pattern=r"^(https://[!-~]+)?$"
    )
    inference_protocol: Literal["", "chat_completions", "responses", "anthropic_messages"] = ""
    inference_source: str = Field("", max_length=128, pattern=r"^([mo]conn_[0-9a-f]{32}@[0-9]+)?$")
    # ``"1"`` on a connection the deployment provides: the member is launched
    # at this host's model relay, at the connection's own path.
    inference_relayed: Literal["", "1"] = ""
    # The CLI's own protocol on a translated plan (Codex or Claude Code on a
    # Chat Completions connection); empty is direct.
    inference_client_protocol: Literal["", "responses", "anthropic_messages"] = ""
    # That member's own loopback token, issued by the control plane; a secret.
    inference_local_token: str = Field("", pattern=r"^([0-9a-f]{64})?$", repr=False)


def model_relay_url() -> str:
    """This host's model relay as its sandboxes reach it; empty when it has none.

    An ``http`` origin on the group's own bridge (``AGENT_SANDBOX_MODEL_RELAY_URL``),
    the same value the sandbox network admits as a relay.
    """
    return (os.environ.get(MODEL_RELAY_URL_ENV) or "").strip().rstrip("/")


def model_relay_base_url(base_url: str) -> str:
    """Where a member on a provided connection is launched: its path on this host's relay."""
    relay = model_relay_url()
    if not relay.startswith("http://") or "/" in relay.removeprefix("http://"):
        raise SandboxError(
            f"{MODEL_RELAY_URL_ENV} must name this host's model relay as a plain http origin"
        )
    return relayed_base_url(base_url, relay)


def resident_launch(
    thread_id: str,
    body: SharedResidentLaunchConfig,
) -> ResidentLaunch:
    """The member launch a request names, at this host's own MCP relay address.

    The MCP relay address is this host's, so the CLI is told the one the host
    rules admit for its group. An owner connection's Base URL is kept as sent:
    it is a public HTTPS endpoint the group reaches through the same egress as
    any other, not a relay. A provided connection's is replaced by its path on
    this host's model relay, the only place its key exists.
    """
    if not body.inference_source:
        raise SandboxError("a resident launch needs its owner's model connection")
    if bool(body.inference_relayed) != body.inference_source.startswith("oconn_"):
        raise SandboxError("a provided model connection, and only one, is relayed")
    return ResidentLaunch(
        thread_id=thread_id,
        runtime_id=body.runtime_id,
        runtime_version=body.runtime_version,
        native_session_id=body.native_session_id,
        model=body.model,
        reasoning_effort=body.reasoning_effort,
        credential=body.credential,
        plugins=tuple(plugin.model_dump(exclude_none=True) for plugin in body.plugins),
        mcp_server_ids=tuple(body.mcp_server_ids),
        # The relay is this host's own, so the address a CLI is told is the
        # one the host rules admit for its group. Only a launch with tools has
        # one to name.
        mcp_base_url=(
            (os.environ.get("AGENT_SANDBOX_MCP_URL") or body.mcp_base_url).strip()
            if body.mcp_server_ids
            else ""
        ),
        mcp_credential=body.mcp_credential,
        sandbox_mcp_servers=tuple(body.sandbox_mcp_servers) if body.mcp_server_ids else (),
        github_credential=body.github_credential,
        github_repo=body.github_repo,
        git_author_name=body.git_author_name,
        git_author_email=body.git_author_email,
        git_coauthor=body.git_coauthor,
        environment=body.environment,
        slot_id=body.slot_id,
        residency_generation=body.residency_generation,
        inference_base_url=(
            model_relay_base_url(body.inference_base_url)
            if body.inference_relayed
            else body.inference_base_url
        ),
        inference_relayed=body.inference_relayed,
        inference_protocol=body.inference_protocol,
        inference_source=body.inference_source,
        inference_client_protocol=body.inference_client_protocol,
        inference_local_token=body.inference_local_token,
    )
