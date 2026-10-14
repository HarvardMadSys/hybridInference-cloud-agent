"""OpenCode wrapper invocation and native session locator handling."""

from __future__ import annotations

import json

from cloud_agent_shared.runtime_catalog import OPENCODE

from .base import EMPTY_RUNTIME_MCP_CONFIG, RuntimeCapabilities, RuntimeMCPConfig
from .events import RAW, NormalizedEvent
from .generic import GenericRuntime


class OpencodeRuntime(GenericRuntime):
    """Tier 2: OpenCode headless, streamed as raw JSON lines.

    Two verified facts shape the invocation. OpenCode ignores
    ``OPENAI_BASE_URL``, and its built-in ``openai`` provider speaks the
    Responses API; the ``opencode-freeinference`` wrapper instead declares a
    provider over the **bundled** ``@ai-sdk/openai-compatible`` package
    (chat-completions dialect, nothing downloaded at run time). And its
    startup fetch of the models.dev catalog hard-fails offline, so the
    wrapper disables it and declares the model in the config — without which
    every sandboxed run dies before the first request.

    ``--auto`` is the same lesson as Claude Code's bypassPermissions: the
    sandbox is the boundary, and an interactive permission gate inside it
    only guarantees the agent cannot do the work.
    """

    name = OPENCODE.name

    def __init__(self) -> None:
        """Fix the wrapper invocation and remember OpenCode's session id."""
        super().__init__(
            "opencode-freeinference run --format json --auto -m freeinference/{model} {prompt}",
            binary="opencode-freeinference",
        )
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
        """Add the model id the wrapper declares in OpenCode's config."""
        argv, env = super().prepare(
            workdir=workdir,
            task_prompt=task_prompt,
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
        )
        env["OPENCODE_GATEWAY_MODEL"] = model
        env["OPENCODE_FREEINFERENCE_CONFIG_DIR"] = (
            activation_dir or "/run/agent-activation/opencode"
        )
        if session_state_dir:
            # OpenCode 1.18.9 stores sessions in its XDG data root. Relocating
            # every XDG root keeps all native continuation data inside the
            # Docker-owned /agent-state tree and out of the repository.
            env.update(
                {
                    "XDG_DATA_HOME": f"{session_state_dir}/opencode/data",
                    "XDG_CONFIG_HOME": f"{session_state_dir}/opencode/config",
                    "XDG_CACHE_HOME": f"{session_state_dir}/opencode/cache",
                    "XDG_STATE_HOME": f"{session_state_dir}/opencode/state",
                }
            )
            if resume_session_id:
                argv[-1:-1] = ["--session", resume_session_id]
        if self._check_effort(reasoning_effort) is not None:
            # No command-line flag exists for this: the level belongs in the
            # provider config the wrapper writes, so it travels as environment
            # rather than argv. Absent means the wrapper writes no option at
            # all, which is the request shape this runtime already sent.
            env["OPENCODE_GATEWAY_REASONING_EFFORT"] = str(reasoning_effort)
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
        """Launch the reviewed stdlib bridge around OpenCode 1.18.9 serve."""
        _argv, env = self.prepare(
            workdir=workdir,
            task_prompt="",
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            session_state_dir=session_state_dir,
            resume_session_id=None,
            activation_dir=activation_dir,
        )
        if resume_session_id:
            env["AGENT_LIVE_SESSION_ID"] = resume_session_id
        env["AGENT_LIVE_WORKDIR"] = workdir
        return ["http-agent-live-bridge", "opencode"], env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Keep Tier-2 output raw while capturing its native session locator."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return self._raw(stripped, "not json")
        if isinstance(event, dict):
            captured = str(event.get("sessionID") or "").strip()
            if captured:
                self.session_id = captured
        return NormalizedEvent(RAW, {"text": stripped[:4000]})

    #: Passed through to the AI SDK provider verbatim, which does not validate
    #: it — same posture as codex. The real constraint is the intersection with
    #: the model's own declared domain. Verified against the pinned CLI: the
    #: config's ``options.reasoningEffort`` arrives as ``reasoning_effort`` in
    #: the chat-completions body, and nothing else in the config does.
    EFFORT_LEVELS = OPENCODE.reasoning_efforts

    def _accept_mcp(
        self, config: RuntimeMCPConfig, *, gateway_base_url: str, env: dict[str, str]
    ) -> None:
        """Hand the relay endpoints to the reviewed wrapper.

        Both halves hold, and both are checked against the pinned binary rather
        than assumed: the wrapper writes the platform's own ``OPENCODE_CONFIG``,
        and it exports ``OPENCODE_DISABLE_PROJECT_CONFIG``, which is the flag
        that stops the repository's ``opencode.json`` and ``.opencode/`` from
        being merged afterwards. Without the second, the first would be one MCP
        source among several and a committed file could add its own servers.
        """
        self._wrapper_mcp_env(config, gateway_base_url=gateway_base_url, env=env)

    def capabilities(self) -> RuntimeCapabilities:
        """Tier 2 mechanics, but this one control is verified and wired."""
        return RuntimeCapabilities(tier=2, reasoning_efforts=self.EFFORT_LEVELS, mcp=OPENCODE.mcp)
