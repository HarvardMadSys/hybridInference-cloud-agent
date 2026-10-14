"""Codex invocation and observed JSON event normalization."""

from __future__ import annotations

import json

from cloud_agent_shared.runtime_catalog import CODEX

from .base import (
    EMPTY_RUNTIME_MCP_CONFIG,
    MCP_TOKEN_ENV,
    AgentRuntime,
    RuntimeCapabilities,
    RuntimeMCPConfig,
)
from .events import (
    DIFF,
    ERROR,
    LIFECYCLE,
    MESSAGE,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    NormalizedEvent,
    _clipped,
    _truncate,
)


class CodexRuntime(AgentRuntime):
    """OpenAI Codex headless (``codex exec --json``).

    Speaks the OpenAI surface, configured through command-line provider flags
    so nothing writes to or depends on the operator's own Codex config.
    """

    name = CODEX.name
    binary = "codex"

    def __init__(self) -> None:
        """Remember the native thread locator announced by ``--json``."""
        self.session_id: str | None = None

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
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
        """Build the headless invocation and its environment.

        ``reasoning_effort`` becomes ``-c model_reasoning_effort="<level>"``
        among the provider flags below. Verified against the pinned
        ``CODEX_VERSION`` (0.145.0) rather than assumed: the CLI puts it on the
        wire as ``reasoning.effort`` in the ``/v1/responses`` body, which is
        exactly where the gateway's Responses translation reads it. See the
        recorded capture in ``tests/fixtures/agent_runtime_streams``.

        MCP uses the merged session relay, authenticated through the member's
        environment. The trusted host installs the exact relay URL into the
        root-owned system requirements before the CLI starts, for both exec
        and app-server. Repository configuration cannot admit another server.

        Mirrors the invocation the gateway's Codex on-call workflow ran in
        production, because that one was known to work against this gateway.
        Three things it gets right that the first pass here did not:

        - the model rides ``--model``. The previous version set ``CODEX_MODEL``,
          which is not a variable the CLI reads, so the owner's model choice was
          silently dropped and Codex used whatever its own default was.
        - ``wire_api = "responses"``, not ``"chat"``. Codex removed chat wire
          support upstream (openai/codex#7782); our gateway serves
          ``/v1/responses`` and translates southbound.
        - the provider is configured with ``-c`` flags rather than a config file.
          ``--ignore-user-config`` keeps the operator's own config out, while a
          supplied ``session_state_dir`` becomes ``CODEX_HOME`` so Codex's
          rollout files survive a process exit and ``exec resume`` can find
          them on the next turn.
        """
        _ = activation_dir
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        provider = "hybridinference"
        argv = [
            self.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            # The operator's own Codex config must not reach a sandbox run.
            "--ignore-user-config",
            # A container backend is already the boundary. Running Codex's
            # bubblewrap sandbox inside that non-root, capability-dropped
            # container cannot create its user namespace, so every shell
            # command fails before it starts. The process backend has no such
            # outer boundary and must retain Codex's workspace sandbox.
            "--sandbox",
            "danger-full-access" if provides_isolation else "workspace-write",
            "--model",
            model,
            # Beside the other -c overrides, and built by the dialect helper so
            # every resident launch uses exactly the same spelling.
            *self.effort_args(reasoning_effort),
            *self._mcp_args(mcp_config, gateway_base_url=gateway_base_url),
            "-c",
            f'model_provider="{provider}"',
            "-c",
            f'model_providers.{provider}.name="HybridInference"',
            "-c",
            f'model_providers.{provider}.base_url="{base}/v1"',
            "-c",
            f'model_providers.{provider}.env_key="CODEX_API_KEY"',
            "-c",
            f'model_providers.{provider}.wire_api="responses"',
        ]
        # 0.145.0 accepts the exec-wide flags above before its ``resume``
        # subcommand. Keeping one option list prevents fresh and resumed turns
        # from drifting in provider, model, sandbox, or reasoning behaviour.
        if resume_session_id:
            argv += ["resume", resume_session_id, task_prompt]
        else:
            argv.append(task_prompt)
        env = {
            "CODEX_API_KEY": credential,
            MCP_TOKEN_ENV: mcp_config.credential,
            # Some code paths still read the OpenAI names; keep them consistent
            # rather than leaving a second, stale credential source.
            "OPENAI_BASE_URL": f"{base}/v1",
            "OPENAI_API_KEY": credential,
        }
        if session_state_dir:
            # Codex requires CODEX_HOME itself to exist. /agent-state is made
            # by the image (or is the state mount), so use that exact root
            # rather than naming a child the CLI would refuse to create.
            env["CODEX_HOME"] = session_state_dir
        return argv, env

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
        command-line overrides, exactly as in :meth:`prepare`, and the root-owned
        managed MCP allowlist installed before launch remains the final policy
        layer for app-server (which has no ``--ignore-user-config`` flag).
        """
        _ = (workdir, model, provides_isolation, resume_session_id, activation_dir)
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        provider = "hybridinference"
        argv = [
            self.binary,
            *self.effort_args(reasoning_effort),
            *self._mcp_args(mcp_config, gateway_base_url=gateway_base_url),
            "-c",
            f'model_provider="{provider}"',
            "-c",
            f'model_providers.{provider}.name="HybridInference"',
            "-c",
            f'model_providers.{provider}.base_url="{base}/v1"',
            "-c",
            f'model_providers.{provider}.env_key="CODEX_API_KEY"',
            "-c",
            f'model_providers.{provider}.wire_api="responses"',
            "app-server",
        ]
        env = {
            "CODEX_API_KEY": credential,
            MCP_TOKEN_ENV: mcp_config.credential,
            "OPENAI_BASE_URL": f"{base}/v1",
            "OPENAI_API_KEY": credential,
        }
        if session_state_dir:
            env["CODEX_HOME"] = session_state_dir
        return argv, env

    def _mcp_args(self, config: RuntimeMCPConfig, *, gateway_base_url: str) -> list[str]:
        """Use the same authenticated relay configuration in exec and app-server."""
        endpoints = self.relay_urls(config, gateway_base_url=gateway_base_url)
        entries = []
        for name, url in endpoints.items():
            entries.append(
                f"{name} = {{ url = {json.dumps(url)}, "
                f'bearer_token_env_var = "{MCP_TOKEN_ENV}", '
                "required = true, startup_timeout_sec = 30, tool_timeout_sec = 1560 }"
            )
        return ["-c", "mcp_servers = { " + ", ".join(entries) + " }"]

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Normalize one ``codex exec --json`` line."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._raw(line, "not json")
        if not isinstance(event, dict):
            return self._raw(line, "not an object")

        kind = event.get("type") or ""
        item = event.get("item") or {}
        item_type = item.get("type") or item.get("item_type")

        if kind == "thread.started":
            captured = str(event.get("thread_id") or "").strip()
            if captured:
                self.session_id = captured
            return NormalizedEvent(
                LIFECYCLE,
                {"phase": "init", "session_id": self.session_id},
            )

        if kind.startswith("item.") and item_type:
            if item_type == "command_execution" and kind == "item.completed":
                output = (
                    item.get("aggregated_output") or item.get("output") or item.get("stdout") or ""
                )
                exit_code = item.get("exit_code")
                return NormalizedEvent(
                    TOOL_RESULT,
                    {
                        "tool_use_id": item.get("id"),
                        "is_error": isinstance(exit_code, int) and exit_code != 0,
                        "content": _truncate(output),
                        "exit_code": exit_code,
                    },
                )
            if item_type in ("command_execution", "function_call", "tool_call"):
                return NormalizedEvent(
                    TOOL_USE,
                    {
                        "name": item.get("name") or item_type,
                        "id": item.get("id"),
                        "input": item.get("command") or item.get("arguments"),
                    },
                )
            if item_type in ("agent_message", "assistant_message"):
                return NormalizedEvent(
                    MESSAGE,
                    {
                        "text": _truncate(item.get("text")),
                        "truncated": _clipped(item.get("text")),
                    },
                )
            if item_type in ("reasoning", "thinking"):
                return NormalizedEvent(THINKING, {"text": _truncate(item.get("text"))})
            if item_type in ("patch", "file_change"):
                return NormalizedEvent(DIFF, {"text": _truncate(item.get("changes"))})
        if kind in ("turn.completed", "session.completed", "thread.completed"):
            return NormalizedEvent(LIFECYCLE, {"phase": "result", "usage": event.get("usage")})
        if kind in ("error", "turn.failed"):
            return NormalizedEvent(ERROR, {"text": _truncate(event.get("error") or event)})
        return self._raw(line, f"unknown type {kind!r}")

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
        """Codex is Tier 1 for events; cost comes from the gateway ledger."""
        return RuntimeCapabilities(
            cost_report=False,
            normalized_events=True,
            tier=1,
            reasoning_efforts=self.EFFORT_LEVELS,
            mcp=CODEX.mcp,
        )
