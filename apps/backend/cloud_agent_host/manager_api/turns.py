"""One shared turn: the resident's events streamed as frames, and its resource report."""

from __future__ import annotations

import contextlib
import json
import queue
import time
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.concurrency import iterate_in_threadpool
from fastapi.responses import StreamingResponse  # noqa: TC002 — FastAPI resolves it.

from cloud_agent_host.bounded_output import OutputPump
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.manager_api.calls import _EndingStreamingResponse
from cloud_agent_host.manager_api.context import supervisor_error
from cloud_agent_host.manager_api.models import (
    SharedTurnRequest,  # noqa: TC001 — FastAPI resolves it.
)
from cloud_agent_host.manager_api.routing import route
from cloud_agent_host.member_sessions import SharedTurnBoundaryError, SharedTurnTelemetry
from cloud_agent_host.observe.guest_observer import (
    GuestObservation,
    add_process_counts,
    add_process_resources,
    attribute_processes,
)
from cloud_agent_host.observe.resources import shared_member_resource_rollup
from cloud_agent_host.observe.tool_windows import ToolWindowTracker, attribute, tool_time_ms
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterator

    from cloud_agent_host.manager_api.context import ManagerContext
    from cloud_agent_host.resident_transport import ResidentTurn
    from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor

# The sandbox manager's own logger: one line per turn's tool marks keeps its name.
logger = get_logger("cloud_agent_host.sandbox_manager")


def build_turn_resources(
    value: Any,
    tool_windows: ToolWindowTracker,
    *,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """The resource object one finished shared turn reports.

    Named and module-level so the frame a turn actually emits can be built by a
    test. The contract between this producer and the runner that reads it lived
    in two closures either side of an HTTP hop, and a hand-written frame in the
    runner's test hid a nesting change that made every real turn report
    nothing.
    """
    telemetry = (
        value
        if isinstance(value, SharedTurnTelemetry)
        else SharedTurnTelemetry(
            observation=(
                value
                if isinstance(value, GuestObservation)
                else GuestObservation.unavailable("shared turn returned no process telemetry")
            )
        )
    )
    marks = tool_windows.apply_marks(telemetry.tool_marks)
    # One line per turn: every call's pairing, lag and reason, so mark
    # adoption can be checked from the sandbox manager log without a database read.
    logger.info("tool marks thread=%s %s", thread_id, marks.describe())
    windows = tool_windows.windows()
    resources: dict[str, Any] = shared_member_resource_rollup()
    tool_calls = attribute(
        [],
        windows,
        windows_complete=not tool_windows.truncated,
    )
    processes, process_counts = attribute_processes(
        telemetry.observation,
        windows,
        windows_complete=not tool_windows.truncated,
    )
    resources.update(
        {
            "tool_calls": add_process_resources(
                add_process_counts(tool_calls, process_counts),
                processes,
                observation_complete=telemetry.observation.complete,
            ),
            "tool_calls_truncated": tool_windows.truncated,
            "tool_time_ms": (None if tool_windows.truncated else tool_time_ms(windows)),
            "process_observer": telemetry.observation.source,
            "process_observation_complete": telemetry.observation.complete,
            "process_events_lost": telemetry.observation.lost_events,
            "processes_truncated": telemetry.observation.truncated,
            "processes": processes,
        }
    )
    # The whole-turn member execution window travels beside the legacy rollup
    # rather than inside it: it is a different measurement with its own
    # endpoint, and folding it into this dict would make the shared-member
    # guard's terms ambiguous.
    if telemetry.turn_resources is not None:
        resources["turn_resources"] = telemetry.turn_resources
    return resources


def _take_reported_checkpoint(
    supervisor: SharedChatSessionSupervisor,
    member_thread_id: str,
    member_turn: ResidentTurn[Any],
) -> dict[str, Any] | None:
    """Collect a checkpoint the sampler thread has already produced for this turn.

    The sampler runs on its own clock, so a turn that emits nothing for
    a minute still gets its uploads; this only forwards what is waiting.
    A stream that outlived its turn takes nothing from the member's next
    one. Best effort by construction: a checkpoint exists so a crash
    keeps the work already measured, and a failure to forward one must
    not reach the turn's own event stream as an error.
    """
    try:
        return supervisor.take_reported_checkpoint(member_thread_id, member_turn)
    except Exception:
        return None


def _frame(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":")) + "\n"


class _TurnBoundary:
    """Whether a turn's activation boundary has closed, as completed or aborted."""

    closed = False


def _failed_turn_telemetry(
    exc: Exception,
    owner: SharedChatSessionSupervisor,
    thread_id: str,
    token: str,
    turn: ResidentTurn[Any],
    boundary: _TurnBoundary,
) -> SharedTurnTelemetry | None:
    """Close a failed turn's boundary, if it is still open, and return its telemetry."""
    telemetry = exc.telemetry if isinstance(exc, SharedTurnBoundaryError) else None
    if telemetry is not None:
        boundary.closed = True
    if not boundary.closed:
        try:
            telemetry = owner.abort_turn(thread_id, token, turn)
            boundary.closed = True
        except SharedTurnBoundaryError as boundary_exc:
            telemetry = boundary_exc.telemetry
            boundary.closed = True
        except Exception:
            telemetry = None
    return telemetry


