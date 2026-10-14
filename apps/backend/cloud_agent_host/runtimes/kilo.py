"""Kilo wrapper invocation and native session locator handling."""

from __future__ import annotations

import json

from cloud_agent_shared.runtime_catalog import KILO

from .base import EMPTY_RUNTIME_MCP_CONFIG, RuntimeCapabilities, RuntimeMCPConfig
from .events import RAW, NormalizedEvent
from .generic import GenericRuntime


class KiloRuntime(GenericRuntime):
    """Tier 2: Kilo Code headless, streamed as raw JSON lines.

    Kilo's CLI is an OpenCode fork and keeps its invocation surface
    (``run --format json --auto -m provider/model``) and both of its gaps:
    ``OPENAI_BASE_URL`` is ignored, and startup fetches the models.dev
    catalog. The ``kilo-freeinference`` wrapper closes them the same way the
    OpenCode wrapper does — the fork renamed every kill switch to ``KILO_*``
    but kept the semantics (verified against the pinned binary).

    What the fork adds is more phone-home: PostHog telemetry, session ingest
    and share links to app.kilo.ai, presence, LSP downloads. Each has a
    supported kill switch, and the wrapper sets them all: the sandbox reaches
    the public web, so a path left enabled would simply phone home. Smoke-verified end to end against the pinned CLI: config
    injection, tool calls through the gateway, and JSON events on stdout.
    """

    name = KILO.name

    def __init__(self) -> None:
        """Fix the wrapper invocation and remember Kilo's session id."""
        super().__init__(
            "kilo-freeinference run --format json --auto -m freeinference/{model} {prompt}",
            binary="kilo-freeinference",
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
        """Add the model id the wrapper declares in Kilo's config."""
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
        env["KILO_GATEWAY_MODEL"] = model
        env["KILO_FREEINFERENCE_CONFIG_DIR"] = activation_dir or "/run/agent-activation/kilo"
        if session_state_dir:
            env.update(
                {
                    "XDG_DATA_HOME": f"{session_state_dir}/kilo/data",
                    "XDG_CONFIG_HOME": f"{session_state_dir}/kilo/config",
                    "XDG_CACHE_HOME": f"{session_state_dir}/kilo/cache",
                    "XDG_STATE_HOME": f"{session_state_dir}/kilo/state",
                }
            )
            if resume_session_id:
                argv[-1:-1] = ["--session", resume_session_id]
        if self._check_effort(reasoning_effort) is not None:
            # No command-line flag exists for this: the level belongs in the
            # provider config the wrapper writes, so it travels as environment
            # rather than argv. Absent means the wrapper writes no option at
            # all, which is the request shape this runtime already sent.
            env["KILO_GATEWAY_REASONING_EFFORT"] = str(reasoning_effort)
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
        """Launch the reviewed stdlib bridge around Kilo 7.4.17 serve."""
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
        return ["http-agent-live-bridge", "kilo"], env

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
    EFFORT_LEVELS = KILO.reasoning_efforts

    def _accept_mcp(
        self, config: RuntimeMCPConfig, *, gateway_base_url: str, env: dict[str, str]
    ) -> None:
        """Pass the relay to the image's Kilo build with project sources disabled."""
        self._wrapper_mcp_env(config, gateway_base_url=gateway_base_url, env=env)

    def capabilities(self) -> RuntimeCapabilities:
        """Kilo supports the same platform MCP surface as every other runtime."""
        return RuntimeCapabilities(tier=2, reasoning_efforts=self.EFFORT_LEVELS, mcp=KILO.mcp)
