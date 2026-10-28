"""Codex app-server invocation: provider flags, the MCP relay entry and effort."""

from __future__ import annotations

import json

from cloud_agent_shared.runtime_catalog import CODEX

from .base import (
    COMPAT_ORIGIN,
    EMPTY_RUNTIME_MCP_CONFIG,
    MCP_TOKEN_ENV,
    AgentRuntime,
    ResponsesViaChatUrl,
    RuntimeCapabilities,
    RuntimeMCPConfig,
    compat_cli_credential,
    compat_launch,
    openai_base_url,
)


class CodexRuntime(AgentRuntime):
    """OpenAI Codex's ``app-server``, over stdio.

    Speaks the OpenAI surface, configured through command-line provider flags
    so nothing writes to or depends on the operator's own Codex config. Its
    events are read by ``drivers.CodexAppServerSession``, not here.
    """

    name = CODEX.name
    binary = "codex"

    def __init__(self) -> None:
        """Remember the native thread locator the app-server announces."""
        self.session_id: str | None = None

    def prepare_live(
        self,
        *,
        workdir: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
        session_state_dir: str | None = None,
        resume_session_id: str | None = None,
        activation_dir: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the pinned 0.145.0 stdio app-server transport.

        Thread creation/resume is an RPC after the initialize handshake, so
        ``model``, ``workdir`` and ``resume_session_id`` intentionally do not
        become positional CLI arguments here.  Provider configuration remains
        command-line overrides, and the root-owned
        managed MCP allowlist installed before launch remains the final policy
        layer for app-server (which has no ``--ignore-user-config`` flag).
        """
        _ = (workdir, provides_isolation, resume_session_id, activation_dir)
        # An owner connection's exact URL, or the Gateway's normalized one. The
        # provider id stays the one ``CodexAppServerSession`` names in its
        # thread RPC; only where it points changes. Both speak Responses,
        # which is the only wire Codex has -- on a Chat Completions connection
        # through the member's compatibility helper, which this URL then names.
        translated = isinstance(gateway_base_url, ResponsesViaChatUrl)
        base_url = f"{COMPAT_ORIGIN}/v1" if translated else openai_base_url(gateway_base_url)
        cli_credential = compat_cli_credential(credential) if translated else credential
        provider = "hybridinference"
        argv = [
            self.binary,
            *self.effort_args(reasoning_effort),
            *(self.TRANSLATED_ARGS if translated else ()),
            *self._mcp_args(mcp_config, gateway_base_url=gateway_base_url),
            "-c",
            f'model_provider="{provider}"',
            "-c",
            f'model_providers.{provider}.name="HybridInference"',
            "-c",
            f"model_providers.{provider}.base_url={json.dumps(base_url)}",
            "-c",
            f'model_providers.{provider}.env_key="CODEX_API_KEY"',
            "-c",
            f'model_providers.{provider}.wire_api="responses"',
            "app-server",
        ]
        env = {
            "CODEX_API_KEY": cli_credential,
            MCP_TOKEN_ENV: mcp_config.credential,
            "OPENAI_BASE_URL": base_url,
            "OPENAI_API_KEY": cli_credential,
        }
        if session_state_dir:
            env["CODEX_HOME"] = session_state_dir
        if isinstance(gateway_base_url, ResponsesViaChatUrl):
            return compat_launch(argv, env, gateway_base_url, credential, model=model)
        return argv, env

    #: What a translated Codex leaves out of its requests: 0.145.0 otherwise
    #: offers the hosted ``web_search`` tool, which only a Responses provider
    #: runs; a Chat Completions upstream has no such tool, and the helper
    #: refuses a request carrying one rather than dropping it. Every function
    #: tool stays, namespaced ones (sub-agents, MCP) included.
    TRANSLATED_ARGS = ("-c", 'web_search="disabled"')

    def _mcp_args(self, config: RuntimeMCPConfig, *, gateway_base_url: str) -> list[str]:
        """The authenticated relay entry, as a ``-c`` override; empty without servers."""
        endpoints = self.relay_urls(config, gateway_base_url=gateway_base_url)
        entries = []
        for name, url in endpoints.items():
            entries.append(
                f"{name} = {{ url = {json.dumps(url)}, "
                f'bearer_token_env_var = "{MCP_TOKEN_ENV}", '
                "required = true, startup_timeout_sec = 30, tool_timeout_sec = 1560 }"
            )
        return ["-c", "mcp_servers = { " + ", ".join(entries) + " }"]

    #: What ``model_reasoning_effort`` may be set to. Codex 0.145.0 does **not**
    #: validate this locally -- ``--strict-config`` checks the key's name and
    #: nothing else, and a nonsense value reaches the wire unchallenged -- so
    #: this list is not the CLI enforcing anything. It is the vocabulary the
    #: CLI is documented to use, and the real constraint is the intersection
    #: with the model's own declared domain, which is where a level the model
    #: never heard of gets refused.
    EFFORT_LEVELS = CODEX.reasoning_efforts

    def effort_args(self, effort: str | None) -> list[str]:
        """Express effort as a ``-c model_reasoning_effort`` override."""
        if self._check_effort(effort) is None:
            return []
        return ["-c", f'model_reasoning_effort="{effort}"']

    def capabilities(self) -> RuntimeCapabilities:
        """Codex takes the platform's MCP relay and an effort level."""
        return RuntimeCapabilities(reasoning_efforts=self.EFFORT_LEVELS, mcp=CODEX.mcp)