def _turn_frames(
    owner: SharedChatSessionSupervisor,
    thread_id: str,
    token: str,
    turn: ResidentTurn[Any],
    boundary: _TurnBoundary,
) -> Generator[str, None, None]:
    """The turn's frames: its checkpoints and events, then one result or error."""
    tool_windows = ToolWindowTracker()
    resources: dict[str, Any] | None = None

    def close_resources(value: Any) -> dict[str, Any]:
        nonlocal resources
        if resources is not None:
            return resources
        resources = build_turn_resources(value, tool_windows, thread_id=thread_id)
        return resources

    def observed_events() -> Iterator[Any]:
        for event in turn.lines():
            # Timestamp at the native read boundary, before queueing:
            # backpressure must not move a tool's message window past
            # the trusted guest hook marks collected within it.
            tool_windows.observe(event.event_type, event.payload, mono=time.monotonic())
            yield event

    events = OutputPump(observed_events(), name="shared-turn-events")
    try:
        while True:
            # Sampling and forwarding must both proceed while the CLI
            # is silent. A crash should retain its latest closed
            # checkpoint even during one long tool invocation.
            checkpoint = _take_reported_checkpoint(owner, thread_id, turn)
            if checkpoint is not None:
                yield _frame({"kind": "resource_checkpoint", "turn_resources": checkpoint})
            try:
                event = events.get(timeout=0.25)
            except queue.Empty:
                # Hand the pulling thread back while the CLI is quiet:
                # a response whose reader has gone can end only once
                # the pull it is waiting on returns.
                yield ""
                continue
            if event is None:
                break
            yield _frame(
                {
                    "kind": "event",
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "raw_line": event.raw_line,
                }
            )
        result = turn.wait()
        # The activation boundary is part of success, not cleanup
        # after success. It reaps escaped background processes and
        # normalizes shared writes; if either fails the runner must
        # receive an error frame instead of a result followed by an
        # opaque settle failure.
        telemetry: SharedTurnTelemetry | None = owner.complete_turn(thread_id, token, turn)
        boundary.closed = True
        resources = close_resources(telemetry)
        yield _frame(
            {
                "kind": "result",
                "status": result.status,
                "final_output": result.final_output,
                "exit_code": result.exit_code,
                "usage": result.usage,
                "raw": result.raw,
                "native_turn_settled": result.native_turn_settled,
                "stderr": turn.stderr_text()[-4000:],
                "resources": resources,
            }
        )
    except Exception as exc:
        telemetry = _failed_turn_telemetry(exc, owner, thread_id, token, turn, boundary)
        yield _frame(
            {
                "kind": "error",
                "detail": str(exc)[:1000],
                "resources": close_resources(telemetry),
            }
        )
    finally:
        events.close()
        with contextlib.suppress(Exception):
            if not boundary.closed:
                owner.abort_turn(thread_id, token, turn)
                boundary.closed = True


class TurnRoutes:
    """The one route that runs a prompt on a member's resident process."""

    def __init__(self, context: ManagerContext) -> None:
        self._context = context

    @route("POST", "/shared-sandboxes/{thread_id}/turn")
    async def run_shared_turn(self, thread_id: str, body: SharedTurnRequest) -> StreamingResponse:
        """Start one turn and stream its frames as NDJSON until its result or error."""
        owner = self._context.require_shared_supervisor()
        endpoint_calls = self._context.endpoint_calls
        try:
            turn = await endpoint_calls.run(
                owner.start_turn,
                thread_id,
                body.token,
                prompt=body.prompt,
                model=body.model,
                reasoning_effort=body.reasoning_effort,
            )
        except (DriverError, SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

        boundary = _TurnBoundary()
        lines = _turn_frames(owner, thread_id, body.token, turn, boundary)

        async def stream() -> AsyncIterator[str]:
            async for line in iterate_in_threadpool(lines):
                if line:
                    yield line

        def end_stream() -> None:
            # Closing the frames aborts a turn they started and did not
            # close; a reader gone before they started leaves it to this.
            lines.close()
            if not boundary.closed:
                with contextlib.suppress(Exception):
                    owner.abort_turn(thread_id, body.token, turn)

        async def ended() -> None:
            # However the response ended -- the runner's process may have
            # died -- the member's turn, lease and lock must not wait for a
            # collector. Shutdown owns the call, as it owns every lifecycle
            # call, and once sealed it closes the supervisor itself.
            with contextlib.suppress(HTTPException):
                await endpoint_calls.run(end_stream)

        return _EndingStreamingResponse(
            stream(),
            on_end=ended,
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )


__all__ = ["TurnRoutes", "build_turn_resources"]
