"""Codex's live session: the pinned app-server's JSON-RPC over one resident process."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent_host.drivers import (
    STEER_REQUEST_PREFIX,
    DriverError,
    InputOutcome,
    JsonResidentSession,
    JsonResidentTurn,
    LaunchContext,
    LiveDriver,
    PendingInput,
    TurnInput,
    question_choices,
)
from cloud_agent_host.harness_event import LINE, HarnessEvent
from cloud_agent_host.runtimes import (
    DIFF,
    ERROR,
    LIFECYCLE,
    MESSAGE,
    RAW,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    USAGE,
    CodexRuntime,
)
from cloud_agent_host.runtimes.codex.app_server import (
    CodexTurnUsage,
    codex_error_text,
    codex_tool_output,
)
from cloud_agent_host.stream_deltas import StreamDelta
from cloud_agent_shared.shared_protocol import (
    INPUT_PROCESS_GONE,
    INPUT_REFUSED,
    INPUT_TURN_ENDED,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


#: How long a steer waits for the CLI to say it took the message. The reply is
#: read by the turn's own stream, which never pauses while a turn is live.
STEER_REPLY_TIMEOUT_S = 10.0


def _codex_raw_row(
    value: dict[str, Any], line: str, owner: Mapping[str, str] | None = None
) -> HarnessEvent:
    """A Codex app-server notification stored as it came, for the trajectory.

    ``owner`` marks a subagent thread's notification as the subagent's.
    """
    return HarnessEvent(
        RAW,
        {
            "text": line[:4000],
            "protocol": "codex-app-server",
            "notification": value,
            **(owner or {}),
        },
        raw_line=line,
    )


class CodexAppServerSession(JsonResidentSession[CodexRuntime]):
    """Codex 0.145.0 app-server, one persisted thread per live process."""

    _PROVIDER = "hybridinference"

    def __init__(
        self,
        driver: LiveDriver[CodexRuntime],
        context: LaunchContext,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        super().__init__(driver, context, resume_session_id=resume_session_id)
        # Each subagent thread's spawning call, as the spawn items name it.
        # Kept for the process, whose threads outlive any one turn.
        self._agent_calls: dict[str, str] = {}
        self._usage = CodexTurnUsage()

    def _thread_owner(self, params: dict[str, Any]) -> dict[str, str]:
        """Mark a notification from another thread as that subagent's.

        The pinned app-server sends every thread's notifications to this
        client, its subagents' included. Those belong to the subagent.
        """
        thread = params.get("thread")
        thread_id = params.get("threadId") or (
            thread.get("id") if isinstance(thread, dict) else None
        )
        owner: dict[str, str] = {}
        if isinstance(thread_id, str) and thread_id and thread_id != self.runtime.session_id:
            owner["agent_thread_id"] = thread_id
            if thread_id in self._agent_calls:
                owner["parent_tool_use_id"] = self._agent_calls[thread_id]
        return owner

    def _initialize_protocol(self) -> None:
        initialize_id = self._request_id("initialize")
        self._write_json(
            {
                "id": initialize_id,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "hybridinference-cloud-agent",
                        "title": "Cloud Agent",
                        "version": "1",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
            }
        )
        initialized = self._read_until(lambda value: value.get("id") == initialize_id)
        if initialized.get("error") is not None:
            raise DriverError(f"Codex initialize failed: {initialized['error']}")
        self._write_json({"method": "initialized"})

        method = "thread/resume" if self._resume_session_id else "thread/start"
        request_id = self._request_id("thread")
        params: dict[str, Any] = {
            "model": self._model,
            "modelProvider": self._PROVIDER,
            "cwd": self._context.workdir,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access"
            if self._context.provides_isolation
            else "workspace-write",
        }
        if self._resume_session_id:
            params["threadId"] = self._resume_session_id
        self._write_json({"id": request_id, "method": method, "params": params})
        response = self._read_until(lambda value: value.get("id") == request_id)
        if response.get("error") is not None:
            raise DriverError(f"Codex {method} failed: {response['error']}")
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        session_id = thread.get("id") if isinstance(thread, dict) else None
        if not session_id:
            raise DriverError(f"Codex {method} returned no thread id")
        if self._resume_session_id and session_id != self._resume_session_id:
            raise DriverError("Codex resumed a different native thread")
        self.runtime.session_id = str(session_id)

        if self._context.mcp_config.server_ids:
            status_id = self._request_id("mcp-status")
            self._write_json(
                {
                    "id": status_id,
                    "method": "mcpServerStatus/list",
                    "params": {"threadId": str(session_id), "detail": "full"},
                }
            )
            status = self._read_until(lambda value: value.get("id") == status_id)
            result = status.get("result")
            inventory = result.get("data", []) if isinstance(result, dict) else []
            session = next((entry for entry in inventory if entry.get("name") == "session"), None)
            if status.get("error") or not session or not session.get("serverInfo"):
                raise DriverError("Codex could not initialize the session MCP relay")

    def _start_protocol_turn(
        self, turn_input: TurnInput, turn: JsonResidentTurn[CodexRuntime]
    ) -> None:
        request_id = self._request_id("turn")
        self._write_json(
            {
                "id": request_id,
                "method": "turn/start",
                "params": {
                    "threadId": self.runtime.session_id,
                    "input": [{"type": "text", "text": turn_input.prompt}],
                    "model": turn_input.model,
                    "effort": turn_input.reasoning_effort,
                },
            }
        )
        response = self._read_until(lambda value: value.get("id") == request_id)
        if response.get("error") is not None:
            raise DriverError(f"Codex turn/start failed: {response['error']}")
        result = response.get("result")
        protocol_turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = protocol_turn.get("id") if isinstance(protocol_turn, dict) else None
        if not turn_id:
            raise DriverError("Codex turn/start returned no turn id")
        turn.protocol_turn_id = str(turn_id)

    def line_settles_turn(
        self, line: str, turn: JsonResidentTurn[CodexRuntime]
    ) -> tuple[bool, bool, bool]:
        """Recognize the completion notification for this exact Codex turn."""
        value = self._object(line)
        if value is None or value.get("method") != "turn/completed":
            return False, False, False
        params = value.get("params")
        completed = params.get("turn") if isinstance(params, dict) else None
        if not isinstance(completed, dict) or completed.get("id") != turn.protocol_turn_id:
            return False, False, False
        status = str(completed.get("status") or "").lower()
        return True, status not in {"completed", "interrupted"}, True

    #: Notifications Codex sends per token (reasoning) or per output chunk
    #: (commands). Each stays a stored ``raw`` notification -- the trajectory
    #: view rebuilds reasoning and live command output from them -- merged with
    #: the deltas beside it rather than stored one row per token.
    _STREAM_DELTAS = frozenset(
        {
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
            "item/commandExecution/outputDelta",
        }
    )

    def stream_delta(self, line: str, turn: JsonResidentTurn[CodexRuntime]) -> StreamDelta | None:
        """Recognize a reasoning or command-output delta notification."""
        if "Delta" not in line:
            return None
        value = self._object(line)
        if value is None or value.get("method") not in self._STREAM_DELTAS:
            return None
        params = value.get("params")
        if (
            "id" in value
            or value.get("error") is not None
            or not isinstance(params, dict)
            or not params.get("itemId")
            or not isinstance(params.get("delta"), str)
        ):
            return None
        # A subagent's reasoning and output are merged as its own, never the
        # conversation's. The thread is part of what is merged, so one merged
        # row never spans two threads.
        owner = self._thread_owner(params)
        return StreamDelta(
            value,
            ("params", "delta"),
            params["delta"],
            line,
            lambda merged, merged_line: _codex_raw_row(merged, merged_line, owner),
        )

    def event_for_protocol_line(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
        self, line: str, turn: JsonResidentTurn[CodexRuntime]
    ) -> HarnessEvent:
        """Normalize one Codex app-server notification for the active turn."""
        value = self._object(line)
        if value is None:
            return HarnessEvent(LINE, raw_line=line)
        if value.get("error") is not None:
            # A JSON-RPC error answer: {code, message}. Its message, not the
            # Python spelling of the whole object.
            error = value["error"]
            message = error.get("message") if isinstance(error, dict) else None
            return HarnessEvent(
                ERROR, {"text": str(message or error)[:2000], "error": error}, raw_line=line
            )
        method = str(value.get("method") or "")
        params = value.get("params")
        params = params if isinstance(params, dict) else {}
        item = params.get("item")
        item = item if isinstance(item, dict) else {}
        item_type = str(item.get("type") or "")
        # Keep the app-server item identity separate from provider message/usage
        # identity. Trajectory can join deltas to this item without inventing a
        # provider request or attributing turn-wide usage to one reply.
        identity = {"native_item_id": str(item["id"])} if item.get("id") else {}
        if (
            item_type == "collabAgentToolCall"
            and item.get("tool") == "spawnAgent"
            and item.get("id")
        ):
            for receiver in item.get("receiverThreadIds") or []:
                if isinstance(receiver, str) and receiver:
                    self._agent_calls.setdefault(receiver, str(item["id"]))
                    self._usage.spawned(receiver, turn.protocol_turn_id)
        owner = self._thread_owner(params)
        raw_notification = _codex_raw_row(value, line, owner)
        if method == "thread/tokenUsage/updated":
            usage = self._usage.counted(params, turn.protocol_turn_id, owner)
            return HarnessEvent(USAGE, usage, raw_line=line) if usage else raw_notification
        if method == "error" and params.get("willRetry") is not True and not owner:
            # The error that fails the turn (0.145.0 sends it before the failed
            # turn/completed); a retried one says ``willRetry`` and stays raw.
            error = params.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            if message:
                return HarnessEvent(
                    ERROR,
                    {"text": codex_error_text(str(message)), "error": error},
                    raw_line=line,
                )
        if method == "item/tool/requestUserInput":
            native_id = value.get("id")
            questions = question_choices(params.get("questions"), dialect="codex")
            if (
                isinstance(native_id, (str, int))
                and not isinstance(native_id, bool)
                and params.get("threadId") == self.runtime.session_id
                and params.get("turnId") == turn.protocol_turn_id
                and questions
            ):

                def respond(answers: dict[str, list[str]], reject: bool) -> None:
                    # Codex's pinned protocol has no reject discriminator:
                    # an empty answers map is its skipped-question response.
                    self._write_json(
                        {
                            "id": native_id,
                            "result": {
                                "answers": {}
                                if reject
                                else {key: {"answers": values} for key, values in answers.items()}
                            },
                        }
                    )

                turn.remember_question(json.dumps(native_id), questions, respond)
            elif isinstance(native_id, (str, int)) and not isinstance(native_id, bool):
                self._write_json(
                    {
                        "id": native_id,
                        "error": {
                            "code": -32602,
                            "message": "Invalid or stale user question request.",
                        },
                    }
                )
            return raw_notification
        if method == "serverRequest/resolved" and params.get("threadId") == self.runtime.session_id:
            native_id = params.get("requestId")
            if isinstance(native_id, (str, int)) and not isinstance(native_id, bool):
                turn.forget_question(json.dumps(native_id))
            return raw_notification
        if method == "item/agentMessage/delta" and params.get("itemId"):
            if owner:
                # A subagent's text is not the conversation's answer.
                return HarnessEvent(LINE, raw_line=line)
            # Batched rather than one stored notification per token.
            piece = turn.preview.add(str(params["itemId"]), str(params.get("delta") or ""))
            return (
                HarnessEvent(RAW, piece, raw_line=line)
                if piece
                else HarnessEvent(LINE, raw_line=line)
            )
        if method == "item/completed" and item_type == "agentMessage":
            preview = {"preview_id": str(item["id"])} if item.get("id") and not owner else {}
            if preview:
                turn.preview.finish(preview["preview_id"])
            return HarnessEvent(
                MESSAGE,
                {"text": str(item.get("text") or ""), **identity, **preview, **owner},
                raw_line=line,
            )
        if method == "item/completed" and item_type == "reasoning":
            text = "\n".join(str(value) for value in item.get("summary") or [])
            return HarnessEvent(THINKING, {"text": text[:4000], **identity, **owner}, raw_line=line)
        if method in {"item/started", "item/updated"} and item_type in {
            "commandExecution",
            "mcpToolCall",
        }:
            item_id = str(item.get("id") or "")
            if (item_id and item_id in turn.reported_tools) or (
                not item_id and method == "item/updated"
            ):
                return raw_notification
            if item_id:
                turn.reported_tools.add(item_id)
            # An MCP call keeps the tool's own name, which a server may have
            # called "task" or "list_agents"; the server it went to is what
            # says it is not Codex's own delegation.
            server = item.get("server") if item_type == "mcpToolCall" else None
            return HarnessEvent(
                TOOL_USE,
                {
                    "name": item.get("tool") or item_type,
                    "id": item_id or None,
                    "input": item.get("command") or item.get("arguments"),
                    **({"mcp_server": server} if isinstance(server, str) and server else {}),
                    **owner,
                },
                raw_line=line,
            )
        if method == "item/completed" and item_type in {"commandExecution", "mcpToolCall"}:
            exit_code = item.get("exitCode")
            return HarnessEvent(
                TOOL_RESULT,
                {
                    "tool_use_id": item.get("id"),
                    "is_error": (isinstance(exit_code, int) and exit_code != 0)
                    or bool(item.get("error")),
                    "content": codex_tool_output(item)[:4000],
                    "exit_code": exit_code,
                    **owner,
                },
                raw_line=line,
            )
        if method in {"turn/diff/updated", "turn/diffUpdated"}:
            return HarnessEvent(
                DIFF, {"text": str(params.get("diff") or "")[:4000], **owner}, raw_line=line
            )
        if method == "turn/completed":
            # A subagent's turn ending is not this turn's result, so it stays a
            # marked lifecycle record; its usage came in tokenUsage notifications.
            completed = params.get("turn")
            return HarnessEvent(
                LIFECYCLE,
                {
                    "phase": "result",
                    "status": completed.get("status") if isinstance(completed, dict) else None,
                    **owner,
                },
                raw_line=line,
            )
        return raw_notification

    def inject_input(
        self, turn: JsonResidentTurn[CodexRuntime], pending: PendingInput
    ) -> InputOutcome:
        """Steer the running Codex turn: its next model request carries the text.

        ``turn/steer`` names the turn it expects, so a message can never land in
        a different turn than the one the owner saw running. Codex puts it in
        the same turn's next model request (verified against the pinned
        0.145.0); the turn's stream reports it taken where the acceptance
        arrives. An answer that is slow to come is still reported by the
        stream if it accepts, so waiting too long is not a refusal.
        """
        if not turn.protocol_turn_id:
            return InputOutcome(False, INPUT_TURN_ENDED)
        request_id = self._request_id(STEER_REQUEST_PREFIX)
        reply = turn.expect_steer(request_id, pending)
        try:
            self._write_json(
                {
                    "id": request_id,
                    "method": "turn/steer",
                    "params": {
                        "threadId": self.runtime.session_id,
                        "expectedTurnId": turn.protocol_turn_id,
                        "input": [{"type": "text", "text": pending.text}],
                    },
                }
            )
        except DriverError:
            turn.forget_reply(request_id)
            return InputOutcome(False, INPUT_PROCESS_GONE)
        answer = reply.wait(STEER_REPLY_TIMEOUT_S)
        if answer is None:
            turn.forget_reply(request_id)
            return InputOutcome(True)
        error = answer.get("error")
        if error is None:
            return InputOutcome(True)
        message = str(error.get("message") if isinstance(error, dict) else error)
        # Codex refuses with "no active turn to steer" once the turn is over and
        # "expected active turn id `X` but found `Y`" once another has begun:
        # either way the turn this message was for has ended.
        if "no active turn" in message or "expected active turn" in message:
            return InputOutcome(False, INPUT_TURN_ENDED)
        return InputOutcome(False, INPUT_REFUSED)

    def interrupt_turn(self, turn: JsonResidentTurn[CodexRuntime]) -> None:
        """Interrupt only the active Codex turn on the resident thread."""
        if not turn.protocol_turn_id:
            raise DriverError("Codex turn id is not available for interrupt")
        self._write_json(
            {
                "id": self._request_id("interrupt"),
                "method": "turn/interrupt",
                "params": {
                    "threadId": self.runtime.session_id,
                    "turnId": turn.protocol_turn_id,
                },
            }
        )

    def fork_native(
        self,
        *,
        source_session_id: str,
        operation_id: str,
        source_native_turn_no: int,
    ) -> str:
        """Fork Codex through the requested settled native turn, inclusive."""
        if source_session_id != self.runtime.session_id:
            raise DriverError("Codex fork source no longer matches the live thread")
        if source_native_turn_no < 1:
            raise DriverError("Codex fork turn must be positive")

        read_id = self._request_id(f"fork-read-{operation_id}")
        self._write_json(
            {
                "id": read_id,
                "method": "thread/read",
                "params": {"threadId": source_session_id, "includeTurns": True},
            }
        )
        read_response = self._read_until(lambda value: value.get("id") == read_id)
        if read_response.get("error") is not None:
            raise DriverError(f"Codex thread/read failed: {read_response['error']}")
        read_result = read_response.get("result")
        source_thread = read_result.get("thread") if isinstance(read_result, dict) else None
        turns = source_thread.get("turns") if isinstance(source_thread, dict) else None
        if not isinstance(turns, list) or source_native_turn_no > len(turns):
            raise DriverError(
                "Codex native history does not contain the requested fork turn "
                f"({source_native_turn_no} of {len(turns) if isinstance(turns, list) else 0})"
            )
        anchor = turns[source_native_turn_no - 1]
        anchor_id = anchor.get("id") if isinstance(anchor, dict) else None
        if not isinstance(anchor_id, str) or not anchor_id:
            raise DriverError("Codex fork turn has no native id")
        request_id = self._request_id(f"fork-{operation_id}")
        self._write_json(
            {
                "id": request_id,
                "method": "thread/fork",
                "params": {
                    "threadId": source_session_id,
                    "model": self._model,
                    "modelProvider": self._PROVIDER,
                    "cwd": self._context.workdir,
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access"
                    if self._context.provides_isolation
                    else "workspace-write",
                    # Pinned app-server 0.145.0 defines this as inclusive. The
                    # platform resolves its stable native-turn ordinal against
                    # the source thread at mutation time instead of persisting
                    # provider ids that a later fork may rewrite.
                    "lastTurnId": anchor_id,
                    # We need only the child locator.  Asking Codex to rebuild
                    # and serialize every historical turn after the native
                    # mutation makes the response slower and adds a second
                    # failure surface after the child rollout already exists.
                    # 0.145.0 explicitly provides this fork mode for clients
                    # that will resume the returned thread separately.
                    "excludeTurns": True,
                },
            }
        )
        response = self._read_until(lambda value: value.get("id") == request_id)
        if response.get("error") is not None:
            raise DriverError(f"Codex thread/fork failed: {response['error']}")
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        child_id = thread.get("id") if isinstance(thread, dict) else None
        if not child_id or child_id == source_session_id:
            raise DriverError("Codex thread/fork returned an invalid child id")
        return str(child_id)
