"""The OpenCode/Kilo live session, read through the private loopback bridge."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.drivers import (
    DriverError,
    JsonResidentSession,
    JsonResidentTurn,
    ResidentLiveRuntime,
    TurnInput,
    question_choices,
)
from cloud_agent_host.harness_event import LINE, HarnessEvent
from cloud_agent_host.runtimes import (
    ERROR,
    LIFECYCLE,
    MESSAGE,
    RAW,
    THINKING,
    TOOL_RESULT,
)
from cloud_agent_host.runtimes.context_snapshot import bridge_report
from cloud_agent_host.runtimes.http_bridge.parts import (
    ContextRecords,
    answer_preview,
    tool_call_event,
)
from cloud_agent_host.stream_deltas import StreamDelta

if TYPE_CHECKING:
    from cloud_agent_host.container.sandbox import SandboxProcess


def _http_agent_error_text(error: Any) -> str:
    """Read an OpenCode/Kilo session error the way their own CLI prints it.

    The error is ``{name, data: {message, ...}}`` (OpenCode 1.18.9, and Kilo's
    fork of it): the message when there is one, otherwise the error's name.
    """
    if isinstance(error, dict):
        data = error.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        return str(message or error.get("name") or "session failed")
    return str(error or "session failed")


def _bridge_raw_row(value: dict[str, Any], line: str) -> HarnessEvent:
    """An OpenCode/Kilo bridge record stored as its bounded line."""
    return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)


class HttpAgentLiveTurn(JsonResidentTurn[ResidentLiveRuntime]):
    """One OpenCode/Kilo activation, deduplicating mutable part updates."""

    def __init__(self, session: HttpAgentLiveSession, process: SandboxProcess) -> None:
        super().__init__(session, process)
        self.text_parts: dict[str, tuple[str, str]] = {}
        self.message_roles: dict[str, str] = {}
        #: Each part's kind, from its first update: a ``message.part.delta``
        #: does not say whether it extends answer text or reasoning.
        self.part_types: dict[str, str] = {}
        #: The call of a tool part that ended without running, reported just
        #: ahead of the result the same update carries.
        self.unopened_call: HarnessEvent | None = None
        #: What the server injected or pruned this turn (the Context events).
        self.context = ContextRecords()


class HttpAgentLiveSession(JsonResidentSession[ResidentLiveRuntime]):
    """OpenCode/Kilo serve hidden behind the private loopback bridge."""

    def _initialize_protocol(self) -> None:
        ready = self._read_until(lambda value: value.get("bridge") in {"ready", "fatal"})
        if ready.get("bridge") != "ready":
            raise DriverError(f"{self.runtime.name} live bridge failed: {ready.get('detail')}")
        session_id = str(ready.get("session_id") or "")
        if not session_id:
            raise DriverError(f"{self.runtime.name} bridge returned no session id")
        if self._resume_session_id and session_id != self._resume_session_id:
            raise DriverError(f"{self.runtime.name} resumed a different native session")
        self.runtime.session_id = session_id

    def _new_turn(self) -> HttpAgentLiveTurn:
        assert self._process is not None
        return HttpAgentLiveTurn(self, self._process)

    def _start_protocol_turn(
        self, turn_input: TurnInput, turn: JsonResidentTurn[ResidentLiveRuntime]
    ) -> None:
        request_id = self._request_id("turn")
        turn.protocol_turn_id = request_id
        self._write_json({"id": request_id, "type": "turn", "prompt": turn_input.prompt})

    def line_settles_turn(
        self, line: str, turn: JsonResidentTurn[ResidentLiveRuntime]
    ) -> tuple[bool, bool, bool]:
        """Recognize the bridge settlement or command failure for this turn."""
        value = self._object(line)
        if value is None:
            return False, False, False
        bridge = value.get("bridge")
        if bridge == "settled" and value.get("turn_id") == turn.protocol_turn_id:
            return True, value.get("status") != "completed", True
        if bridge in {"fatal", "command_error"}:
            command_id = value.get("id")
            if bridge == "fatal" or command_id in {"", turn.protocol_turn_id}:
                return True, True, False
        return False, False, False

    def stream_delta(
        self, line: str, turn: JsonResidentTurn[ResidentLiveRuntime]
    ) -> StreamDelta | None:
        """Recognize this turn's per-token delta of a part other than answer text.

        Reasoning arrives one ``message.part.delta`` per token, like the answer;
        the answer's own deltas only feed its preview and are not stored.
        """
        assert isinstance(turn, HttpAgentLiveTurn)
        if "message.part.delta" not in line:
            return None
        value = self._object(line)
        if (
            value is None
            or value.get("bridge") != "event"
            or value.get("turn_id") != turn.protocol_turn_id
        ):
            return None
        event = value.get("event")
        properties = event.get("properties") if isinstance(event, dict) else None
        if (
            not isinstance(event, dict)
            or event.get("type") != "message.part.delta"
            or not isinstance(properties, dict)
            or not isinstance(properties.get("delta"), str)
        ):
            return None
        if (
            properties.get("field") == "text"
            and turn.part_types.get(str(properties.get("partID") or "")) == "text"
        ):
            return None
        return StreamDelta(
            value, ("event", "properties", "delta"), properties["delta"], line, _bridge_raw_row
        )

    def event_for_protocol_line(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
        self, line: str, turn: JsonResidentTurn[ResidentLiveRuntime]
    ) -> HarnessEvent:
        """Normalize one OpenCode/Kilo bridge event for the active turn."""
        assert isinstance(turn, HttpAgentLiveTurn)
        value = self._object(line)
        if value is None:
            return HarnessEvent(LINE, raw_line=line)
        bridge = value.get("bridge")
        turn.settle_question_reply(value)
        if bridge in {"event", "settled"} and value.get("turn_id") != turn.protocol_turn_id:
            return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
        if bridge == "event":
            event = value.get("event")
            event = event if isinstance(event, dict) else {}
            event_type = event.get("type")
            properties = event.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            if event_type == "question.asked":
                native_id = properties.get("id")
                questions = question_choices(properties.get("questions"), dialect="http")
                if (
                    isinstance(native_id, str)
                    and native_id
                    and properties.get("sessionID") == self.runtime.session_id
                    and questions
                ):
                    question_request_id: str = native_id

                    def respond(answers: dict[str, list[str]], reject: bool) -> None:
                        command_id = self._request_id("question")
                        turn.track_question_reply(command_id, question_request_id)
                        self._write_json(
                            {
                                "id": command_id,
                                "type": "question",
                                "turn_id": turn.protocol_turn_id,
                                "request_id": question_request_id,
                                "reject": reject,
                                "answers": []
                                if reject
                                else [answers[question["id"]] for question in questions],
                            }
                        )

                    turn.remember_question(question_request_id, questions, respond)
                elif (
                    isinstance(native_id, str)
                    and native_id
                    and properties.get("sessionID") == self.runtime.session_id
                ):
                    # A malformed native question must not leave a tool
                    # blocked behind an interaction the UI cannot present.
                    self._write_json(
                        {
                            "id": self._request_id("question"),
                            "type": "question",
                            "turn_id": turn.protocol_turn_id,
                            "request_id": native_id,
                            "reject": True,
                            "answers": [],
                        }
                    )
            elif event_type in {"question.replied", "question.rejected"}:
                native_id = properties.get("requestID")
                if isinstance(native_id, str):
                    turn.forget_question(native_id)
            part = properties.get("part")
            part = part if isinstance(part, dict) else {}
            part_type = part.get("type")
            part_id = str(part.get("id") or "")
            if event_type == "message.updated":
                info = properties.get("info")
                if isinstance(info, dict) and info.get("id"):
                    turn.message_roles[str(info["id"])] = str(info.get("role") or "")
            if event_type == "message.part.updated" and part_id and part_type:
                turn.part_types[part_id] = str(part_type)
            if event_type == "message.part.updated" and part_type == "text":
                if not part_id:
                    return HarnessEvent(LINE, raw_line=line)
                # User prompts also emit text parts. Resolve ownership at
                # settlement so even a part preceding its message metadata
                # cannot turn the user's input into an assistant reply.
                message_id = str(part.get("messageID") or "")
                turn.text_parts[part_id] = (message_id, str(part.get("text") or ""))
                if (injected := turn.context.text_part(part, part_id, turn)) is not None:
                    return injected
                timing = part.get("time")
                ended = isinstance(timing, dict) and bool(timing.get("end"))
                return answer_preview(turn, part_id, ended=ended, line=line)
            if (
                event_type == "message.part.delta"
                and properties.get("field") == "text"
                and turn.part_types.get(str(properties.get("partID") or "")) == "text"
            ):
                # The pinned 1.18.9 (and Kilo) send a text part as an empty
                # update, then one delta per token, then the complete update.
                delta_part = str(properties["partID"])
                owner, text = turn.text_parts.get(
                    delta_part, (str(properties.get("messageID") or ""), "")
                )
                turn.text_parts[delta_part] = (owner, text + str(properties.get("delta") or ""))
                return answer_preview(turn, delta_part, ended=False, line=line)
            if event_type == "message.part.updated" and part_type == "reasoning":
                text = str(part.get("text") or "")
                if part.get("time", {}).get("end") if isinstance(part.get("time"), dict) else False:
                    return HarnessEvent(THINKING, {"text": text[:4000]}, raw_line=line)
                return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
            if event_type == "message.part.updated" and part_type == "tool":
                state = part.get("state")
                state = state if isinstance(state, dict) else {}
                call_id = str(part.get("callID") or part_id)
                status = state.get("status")
                pruned = turn.context.tool_part(self.runtime.name, part, call_id, line)
                if pruned is not None:
                    return pruned
                # Both servers open a tool part as pending, with an empty input,
                # while the model is still writing the call's arguments. The
                # call is reported from the update that starts it running,
                # which is the first to carry them.
                if status == "running" and call_id not in turn.reported_tools:
                    turn.reported_tools.add(call_id)
                    return tool_call_event(part, state, call_id, line)
                if status in {"completed", "error"}:
                    if call_id not in turn.reported_tools:
                        # It never ran, as when an interrupt stops a pending
                        # call: the update that ends it opens it as well.
                        turn.reported_tools.add(call_id)
                        turn.unopened_call = tool_call_event(part, state, call_id, line)
                    content = str(state.get("output") or state.get("error") or "")
                    # A task's result is its subagent's whole answer, so only the
                    # marked event bound applies to it. Other tools keep a short
                    # preview that says it is one.
                    clipped = part.get("tool") != "task" and len(content) > 4000
                    return HarnessEvent(
                        TOOL_RESULT,
                        {
                            "tool_use_id": call_id,
                            "is_error": status == "error",
                            "content": content[:4000] if clipped else content,
                            **({"truncated": True} if clipped else {}),
                        },
                        raw_line=line,
                    )
            if event_type == "session.error":
                return HarnessEvent(
                    ERROR,
                    {"text": _http_agent_error_text(properties.get("error"))[:2000]},
                    raw_line=line,
                )
            return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
        if bridge == "settled":
            # Reported by events_for_protocol_line, one message per part.
            return HarnessEvent(LIFECYCLE, {"phase": "result"}, raw_line=line)
        if bridge == "context":
            # The bridge's own read of what this turn's first request carries,
            # sent only while the turn is active; nothing waits for it.
            if value.get("turn_id") != turn.protocol_turn_id:
                return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
            return HarnessEvent(LIFECYCLE, bridge_report(value, runtime=self.runtime.name))
        if bridge in {"fatal", "command_error"}:
            return HarnessEvent(
                ERROR, {"text": str(value.get("detail") or "")[:2000]}, raw_line=line
            )
        return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)

    def events_for_protocol_line(
        self, line: str, turn: JsonResidentTurn[ResidentLiveRuntime]
    ) -> list[HarnessEvent]:
        """At settlement, the assistant's answer: one message per text part.

        Each carries its part's ``preview_id``, so the complete text takes the
        place of its preview, between the tool calls it was written around.
        The turn's final output is still every part joined, as before.

        The update ending a call that never ran also stands for its call, which
        comes first.
        """
        assert isinstance(turn, HttpAgentLiveTurn)
        value = self._object(line)
        if (
            value is None
            or value.get("bridge") != "settled"
            or value.get("turn_id") != turn.protocol_turn_id
        ):
            event = self.event_for_protocol_line(line, turn)
            opened, turn.unopened_call = turn.unopened_call, None
            return [event] if opened is None else [opened, event]
        turn.preview.clear()
        injected = turn.context.pending(turn)
        answer = [
            HarnessEvent(MESSAGE, {"text": text, "preview_id": part_id}, raw_line=line)
            for part_id, (message_id, text) in turn.text_parts.items()
            if text.strip()
            and turn.message_roles.get(message_id) == "assistant"
            and not turn.context.is_synthetic(part_id)
        ]
        return [*injected, *(answer or [self.event_for_protocol_line(line, turn)])]

    def interrupt_turn(self, turn: JsonResidentTurn[ResidentLiveRuntime]) -> None:
        """Interrupt only the active bridge turn on the resident session."""
        self._write_json(
            {
                "id": self._request_id("interrupt"),
                "type": "interrupt",
                "turn_id": turn.protocol_turn_id,
            }
        )

    def fork_native(
        self,
        *,
        source_session_id: str,
        operation_id: str,
        source_native_turn_no: int,
    ) -> str:
        """Create an OpenCode/Kilo child at one settled native turn."""
        if source_session_id != self.runtime.session_id:
            raise DriverError(f"{self.runtime.name} fork source no longer matches")
        request_id = self._request_id(f"fork-{operation_id}")
        self._write_json(
            {
                "id": request_id,
                "type": "fork",
                "source_native_turn_no": source_native_turn_no,
            }
        )
        response = self._read_until(
            lambda value: (
                value.get("bridge") in {"fork_result", "command_error", "fatal"}
                and (value.get("id") in {None, "", request_id})
            )
        )
        if response.get("bridge") != "fork_result":
            raise DriverError(f"{self.runtime.name} fork failed: {response.get('detail')}")
        child_id = str(response.get("session_id") or "")
        if not child_id or child_id == source_session_id:
            raise DriverError(f"{self.runtime.name} fork returned an invalid child id")
        return child_id

    def close(self) -> None:
        """Let the HTTP server flush SQLite before using the kill backstop."""
        self._closed = True
        process = self._process
        if process is None:
            return
        process.close_stdin()
        deadline = time.monotonic() + 6
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
        process.wait()
