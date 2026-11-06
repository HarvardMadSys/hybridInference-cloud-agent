"""pi's live session: its RPC mode over one resident process."""

from __future__ import annotations

from typing import Any

from cloud_agent_host.drivers import (
    DriverError,
    JsonResidentSession,
    JsonResidentTurn,
    TurnInput,
)
from cloud_agent_host.harness_event import LINE, HarnessEvent
from cloud_agent_host.runtimes import (
    ERROR,
    PiRuntime,
)


class PiRpcSession(JsonResidentSession[PiRuntime]):
    """pi RPC mode with an explicit native session locator.

    Verified against the image's 0.73.1 and against 1.2.0 from the package pi
    moved to, ``@earendil-works/pi-coding-agent``: the commands this uses, a
    turn, a fork, a clone, a resume, an abort and an automatic retry.
    """

    def _initialize_protocol(self) -> None:
        request_id = self._request_id("state")
        self._write_json({"id": request_id, "type": "get_state"})
        response = self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == request_id
        )
        if response.get("success") is not True:
            raise DriverError(f"pi get_state failed: {response.get('error')}")
        data = response.get("data")
        session_id = data.get("sessionId") if isinstance(data, dict) else None
        if not session_id:
            raise DriverError("pi get_state returned no session id")
        if self._resume_session_id and session_id != self._resume_session_id:
            raise DriverError("pi resumed a different native session")
        self.runtime.session_id = str(session_id)

    def _start_protocol_turn(
        self, turn_input: TurnInput, turn: JsonResidentTurn[PiRuntime]
    ) -> None:
        request_id = self._request_id("prompt")
        turn.protocol_turn_id = request_id
        self._write_json({"id": request_id, "type": "prompt", "message": turn_input.prompt})

    def line_settles_turn(
        self, line: str, turn: JsonResidentTurn[PiRuntime]
    ) -> tuple[bool, bool, bool]:
        """Recognize Pi's response failure or terminal agent-end event."""
        value = self._object(line)
        if value is None:
            return False, False, False
        if (
            value.get("type") == "response"
            and value.get("id") == turn.protocol_turn_id
            and value.get("success") is False
        ):
            return True, True, False
        # From 0.75.4 a transient failure ends the run with ``willRetry`` and
        # pi runs it again; the turn ends with the run that is not retried.
        if value.get("type") == "agent_end" and value.get("willRetry") is not True:
            return True, self.runtime.agent_error(value) is not None, True
        return False, False, False

    def event_for_protocol_line(self, line: str, turn: JsonResidentTurn[PiRuntime]) -> HarnessEvent:
        """Normalize one Pi RPC protocol event for the active turn."""
        value = self._object(line)
        if (
            value is not None
            and value.get("type") == "response"
            and value.get("id") == turn.protocol_turn_id
            and value.get("success") is False
        ):
            # The prompt failed before it ran (0.73.1 answers this way only
            # before preflight succeeds); later failures end in agent_end.
            text = str(value.get("error") or "pi did not accept the prompt")
            return HarnessEvent(ERROR, {"text": text[:2000]}, raw_line=line)
        event = self.runtime.parse_event(line)
        if event is None:
            return HarnessEvent(LINE, raw_line=line)
        return HarnessEvent(event.event_type, dict(event.payload or {}), raw_line=line)

    def events_for_protocol_line(
        self, line: str, turn: JsonResidentTurn[PiRuntime]
    ) -> list[HarnessEvent]:
        """Every event a line stands for, through :meth:`PiRuntime.parse_events`.

        An assistant message's ``message_end`` releases the blocks held for its
        response id, so one line can stand for several events.
        """
        value = self._object(line)
        if value is not None and value.get("type") == "response":
            return [self.event_for_protocol_line(line, turn)]
        events = self.runtime.parse_events(line)
        if not events:
            return [HarnessEvent(LINE, raw_line=line)]
        return [
            HarnessEvent(event.event_type, dict(event.payload or {}), raw_line=line)
            for event in events
        ]

    def interrupt_turn(self, turn: JsonResidentTurn[PiRuntime]) -> None:
        """Abort the active Pi RPC turn without closing its session process."""
        self._write_json({"id": self._request_id("abort"), "type": "abort"})

    def fork_native(
        self,
        *,
        source_session_id: str,
        operation_id: str,
        source_native_turn_no: int,
    ) -> str:
        """Fork Pi before the next prompt, or clone at the current leaf."""
        if source_session_id != self.runtime.session_id:
            raise DriverError("pi fork source no longer matches the live session")
        if source_native_turn_no < 1:
            raise DriverError("pi fork turn must be positive")

        list_id = self._request_id(f"fork-list-{operation_id}")
        self._write_json({"id": list_id, "type": "get_fork_messages"})
        listed = self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == list_id
        )
        data = listed.get("data") if listed.get("success") is True else None
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list) or source_native_turn_no > len(messages):
            detail = listed.get("error") if listed.get("success") is not True else None
            raise DriverError(
                str(detail)
                if detail
                else "pi native history does not contain the requested fork turn "
                f"({source_native_turn_no} of "
                f"{len(messages) if isinstance(messages, list) else 0})"
            )

        fork_id = self._request_id(f"fork-{operation_id}")
        if source_native_turn_no < len(messages):
            # Pi's RPC `fork` branches immediately *before* the selected user
            # entry. Selecting turn N+1 therefore includes the complete Nth
            # exchange and excludes the prompt the user is about to edit.
            next_message = messages[source_native_turn_no]
            entry_id = next_message.get("entryId") if isinstance(next_message, dict) else None
            if not isinstance(entry_id, str) or not entry_id:
                raise DriverError("pi next fork message has no native entry id")
            command: dict[str, Any] = {"id": fork_id, "type": "fork", "entryId": entry_id}
            expected_command = "fork"
        else:
            command = {"id": fork_id, "type": "clone"}
            expected_command = "clone"
        self._write_json(command)
        forked = self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == fork_id
        )
        fork_data = forked.get("data") if forked.get("success") is True else None
        if (
            forked.get("command") != expected_command
            or not isinstance(fork_data, dict)
            or fork_data.get("cancelled") is not False
        ):
            raise DriverError(f"pi native fork failed: {forked.get('error') or fork_data}")

        state_id = self._request_id(f"fork-state-{operation_id}")
        self._write_json({"id": state_id, "type": "get_state"})
        state = self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == state_id
        )
        state_data = state.get("data") if state.get("success") is True else None
        child_id = state_data.get("sessionId") if isinstance(state_data, dict) else None
        if not isinstance(child_id, str) or not child_id or child_id == source_session_id:
            raise DriverError("pi native fork returned an invalid child session id")
        self.runtime.session_id = child_id
        return child_id
