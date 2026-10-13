"""pi wrapper invocation and observed JSON event normalization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.runtime_catalog import PI

from .base import EMPTY_RUNTIME_MCP_CONFIG, RuntimeCapabilities, RuntimeMCPConfig
from .events import (
    ERROR,
    LIFECYCLE,
    MESSAGE,
    RAW,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    USAGE,
    NormalizedEvent,
    _clipped,
    _truncate,
)
from .generic import GenericRuntime
from .preview import ReplyPreview

if TYPE_CHECKING:
    from collections.abc import Callable


#: The reviewed MCP bridge, shipped in the sandbox image and loaded explicitly.
#: It lives beside the other image helpers rather than in the workspace, so the
#: code that talks to the relay is the code the image was built and reviewed
#: with. Verified against pi 0.73.1: with `--no-extensions -e <this>` the relay
#: is discovered at its merged endpoint, the tools it lists are registered under
#: that one entry's name followed by the relay's own `<alias>__<tool>`, a call
#: reaches the upstream with the bare name, and a repository's own
#: `.pi/extensions/*.ts` does not load.
PI_MCP_BRIDGE = "/usr/local/libexec/pi-mcp-bridge.ts"


class PiRuntime(GenericRuntime):
    """Tier 1: the pi coding agent, ``--mode json`` normalized.

    pi ignores ``OPENAI_BASE_URL`` — its built-in ``openai`` provider goes
    straight to api.openai.com (verified against a local fake: zero hits, a
    real OpenAI 401). The supported route is a custom provider in
    ``~/.pi/agent/models.json``, so the sandbox image ships a reviewed
    ``pi-freeinference`` wrapper that writes that file from this environment
    and then ``exec``s the real CLI. The prompt stays in argv end to end;
    nothing user-controlled passes through a shell.

    Promoted from Tier 2 on the condition the tier design set: real jobs
    proved the format. Passing the stream through as ``raw`` was not a quiet
    downgrade but a silent one — ``raw`` renders nowhere in the thread and
    only ``message`` events reach ``agent_thread_messages``, so five finished
    staging runs (thinking, a bash call, a full answer each) showed the owner
    an empty conversation and "completed (no changes)". The mapping below is
    derived from those recorded runs, not from documentation; the fixture is a
    sanitized replay of one.
    """

    name = PI.name

    # pi streams a completed block twice: once as deltas, then whole. The
    # ``*_end`` sub-events are what is mapped — the recorded run's 300
    # ``message_update`` lines carry 4 completed blocks between them, and
    # emitting per delta would be the thinking_tokens flood again with a
    # different name. Answer text deltas only feed the batched preview.
    _BLOCK_END_KINDS = frozenset({"thinking_end", "text_end", "toolcall_end"})

    # Recognized and deliberately not emitted. ``turn_end`` and ``agent_end``
    # repeat the whole assistant message that ``*_end`` already reported, and
    # the ``tool_execution_*`` trio repeats the tool call and its output; the
    # authoritative copies carry ``isError``, which those do not.
    _SILENT_KINDS = frozenset(
        {
            "agent_start",
            "turn_start",
            "turn_end",
            "message_start",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }
    )

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        """Fix the wrapper invocation and remember pi's native session id."""
        super().__init__(
            "pi-freeinference --provider freeinference --model {model} "
            # pi has no native MCP client at the pinned version; MCP arrives
            # through executable extensions, and a project-local one is as easy
            # to commit as a file in the checkout. `--no-extensions` disables
            # discovery entirely -- the project's `.pi/extensions/`, the global
            # directory, and anything a settings file names -- leaving the
            # explicit `-e` below as the only way in.
            "--mode json --no-extensions "
            f"-e {PI_MCP_BRIDGE} "
            "-p {prompt}",
            binary="pi-freeinference",
        )
        # One adapter instance per job (see get_runtime), so the suppression
        # in _from_message_update is per-run and cannot leak across jobs.
        self._reported_subtypes: set[str] = set()
        self.session_id: str | None = None
        self._preview = ReplyPreview(clock)
        # pi gives its messages no id of its own, and the provider's
        # ``responseId`` is absent from ``message_start``; counting messages
        # gives each text block a preview id that its ``text_end`` can name again.
        self._message_no = 0

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
        """Add the model id the wrapper writes into pi's provider config."""
        argv, env = super().prepare(
            workdir=workdir,
            task_prompt=task_prompt,
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            # The relay endpoints are published below rather than by the base
            # class, because pi is the one runtime whose MCP client is not the
            # CLI: the bridge extension reads them, and handing a server set to
            # a CLI that cannot read it would be a silent no-op.
            mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
            reasoning_effort=reasoning_effort,
        )
        self._accept_mcp(mcp_config, gateway_base_url=gateway_base_url, env=env)
        # models.json wants the model listed under the provider; the wrapper
        # cannot parse it back out of pi's argv without reimplementing pi's
        # option handling, so hand it over explicitly.
        env["PI_GATEWAY_MODEL"] = model
        # Provider config contains the short-lived turn credential. Keep it in
        # the activation tmpfs; only the session JSONL belongs in durable chat
        # state. The wrapper creates this directory before exec.
        env["PI_CODING_AGENT_DIR"] = activation_dir or "/run/agent-activation/pi"
        if session_state_dir:
            prompt_at = argv.index("-p")
            native_args = ["--session-dir", session_state_dir]
            if resume_session_id:
                native_args += ["--session", resume_session_id]
            argv[prompt_at:prompt_at] = native_args
        if self._check_effort(reasoning_effort) is not None:
            argv += ["--thinking", str(reasoning_effort)]
            # `--thinking` alone is a no-op: verified against 0.73.1, pi emits
            # no `reasoning_effort` at all unless the provider entry declares
            # the model as a reasoning model. The wrapper writes that entry, so
            # it has to be told — and only when an effort was asked for, since
            # declaring it unconditionally makes pi send `medium` on every turn
            # and Auto would stop meaning "as before".
            env["PI_GATEWAY_REASONING"] = "1"
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
        """Build pi 0.73.1's bidirectional JSONL RPC process."""
        argv, env = self.prepare(
            workdir=workdir,
            task_prompt="",
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            session_state_dir=session_state_dir,
            resume_session_id=resume_session_id,
            activation_dir=activation_dir,
        )
        mode_at = argv.index("--mode") + 1
        argv[mode_at] = "rpc"
        prompt_at = argv.index("-p")
        del argv[prompt_at : prompt_at + 2]
        return argv, env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Normalize one ``--mode json`` line (session format v3)."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._raw(line, "not json")
        if not isinstance(event, dict):
            return self._raw(line, "not an object")

        kind = event.get("type")
        if kind == "session":
            captured = str(event.get("id") or "").strip()
            if captured:
                self.session_id = captured
            return NormalizedEvent(
                LIFECYCLE,
                {
                    "phase": "init",
                    "session_id": self.session_id,
                    # pi's own version is not on the wire; this is the session
                    # format's, and naming it as such keeps it from being read
                    # as the CLI version the way Claude Code's line supplies.
                    "session_format": event.get("version"),
                },
            )
        if kind == "message_update":
            return self._from_message_update(event, line)
        if kind == "message_end":
            return self._from_message_end(event, line)
        if kind == "message_start":
            self._message_no += 1
            return None
        if kind == "agent_end":
            self._preview.clear()
            failure = self.agent_error(event)
            if failure is not None:
                return NormalizedEvent(ERROR, {"text": failure})
            return NormalizedEvent(LIFECYCLE, {"phase": "result"})
        if kind in self._SILENT_KINDS:
            return None
        return self._raw(line, f"unknown type {kind!r}")

    @staticmethod
    def agent_error(event: dict[str, Any]) -> str | None:
        """Read the final assistant outcome from Pi's native agent-end event."""
        messages = event.get("messages")
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                if message.get("stopReason") in {"error", "aborted"}:
                    return _truncate(message.get("errorMessage") or "Pi agent turn failed")
                return None
        return None

    def _from_message_update(self, event: dict[str, Any], line: str) -> NormalizedEvent | None:
        """Map a completed content block; drop the deltas that built it."""
        update = event.get("assistantMessageEvent")
        if not isinstance(update, dict):
            return self._raw(line, "message_update without an assistantMessageEvent")
        subtype = str(update.get("type") or "")
        if subtype == "text_delta":
            piece = self._preview.add(self._preview_id(update), str(update.get("delta") or ""))
            return NormalizedEvent(RAW, piece) if piece else None
        if subtype.endswith(("_start", "_delta")):
            return None
        if subtype not in self._BLOCK_END_KINDS:
            # Same treatment as an unclassified Claude Code subtype: keep the
            # first as a diagnostic so a new block kind is discoverable, drop
            # the repeats so it cannot flood the log at one row per token.
            if subtype in self._reported_subtypes:
                return None
            self._reported_subtypes.add(subtype)
            return self._raw(line, f"unclassified message_update subtype {subtype!r}")

        response = self._response_identity(event)
        if subtype == "thinking_end":
            return NormalizedEvent(THINKING, {"text": _truncate(update.get("content")), **response})
        if subtype == "text_end":
            # The one event the owner is actually waiting for: `message` is
            # what the control plane copies into agent_thread_messages, which
            # is both the visible reply and the next turn's context.
            preview_id = self._preview_id(update)
            self._preview.finish(preview_id)
            return NormalizedEvent(
                MESSAGE,
                {
                    "text": _truncate(update.get("content")),
                    "truncated": _clipped(update.get("content")),
                    "preview_id": preview_id,
                    **response,
                },
            )
        tool_call = update.get("toolCall")
        if not isinstance(tool_call, dict):
            return self._raw(line, "toolcall_end without a toolCall")
        return NormalizedEvent(
            TOOL_USE,
            {
                "name": tool_call.get("name"),
                "id": tool_call.get("id"),
                # Keyed `input` like every other adapter, because that is what
                # the UI reads to render the command.
                "input": tool_call.get("arguments"),
                **response,
            },
        )

    @staticmethod
    def _response_identity(event: dict[str, Any]) -> dict[str, str]:
        """Name the provider response a message belongs to, when pi recorded it.

        pi copies the first stream chunk's id into ``responseId`` and repeats
        the message on every ``message_update`` and ``message_end``. Against
        the gateway that is the chat-completion id it logs for the request, so
        ``usage_id`` joins a message to its measured request, as Claude Code's
        native ``message.id`` does.
        """
        message = event.get("message")
        response_id = message.get("responseId") if isinstance(message, dict) else None
        return {"usage_id": response_id} if isinstance(response_id, str) and response_id else {}

    def _preview_id(self, update: dict[str, Any]) -> str:
        return f"message-{self._message_no}:{update.get('contentIndex')}"

    def _from_message_end(self, event: dict[str, Any], line: str) -> NormalizedEvent | None:
        """Map the end of a message: a tool result, or a turn's token usage."""
        message = event.get("message")
        if not isinstance(message, dict):
            return self._raw(line, "message_end without a message")
        role = message.get("role")
        if role == "toolResult":
            return NormalizedEvent(
                TOOL_RESULT,
                {
                    "tool_use_id": message.get("toolCallId"),
                    "name": message.get("toolName"),
                    # The runner reads this to count failed calls, so a Tier 2
                    # pi job had none. `isError` appears here and nowhere else in the
                    # stream, which is why this is the authoritative copy.
                    "is_error": bool(message.get("isError")),
                    "content": _truncate(message.get("content")),
                },
            )
        if role == "assistant":
            usage = message.get("usage")
            if not isinstance(usage, dict):
                return None
            return NormalizedEvent(
                USAGE,
                {
                    "input_tokens": usage.get("input"),
                    "output_tokens": usage.get("output"),
                    "cache_read_tokens": usage.get("cacheRead"),
                    "cache_write_tokens": usage.get("cacheWrite"),
                    "total_tokens": usage.get("totalTokens"),
                    "model": message.get("model"),
                    "text": (f"{usage.get('input', 0)} in / {usage.get('output', 0)} out tokens"),
                    **self._response_identity(event),
                },
            )
        # role="user" is the prompt echoed back; the owner wrote it, and the
        # thread already has it.
        if role == "user":
            return None
        return self._raw(line, f"message_end with unknown role {role!r}")

    #: pi's own flag vocabulary, minus the two that do not survive the trip.
    #: Verified against 0.73.1 against a recording endpoint: `off` sends no
    #: parameter at all (which is Auto by another name, and Auto is already
    #: spelled by not choosing), and **`xhigh` arrives as `high`** — offering
    #: it would let someone pick a depth this runtime cannot actually ask for,
    #: which is the whole failure this control exists to end.
    EFFORT_LEVELS = PI.reasoning_efforts

    def effort_args(self, effort: str | None) -> list[str]:
        """Rendered in :meth:`prepare`, because the flag is only half of it.

        pi needs the provider entry to declare the model as a reasoning model
        as well, and that is the wrapper's file rather than this argv — so both
        halves are set together where the environment is also being built.
        """
        self._check_effort(effort)
        return []

    def _accept_mcp(
        self, config: RuntimeMCPConfig, *, gateway_base_url: str, env: dict[str, str]
    ) -> None:
        """Publish the relay endpoints for the image's own bridge to read.

        Both halves hold, and both were checked against the pinned binary
        rather than assumed: `--no-extensions` leaves the explicit `-e` as the
        only loadable extension, so the bridge is the only MCP source, and the
        flag is passed whether or not the run has servers -- the bridge returns
        immediately on an empty map, which keeps the argv identical between a
        run with tools and one without, so an injected extension has nowhere to
        hide behind a turn that did not need one.
        """
        self._wrapper_mcp_env(config, gateway_base_url=gateway_base_url, env=env)

    def capabilities(self) -> RuntimeCapabilities:
        """Tier 1 events and no trusted cost attribution.

        pi reports a per-turn ``cost`` block, but it is computed from a price
        table the gateway does not populate — every field came back zero in
        the recorded runs. ``api_logs`` is the billing authority regardless,
        so this claims no cost report rather than surfacing a confident zero.
        Native resume is declared by :class:`drivers.PiDriver`, not by this
        event/cost capability row.
        """
        return RuntimeCapabilities(
            cost_report=False,
            normalized_events=True,
            tier=1,
            mcp=PI.mcp,
            reasoning_efforts=self.EFFORT_LEVELS,
        )
