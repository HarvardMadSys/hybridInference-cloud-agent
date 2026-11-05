"""The OpenCode family: a CLI's ``serve`` behind the reviewed stdlib HTTP bridge."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from .base import EMPTY_RUNTIME_MCP_CONFIG, AgentRuntime, RuntimeCapabilities, RuntimeMCPConfig

if TYPE_CHECKING:
    from cloud_agent_shared.runtime_catalog import RuntimeDefinition


class HttpBridgeRuntime(AgentRuntime):
    """A CLI of the OpenCode family, driven through ``http-agent-live-bridge``.

    OpenCode and its fork Kilo are configured the same way under their own
    names. The image's ``<name>-freeinference`` wrapper writes the CLI's
    provider config from ``<NAME>_*`` variables, and the bridge runs the CLI's
    ``serve`` and speaks its HTTP API for the session, so the argv names only
    the bridge and the CLI. Sessions are read by ``drivers.HttpAgentLiveSession``.
    """

    #: The runtime's entry in the shared catalog: its name, MCP support and efforts.
    catalog: ClassVar[RuntimeDefinition]

    def __init__(self) -> None:
        """Remember the CLI's native session id."""
        self.session_id: str | None = None

    def prepare_live(
        self,
        *,
        workdir: str,
        model: str,
        model_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Launch the reviewed stdlib bridge around the pinned CLI's ``serve``."""
        del provides_isolation
        effort = self._check_effort(reasoning_effort)
        env = self._wrapper_env(model_base_url, credential)
        self._accept_mcp(mcp_config, model_base_url=model_base_url, env=env)
        prefix = self.name.upper()
        env[f"{prefix}_GATEWAY_MODEL"] = model
        env[f"{prefix}_FREEINFERENCE_CONFIG_DIR"] = (
            activation_dir or f"/run/agent-activation/{self.name}"
        )
        if session_state_dir:
            # The CLI stores its sessions in its XDG data root. Relocating
            # every XDG root keeps all native continuation data inside the
            # Docker-owned /agent-state tree and out of the repository.
            env.update(
                {
                    f"XDG_{root.upper()}_HOME": f"{session_state_dir}/{self.name}/{root}"
                    for root in ("data", "config", "cache", "state")
                }
            )
        if effort is not None:
            # No command-line flag exists for this: the level belongs in the
            # provider config the wrapper writes, so it travels as environment
            # rather than argv. Absent means the wrapper writes no option at
            # all, which is the request shape this runtime already sent. The AI
            # SDK provider passes it through verbatim without validating it,
            # the same posture as Codex: verified against the pinned CLI, the
            # config's ``options.reasoningEffort`` arrives as
            # ``reasoning_effort`` in the chat-completions body, and nothing
            # else in the config does.
            env[f"{prefix}_GATEWAY_REASONING_EFFORT"] = effort
        if resume_session_id:
            env["AGENT_LIVE_SESSION_ID"] = resume_session_id
        env["AGENT_LIVE_WORKDIR"] = workdir
        return ["http-agent-live-bridge", self.name], env

    def _accept_mcp(
        self, config: RuntimeMCPConfig, *, model_base_url: str, env: dict[str, str]
    ) -> None:
        """Hand the relay endpoints to the reviewed wrapper.

        Both halves hold, and both are checked against the pinned binary rather
        than assumed: the wrapper writes the platform's own ``<NAME>_CONFIG``,
        and it exports ``<NAME>_DISABLE_PROJECT_CONFIG``, which is the flag that
        stops the repository's own config from being merged afterwards. Without
        the second, the first would be one MCP source among several and a
        committed file could add its own servers.
        """
        self._wrapper_mcp_env(config, model_base_url=model_base_url, env=env)

    def capabilities(self) -> RuntimeCapabilities:
        """Take the platform's MCP relay and a verified effort level."""
        return RuntimeCapabilities(
            reasoning_efforts=self.catalog.reasoning_efforts, mcp=self.catalog.mcp
        )
