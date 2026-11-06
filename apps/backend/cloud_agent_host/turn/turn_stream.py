"""Relay one admitted turn's frames from the sandbox manager under the attempt's fence."""

from __future__ import annotations

import contextlib
import queue
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.bounded_output import OutputPump, TextTail
from cloud_agent_host.claimed_job import ClaimedJob, LeaseLost
from cloud_agent_host.drivers import LINE
from cloud_agent_host.runtimes import NormalizedEvent
from cloud_agent_host.sandbox_manager_client import (
    SandboxManagerClient,
    SandboxManagerError,
)
from cloud_agent_shared import operation_profiling

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.control_plane_client import (
        ControlPlane,
    )
    from cloud_agent_host.service.attempt_lease import Heartbeater

# How often the control checks run while the agent is silent.
_POLL_INTERVAL_S = 0.5


# The sandbox manager closes guest telemetry and enforces the member activation
# boundary after an interrupt. Keep draining its terminal frame so a cancel or
# timeout cannot strand the member lock or discard the already-closed resource
# report. Sandbox Manager lifecycle requests have the same 180 second request ceiling;
# the turn stream gets the execution budget plus this drain window.
_SHARED_TURN_INTERRUPT_DRAIN_S = 180.0


#: The top-level keys a turn summary must carry before this host will post it.
#: The control plane owns the authoritative schema; this is only the check
#: that says "a report, not a fragment", so a producer bug cannot post a body
#: that the endpoint would reject as malformed. A rejected report is worse
#: than an absent one: it reads as a platform error instead of a measurement
#: that was never taken.
_TURN_RESOURCES_KEYS = (
    "measurement_id",
    "report_seq",
    "state",
    "window",
    "cpu",
    "memory",
    "storage_io",
)


def _report_checkpoint_quietly(control: Any, payload: dict[str, Any]) -> None:
    """Report one running checkpoint, never letting it disturb the stream.

    A checkpoint exists so a crashed turn keeps the work it already did. That
    makes it strictly optional at the time it is taken: if the report cannot
    be delivered, the next checkpoint and the final snapshot carry the same
    cumulative state, so a failure here must not surface as a turn error.
    """
    with contextlib.suppress(Exception):
        control.report_turn_resources(payload)


