"""Claude Code invocation and observed stream-json event normalization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.runtime_catalog import CLAUDE_CODE

from .base import (
    EMPTY_RUNTIME_MCP_CONFIG,
    MCP_TOKEN_ENV,
    AgentRuntime,
    RuntimeCapabilities,
    RuntimeMCPConfig,
)
from .events import (
    ERROR,
    LIFECYCLE,
    MESSAGE,
    RAW,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    NormalizedEvent,
    _harness_inventory,
    _payload_text,
)
from .preview import ReplyPreview

if TYPE_CHECKING:
    from collections.abc import Callable


class ClaudeCodeRuntime(AgentRuntime):
    """Claude Code headless (``claude -p --output-format stream-json``).

    Speaks the Anthropic Messages surface to the gateway, so ``credential``
    (the job's capability token) rides in ``ANTHROPIC_API_KEY``.
    """

    name = CLAUDE_CODE.name
    binary = "claude"

    # `type: "system"` covers two unrelated things: a few genuine milestones,
    # and progress telemetry. Only these are milestones. The distinction is not
    # cosmetic — the first real job on staging stored 412 lifecycle events, 408
    # of them `subtype: "thinking_tokens"`, so a counter became 408 rows in the
    # append-only log, 408 SSE frames, and 408 ticked-off steps in the UI.
    # An allowlist rather than a denylist: a CLI upgrade that invents another
    # counter must not be able to flood the stream just because nobody had
    # heard of it yet.
    MILESTONE_SUBTYPES = frozenset({"init", "compact_boundary"})

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        """Track which unclassified subtypes this run has already reported."""
        # One adapter instance per job (see get_runtime), so this is per-run
        # state — the suppression below cannot leak across jobs.
        self._reported_subtypes: set[str] = set()
        #: The CLI's own session id, captured from the ``init`` event. The
        #: retained container resumes this native locator directly; ``None``
        #: until the CLI has announced itself.
        self.session_id: str | None = None
        self._preview = ReplyPreview(clock)
        # The text block being written: ``<message id>:<block index>``.
        self._message_id: str | None = None
        self._text_block: str | None = None

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
        config_dir: str | None = None,
        resume_session_id: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the headless invocation and its environment.

        ``config_dir`` relocates the CLI's config/session storage inside the
        retained container's writable layer; ``resume_session_id`` continues
        an earlier native session with only the new prompt after the resident
        process was replaced. Both are Claude-specific and passed only by the
        Claude driver; the generic session never sets them.
        """
        argv = [
            self.binary,
            "-p",
            task_prompt,
        ]
        if resume_session_id:
            argv += ["--resume", resume_session_id]
        argv += [
            "--model",
            model,
            # Kept adjacent to --model deliberately: the resident session finds
            # the model slot by index and rebuilds the effort beside it, so the
            # two settings a turn can carry stay in one place in the argv.
            *self.effort_args(reasoning_effort),
            "--output-format",
            "stream-json",
            "--verbose",
            # Without it the CLI writes an answer only once the model has
            # finished it, so the owner watched nothing happen and then a whole
            # answer. With it the text also arrives as it is written, as
            # ``stream_event`` lines; ``_from_stream`` turns those into a
            # preview that the complete block then replaces.
            "--include-partial-messages",
            # A repository can commit .mcp.json and Claude otherwise starts
            # its stdio commands before the model's first turn. Reproduced
            # against the real CLI in this exact configuration: without this
            # flag the CLI reports the repository's own server alongside the
            # platform's, with no approval step, because the permission mode
            # below is precisely what removes the gate that would have caught
            # it. Unconditional, including for a job with no MCP servers —
            # which is where an injected one would be least expected.
            "--strict-mcp-config",
            # Fork siblings share the repository, so its project/local
            # settings are untrusted launch input. Only the member's private
            # user settings (including platform-installed hooks) are loaded.
            "--setting-sources",
            "user",
            # The sandbox IS the boundary, so an interactive permission prompt
            # inside it has nothing left to protect — it only guarantees the
            # agent cannot do the work. Without this the CLI denies every write
            # with "you haven't granted it yet", the job produces an empty
            # patch, and (worse) a model that ignores the error still reports
            # success. Found by the first real run, not by the fake.
            "--permission-mode",
            "bypassPermissions",
            # Paired with --strict-mcp-config above: that flag makes this the
            # *only* source of MCP servers, and this supplies the platform's.
            # Passed even when empty, so the pairing has no gap.
            "--mcp-config",
            json.dumps(
                {"mcpServers": self._mcp_endpoints(mcp_config, gateway_base_url=gateway_base_url)}
            ),
        ]
        env = {
            "ANTHROPIC_BASE_URL": gateway_base_url.rstrip("/").removesuffix("/v1"),
            "ANTHROPIC_API_KEY": credential,
            "MCP_TOOL_TIMEOUT": "1560000",
            # Remote MCP has a separate five-minute idle timer. The relay can
            # wait ten minutes for owner approval without sending progress;
            # keep that wait within the overall tool timeout so the CLI receives
            # the approval outcome instead of abandoning a still-pending call.
            "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT": "1560000",
            # Referenced by the MCP config above rather than written into it,
            # so the job token never appears in a process argument.
            # The MCP relay's token, which is *not* the inference credential
            # above. Two services, two scopes, two blast radii.
            MCP_TOKEN_ENV: mcp_config.credential,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            # A concrete env override outranks Agent's model argument and the
            # subagent definition. `inherit` skips that override, allowing
            # Explore to pick an ungranted default (e.g. opus for DeepSeek).
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            # The pinned CLI retries transient inference failures inside the
            # current native turn with exponential backoff and jitter. Extend
            # its default request budget without replaying the user's prompt;
            # disabling the watchdog keeps exhaustion bounded.
            "CLAUDE_CODE_MAX_RETRIES": "15",
            "CLAUDE_CODE_RETRY_WATCHDOG": "0",
            # Telemetry and auto-update are switched off explicitly. The
            # sandbox reaches the public web, so nothing else stops the CLI
            # phoning home or replacing itself in the middle of a job.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        if config_dir:
            # Session files land in the retained container layer instead of a
            # process-private HOME, so docker stop/start can resume them.
            env["CLAUDE_CONFIG_DIR"] = config_dir
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
        config_dir: str | None = None,
        resume_session_id: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """The resident invocation (HOT): prompts arrive on stdin, not argv.

        ``--input-format stream-json`` keeps the process alive between turns,
        reading user messages from stdin — the terminal-never-closed shape the
        design's HOT tier is. ``--replay-user-messages`` echoes each one back
        on stdout at the moment the CLI reads it into the conversation, which
        is how a message sent into a running turn is known to have landed in
        it. The common argv/env builder stays shared with runtime conformance
        tests so auth, model, and MCP flags cannot drift.
        """
        argv, env = self.prepare(
            workdir=workdir,
            task_prompt="",
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            provides_isolation=provides_isolation,
            mcp_config=mcp_config,
            reasoning_effort=reasoning_effort,
            config_dir=config_dir,
            resume_session_id=resume_session_id,
        )
        # Drop the empty positional prompt (`-p ""`), add the input format.
        prompt_at = argv.index("-p")
        del argv[prompt_at : prompt_at + 2]
        argv[1:1] = ["-p", "--input-format", "stream-json", "--replay-user-messages"]
        # AskUserQuestion and ExitPlanMode require owner interaction even in bypassPermissions.
        # The pinned CLI sends its can_use_tool request over this control
        # channel, and the live turn answers it with the owner's choices.
        argv += ["--permission-prompt-tool", "stdio"]
        return argv, env

    @staticmethod
    def user_message_line(text: str) -> str:
        """One stdin line carrying one user turn, in the CLI's wire shape."""
        return json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        )

    @staticmethod
    def turn_settled(line: str) -> bool:
        """Whether this stdout line is the CLI's end-of-turn result event."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
        return isinstance(event, dict) and event.get("type") == "result"

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Normalize one stream-json line."""
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
        if kind == "system":
            subtype = str(event.get("subtype") or "system").strip() or "system"
            if subtype == "api_retry":
                # Each notice describes a new request attempt and its wait;
                # suppressing repeated subtypes would hide the retry progress.
                return NormalizedEvent(
                    LIFECYCLE,
                    {
                        "phase": "api_retry",
                        "attempt": event.get("attempt"),
                        "max_retries": event.get("max_retries"),
                        "retry_delay_ms": event.get("retry_delay_ms"),
                        "error_status": event.get("error_status"),
                    },
                )
            if subtype not in self.MILESTONE_SUBTYPES:
                # Not discarded outright: the first occurrence is kept as a raw
                # diagnostic so a subtype nobody has classified yet is still
                # discoverable in the event log. Repeats are dropped, which is
                # what turns 408 rows into 1.
                if subtype in self._reported_subtypes:
                    return None
                self._reported_subtypes.add(subtype)
                return self._raw(line, f"unclassified system subtype {subtype!r}")
            # Only the session's own init is republished as its inventory. A
            # subagent emits the same subtype for a narrower menu, and the
            # normalized event carries no marker saying which one it was, so
            # attaching the inventory there would read as the session's.
            harness: dict[str, Any] | None = None
            if subtype == "init" and not event.get("parent_tool_use_id"):
                captured = str(event.get("session_id") or "").strip()
                if captured:
                    self.session_id = captured
                harness = _harness_inventory(event)
            payload: dict[str, Any] = {
                "phase": subtype,
                "model": event.get("model"),
                "runtime_version": event.get("claude_code_version"),
                "session_id": self.session_id,
            }
            if harness is not None:
                payload["harness"] = harness
            return NormalizedEvent(LIFECYCLE, payload)
        if kind == "stream_event":
            return self._from_stream(event)
        if kind == "assistant":
            return self._from_assistant(event)
        if kind == "user":
            return self._from_user(event)
        if kind == "rate_limit_event":
            return NormalizedEvent(
                LIFECYCLE, {"phase": "rate_limit", "info": event.get("rate_limit_info")}
            )
        if kind == "result":
            self._preview.clear()
            self._message_id = self._text_block = None
            return self._from_result(event)
        return self._raw(line, f"unknown type {kind!r}")

    def _from_stream(self, event: dict[str, Any]) -> NormalizedEvent | None:
        """Preview the answer text the model is writing.

        Only answer text is previewed. Thinking stays a single status in the
        chat, and a tool call's input is shown once it is complete; a file the
        model writes would otherwise arrive as thousands of fragments. A
        subagent's text is not the conversation's answer, so it is left out
        too. Every other line is consumed here and stored nowhere, which is
        safe because the CLI repeats the complete content in ``assistant`` lines.
        """
        if event.get("parent_tool_use_id"):
            return None
        stream = event.get("event")
        if not isinstance(stream, dict):
            return None
        kind = stream.get("type")
        index = stream.get("index")
        if kind == "message_start":
            message = stream.get("message")
            message_id = message.get("id") if isinstance(message, dict) else None
            self._message_id = str(message_id) if message_id else None
            self._text_block = None
            return None
        if kind == "content_block_start":
            block = stream.get("content_block")
            is_text = isinstance(block, dict) and block.get("type") == "text"
            self._text_block = (
                f"{self._message_id}:{index}"
                if is_text and self._message_id and isinstance(index, int)
                else None
            )
            return None
        delta = stream.get("delta")
        if (
            kind != "content_block_delta"
            or self._text_block is None
            or self._text_block != f"{self._message_id}:{index}"
            or not isinstance(delta, dict)
            or delta.get("type") != "text_delta"
        ):
            return None
        piece = self._preview.add(self._text_block, str(delta.get("text") or ""))
        return NormalizedEvent(RAW, piece) if piece else None

    def _from_assistant(self, event: dict[str, Any]) -> NormalizedEvent:
        """Map an assistant turn to its most significant block."""
        message = event.get("message") or {}
        blocks = message.get("content") or []
        usage = message.get("usage") or {}
        # The CLI emits text and parallel tools as separate blocks of one
        # response, each repeating its usage. Keep the identity for counting.
        usage_fields = {"usage": usage or None, "usage_id": message.get("id")}
        parent_tool_use_id = event.get("parent_tool_use_id")
        if isinstance(parent_tool_use_id, str) and parent_tool_use_id:
            # The enclosing Task/Agent call owns these blocks. Keep that
            # recorded relationship so the UI can distinguish a child agent's
            # activity and reply from the conversation's own answer.
            usage_fields["parent_tool_use_id"] = parent_tool_use_id
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                return NormalizedEvent(
                    TOOL_USE,
                    {
                        "name": block.get("name"),
                        "id": block.get("id"),
                        "input": block.get("input"),
                        **usage_fields,
                    },
                )
            if block.get("type") == "thinking":
                return NormalizedEvent(
                    THINKING, {"text": block.get("thinking") or "", **usage_fields}
                )
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        payload: dict[str, Any] = {"text": text, **usage_fields}
        preview_id = self._text_block
        if (
            preview_id is not None
            and not event.get("parent_tool_use_id")
            and preview_id.startswith(f"{message.get('id')}:")
        ):
            # The pinned 2.1.220 writes a block's complete line before that
            # block's stop event, so this is the block that was previewed.
            payload["preview_id"] = preview_id
            self._preview.finish(preview_id)
            self._text_block = None
        return NormalizedEvent(MESSAGE, payload)

    def _from_user(self, event: dict[str, Any]) -> NormalizedEvent | None:
        """Map a tool result echoed back into the conversation."""
        if event.get("isReplay"):
            # A user message the CLI read from stdin, echoed back: the driver
            # accounts for it, and it is nothing new to report.
            return None
        blocks = (event.get("message") or {}).get("content") or []
        parent_tool_use_id = event.get("parent_tool_use_id")
        parent_fields = (
            {"parent_tool_use_id": parent_tool_use_id}
            if isinstance(parent_tool_use_id, str) and parent_tool_use_id
            else {}
        )
        # Native tools carry their structured result outside the content block.
        # TaskCreate's assigned task ID is here rather than in its input.
        structured_output = event.get("tool_use_result")
        result_fields = (
            {"structured_output": structured_output}
            if isinstance(structured_output, (dict, list))
            else {}
        )
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                return NormalizedEvent(
                    TOOL_RESULT,
                    {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "content": _payload_text(content),
                        **parent_fields,
                        **result_fields,
                    },
                )
        return NormalizedEvent(RAW, {"text": "user turn without a tool result"})

    def _from_result(self, event: dict[str, Any]) -> NormalizedEvent:
        """Map the terminal result line."""
        # Result usage is the total for this query (one platform attempt),
        # including failed queries. It supersedes provisional block usage.
        usage_fields = {"usage": event.get("usage") or None, "usage_scope": "attempt"}
        if event.get("is_error") or event.get("terminal_reason") == "api_error":
            # An API failure arrives as subtype "success" with the reason in
            # ``result``; the error subtypes carry it in ``errors`` instead
            # (both verified against the pinned 2.1.220).
            errors = event.get("errors")
            reasons = [str(item) for item in errors if item] if isinstance(errors, list) else []
            text = (
                str(event.get("result") or "")
                or "; ".join(reasons)
                or str(event.get("subtype") or "")
            )
            return NormalizedEvent(
                ERROR,
                {
                    "subtype": event.get("subtype"),
                    "terminal_reason": event.get("terminal_reason"),
                    "api_error_status": event.get("api_error_status"),
                    "text": text[:2000],
                    **usage_fields,
                },
            )
        return NormalizedEvent(
            LIFECYCLE,
            {
                "phase": "result",
                "text": str(event.get("result") or ""),
                **usage_fields,
                "num_turns": event.get("num_turns"),
                # Reported for observability only. The billing authority is
                # api_logs.agent_job_id — the platform never trusts a number
                # the agent computed about itself.
                "reported_cost_usd": event.get("total_cost_usd"),
            },
        )

    #: Captured from the pinned 2.1.220, **not** from ``--help``: the 2.1.90
    #: help string reads "(low, medium, high, max)" and this list was first
    #: written from it, missing ``xhigh`` — which the CLI does put on the wire.
    #: Reading the documentation instead of the request is how a level the
    #: product supports went unoffered.
    #:
    #: Exactness matters more here than for the config-file runtimes, because
    #: **an unrecognised level is not refused, it silently becomes ``high``**
    #: (``--effort ultra``, ``none`` and ``minimal`` all arrive as ``high``).
    #: A level declared here that the CLI does not know would therefore run at
    #: a depth nobody chose while reporting the one they did.
    EFFORT_LEVELS = CLAUDE_CODE.reasoning_efforts

    def effort_args(self, effort: str | None) -> list[str]:
        """Express effort as ``--effort <level>``."""
        if self._check_effort(effort) is None:
            return []
        return ["--effort", str(effort)]

    def capabilities(self) -> RuntimeCapabilities:
        """Claude Code is Tier 1: normalized events, a cost report, and MCP."""
        return RuntimeCapabilities(
            cost_report=True,
            normalized_events=True,
            tier=1,
            mcp=CLAUDE_CODE.mcp,
            reasoning_efforts=self.EFFORT_LEVELS,
        )