def _reportable_turn_resources(payload: Any) -> dict[str, Any] | None:
    """Return the snapshot to post, or None when there is nothing honest to send.

    ``None`` is a real answer, not a failure: a turn whose sandbox manager returned no
    summary was not measured, and a synthesized empty snapshot would store
    "this turn used nothing" -- a claim nobody made. The attempt's legacy
    resource row already says "no member totals were attributed", so silence
    here leaves the two consistent.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != 1:
        return None
    if any(key not in payload for key in _TURN_RESOURCES_KEYS):
        return None
    return payload


def _stream_shared_agent_turn(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
    *,
    supervisor: SandboxManagerClient,
    job: ClaimedJob,
    control: ControlPlane,
    heart: Heartbeater,
    lease_token: str,
    timeout_s: float,
    measurement: operation_profiling.Measurement | None = None,
    on_frozen: Callable[[operation_profiling.Measurement], None] | None = None,
) -> tuple[int, str, list[str], dict[str, Any] | None, str | None]:
    """Relay exactly one sandbox-manager-framed member turn under its attempt fence.

    Handing the prompt to the supervisor closes the startup measurement:
    everything after that point is model execution, which the design keeps in a
    separate post-dispatch fragment. The caller marks the ``prompt_dispatch``
    boundary immediately before this call, so the handoff is one stage in the
    startup timeline rather than a stage of its own after it.

    The last element is what the turn itself said went wrong, when it said
    anything: the Agent CLI's own last error, or else the sandbox manager's. It is the
    failure's reason; the output tail is only a fallback for when neither
    exists, because its last lines are whatever the CLI printed last -- for a
    Codex turn refused with a 403 that was a configuration warning.
    """
    stream = supervisor.start_shared_turn(
        job.thread_id,
        token=lease_token,
        prompt=job.task_prompt,
        model=job.model,
        reasoning_effort=job.reasoning_effort,
        # Keep a quiet stream open through the watchdog's execution deadline
        # and terminal-resource drain, including its next control-loop poll.
        read_timeout_s=max(0.0, timeout_s) + _SHARED_TURN_INTERRUPT_DRAIN_S + _POLL_INTERVAL_S,
    )
    operation_profiling.finish(measurement, outcome="dispatched")
    # Queued here, not when the turn ends. The prompt has been handed over and
    # the startup is complete and frozen, so the sample exists now -- while the
    # model may run for another hour. Waiting would both delay an operator's
    # view of it and, on a long enough turn, deliver it after the experiment's
    # grace period, which the endpoint correctly refuses.
    if measurement is not None and on_frozen is not None:
        on_frozen(measurement)
    frames: OutputPump[dict[str, Any]] | None = None
    deadline = time.monotonic() + timeout_s
    tail = TextTail()
    tool_errors: list[str] = []
    result_frame: dict[str, Any] | None = None
    forced_exit_code: int | None = None
    interrupt_deadline: float | None = None
    agent_error: str | None = None
    sandbox_error: str | None = None

    def outcome(exit_code: int) -> tuple[int, str, list[str], dict[str, Any] | None, str | None]:
        # Events the control plane could not record fail the turn before its outcome counts.
        control.flush()
        if agent_error:
            reported = f"agent reported an error: {agent_error[:900]}"
        elif sandbox_error:
            reported = f"sandbox failed: {sandbox_error[:900]}"
        else:
            reported = None
        return exit_code, tail.text(), tool_errors, result_frame, reported

    try:
        # The sandbox manager admits and dispatches the prompt before opening this
        # response stream. Resident follow-ups need not emit a native init
        # event, so store this shared boundary before waiting for CLI output.
        control.append_event(NormalizedEvent("lifecycle", {"phase": "turn_started"}))
        control.flush()
        frames = OutputPump(stream.frames(), name="shared-agent-stream")
        while True:
            if heart.lease_lost or control.lease_lost:
                with contextlib.suppress(Exception):
                    supervisor.interrupt_shared(job.thread_id, token=lease_token)
                raise LeaseLost("lease lost while the shared Agent CLI was running")
            now = time.monotonic()
            if heart.cancel_requested and forced_exit_code is None:
                with contextlib.suppress(Exception):
                    supervisor.interrupt_shared(job.thread_id, token=lease_token)
                control.append_event(NormalizedEvent("lifecycle", {"phase": "cancelled_by_owner"}))
                forced_exit_code = 130
                interrupt_deadline = now + _SHARED_TURN_INTERRUPT_DRAIN_S
            if forced_exit_code is None and now > deadline:
                with contextlib.suppress(Exception):
                    supervisor.interrupt_shared(job.thread_id, token=lease_token)
                control.append_event(
                    NormalizedEvent("error", {"text": f"agent exceeded {timeout_s:.0f}s"})
                )
                forced_exit_code = 124
                interrupt_deadline = now + _SHARED_TURN_INTERRUPT_DRAIN_S
            if interrupt_deadline is not None and now > interrupt_deadline:
                return outcome(forced_exit_code or 1)
            try:
                frame = frames.get(timeout=_POLL_INTERVAL_S)
            except queue.Empty:
                continue
            except Exception as exc:
                if result_frame is not None:
                    break
                raise SandboxManagerError(502, str(exc)[:1000]) from exc
            if frame is None:
                break
            kind = frame.get("kind")
            if kind == "event":
                raw_line = frame.get("raw_line")
                if isinstance(raw_line, str):
                    tail.append(raw_line)
                event_type = str(frame.get("event_type") or "")
                payload = frame.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                if event_type == "tool_result" and payload.get("is_error"):
                    content = str(payload.get("content") or "")
                    tool_errors.append(content[:200])
                if event_type == "error":
                    # Each driver reports its CLI's own failure this way: a
                    # Claude error result, Codex's final error, pi's failed
                    # prompt or agent end, OpenCode and Kilo's session error.
                    text = str(payload.get("text") or payload.get("detail") or "").strip()
                    if text:
                        agent_error = text
                if event_type and event_type != LINE:
                    control.append_event(NormalizedEvent(event_type, payload))
                continue
            if kind == "resource_checkpoint":
                # A running checkpoint, not a terminal frame. It is reported
                # as `collecting` so a crash keeps the work measured up to
                # here; the closing snapshot carries the same counters and the
                # higher sequence, which is what supersedes this one.
                checkpoint = _reportable_turn_resources(frame.get("turn_resources"))
                if checkpoint is not None:
                    _report_checkpoint_quietly(control, checkpoint)
                continue
            if kind == "result":
                result_frame = frame
                stderr = str(frame.get("stderr") or "")
                if stderr.strip():
                    tail.append(stderr[-2000:])
                continue
            if kind == "error":
                result_frame = frame
                detail = str(frame.get("detail") or "shared turn failed")
                tail.append(detail[-2000:])
                sandbox_error = detail
                return outcome(forced_exit_code or 1)
            if kind == "client_error":
                # The result may already have crossed the wire before the
                # transport failed. Preserve that terminal frame rather than
                # converting a completed activation into an opaque 502.
                if result_frame is not None:
                    break
                raise SandboxManagerError(502, str(frame.get("detail") or "shared turn failed"))
            raise SandboxManagerError(502, "shared turn returned an unknown frame")
        if result_frame is None:
            raise SandboxManagerError(502, "shared turn ended without a result")
        if forced_exit_code is not None:
            return outcome(forced_exit_code)
        status = str(result_frame.get("status") or "failed")
        exit_code = int(result_frame.get("exit_code") or 0)
        if status == "cancelled":
            exit_code = 130
        elif status != "completed" and exit_code == 0:
            exit_code = 1
        if tool_errors:
            summary = {"detail": f"{len(tool_errors)} tool call(s) failed", "first": tool_errors[0]}
            control.append_event(NormalizedEvent("error", summary))
        return outcome(exit_code)
    finally:
        if frames is not None:
            frames.close()
        stream.close()
