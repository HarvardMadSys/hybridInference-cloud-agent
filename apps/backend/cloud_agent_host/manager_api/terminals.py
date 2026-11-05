"""A workspace's interactive terminals: opening, streaming, keystrokes and suspension."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import time
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from cloud_agent_host.manager_api.context import supervisor_error
from cloud_agent_host.manager_api.models import (  # noqa: TC001 — FastAPI resolves the request models.
    TerminalCoordinationRequest,
    TerminalCreateRequest,
    TerminalInputRequest,
    TerminalResizeRequest,
)
from cloud_agent_host.manager_api.routing import route
from cloud_agent_host.operation_call import open_call
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.operation_profiling import PROFILING_CONTEXT_HEADER
from cloud_agent_shared.sandbox_restore import START_FENCE_HEADER, parse_start_fence

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent_host.manager_api.context import ManagerContext
    from cloud_agent_host.terminal_session import _TerminalEvent

TERMINAL_STREAM_WAIT_S = 15.0


def _terminal_sse_frame(event: _TerminalEvent) -> str:
    """Serialize one terminal event using the control plane's stable SSE contract."""
    if event.kind == "output":
        payload = {
            "seq": event.seq,
            "data": base64.b64encode(event.data).decode("ascii"),
        }
    elif event.kind == "reset":
        payload = {"seq": event.seq, "reason": event.reason}
    else:
        payload = {"seq": event.seq, "exit_code": event.exit_code}
    data = json.dumps(payload, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n"


def _terminal_attached_frame(last_seq: int) -> str:
    """Tell a new viewer where the terminal's history ends.

    Events up to ``last_seq`` were produced before the viewer attached. A fresh
    screen replaying them must not answer the terminal queries among them --
    the program that asked is long past waiting, or gone -- or the answers
    reach the shell as typed input. It is not a resumable event, so it has no
    id; a client that does not know it ignores it.
    """
    data = json.dumps({"seq": last_seq}, separators=(",", ":"))
    return f"event: attached\ndata: {data}\n\n"


class TerminalRoutes:
    """The terminals of one group's workspace, addressed through any of its members."""

    def __init__(self, context: ManagerContext) -> None:
        self._context = context

    @route("POST", "/workspaces/{workspace_id}/terminals")
    async def create_terminal(
        self, workspace_id: str, request: Request, body: TerminalCreateRequest
    ) -> dict[str, Any]:
        """Open one terminal, or run one environment action in it."""
        context = self._context
        endpoint_calls = context.endpoint_calls
        call = open_call(
            request.headers.get(PROFILING_CONTEXT_HEADER), operations=("terminal.open",)
        )
        owner = await endpoint_calls.run(context.retained_owner, workspace_id)
        start_seq = parse_start_fence(request.headers.get(START_FENCE_HEADER))
        terminal_token: str | None = None
        try:
            if body.environment is not None and body.environment.required:
                await endpoint_calls.run(
                    owner.prepare_environment,
                    workspace_id,
                    body.environment,
                    context.environment_preparation,
                    start_seq=start_seq,
                )
            # Admission proves the group network for this PTY.
            with call.stage("admission"):
                token, descriptor = await endpoint_calls.run(
                    owner.terminal_opened, workspace_id, start_seq=start_seq
                )
            terminal_token = token
            retained = await endpoint_calls.run(owner.backend, workspace_id)

            def on_terminal_closed(last_activity: float) -> None:
                owner.terminal_closed(
                    workspace_id,
                    token,
                    last_activity_monotonic=last_activity,
                )

            with call.stage("pty_spawn"):
                body_out = await endpoint_calls.run(
                    context.terminals.create,
                    descriptor.origin_thread_id,
                    "/workspace",
                    rows=body.rows,
                    cols=body.cols,
                    backend=retained,
                    idle_ttl_s=owner.idle_timeout_s,
                    on_closed=on_terminal_closed,
                    environment=body.environment,
                    action_id=body.action_id,
                    run_id=body.run_id,
                )
            return call.answer(body_out)
        except (HTTPException, SandboxError, ValueError) as exc:
            if terminal_token is not None:
                with contextlib.suppress(HTTPException, SandboxError):
                    await endpoint_calls.run(owner.terminal_closed, workspace_id, terminal_token)
            if isinstance(exc, HTTPException):
                raise
            raise supervisor_error(exc) from exc

    @route("GET", "/workspaces/{workspace_id}/terminals/{terminal_id}/output")
    async def terminal_output(self, workspace_id: str, terminal_id: str) -> dict[str, Any]:
        """Summarize one terminal's output, or an action's durable result."""
        context = self._context

        def read_output() -> dict[str, Any]:
            output = context.terminals.output(context.terminal_scope(workspace_id), terminal_id)
            if (
                output["state"] == "running"
                and context.retained_owner(workspace_id).status(workspace_id).snapshotting
            ):
                output["state"] = "paused"
            return output

        # Resolving the scope and the status inspect Docker; reaping and the
        # durable receipt touch Docker and disk. None of it runs on the loop.
        return await context.endpoint_calls.run(read_output)

    @route("POST", "/workspaces/{workspace_id}/terminals/suspend")
    async def suspend_terminals(
        self, workspace_id: str, body: TerminalCoordinationRequest
    ) -> dict[str, Any]:
        """Freeze the group's terminals for one protected turn."""
        endpoint_calls = self._context.endpoint_calls
        scope = await endpoint_calls.run(self._context.terminal_scope, workspace_id)
        return await endpoint_calls.run(self._context.terminals.suspend, scope, job_id=body.job_id)

    @route("POST", "/workspaces/{workspace_id}/terminals/resume")
    async def resume_terminals(
        self, workspace_id: str, body: TerminalCoordinationRequest
    ) -> dict[str, Any]:
        """Release one turn's suspension, resuming the terminals after the last."""
        endpoint_calls = self._context.endpoint_calls
        scope = await endpoint_calls.run(self._context.terminal_scope, workspace_id)
        return await endpoint_calls.run(self._context.terminals.resume, scope, job_id=body.job_id)

    @route("POST", "/workspaces/{workspace_id}/terminals/resume-settled")
    async def resume_settled_terminals(
        self, workspace_id: str, body: TerminalCoordinationRequest
    ) -> dict[str, Any]:
        """Release a settled turn's suspension."""
        # A settled turn releases its own token like any other resume; a
        # concurrent sibling's suspension stays in force.
        endpoint_calls = self._context.endpoint_calls
        scope = await endpoint_calls.run(self._context.terminal_scope, workspace_id)
        return await endpoint_calls.run(self._context.terminals.resume, scope, job_id=body.job_id)

    @route("GET", "/workspaces/{workspace_id}/terminals")
    async def list_terminals(self, workspace_id: str, request: Request) -> dict[str, Any]:
        """List the group's open terminals and the limits a new one meets."""
        context = self._context
        terminals = context.terminals
        call = open_call(
            request.headers.get(PROFILING_CONTEXT_HEADER), operations=("terminal.open",)
        )
        scope = await context.endpoint_calls.run(context.terminal_scope, workspace_id)
        with call.stage("list"):
            listed, max_sessions = await context.endpoint_calls.run(
                lambda: (terminals.list(scope), terminals.max_sessions_per_workspace)
            )
            body = {
                "terminals": listed,
                "max_sessions": max_sessions,
                "max_input_bytes": int(context.settings.applied["terminal_input_max_kib"]) * 1024,
            }
        return call.answer(body)

    @route("GET", "/workspaces/{workspace_id}/terminals/{terminal_id}/stream")
    async def stream_terminal(
        self,
        workspace_id: str,
        terminal_id: str,
        request: Request,
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        """Stream one terminal's output after ``after`` as server-sent events."""
        context = self._context
        call = open_call(
            request.headers.get(PROFILING_CONTEXT_HEADER), operations=("terminal.open",)
        )
        scope = await context.endpoint_calls.run(context.terminal_scope, workspace_id)
        with call.stage("stream_attach"):
            session = await context.endpoint_calls.run(context.terminals.get, scope, terminal_id)
            session.open_stream()

        async def event_stream() -> AsyncIterator[str]:
            cursor = after
            try:
                yield _terminal_attached_frame(session.descriptor()["last_seq"])
                while True:
                    events = await session.events_after_async(
                        cursor,
                        wait_s=TERMINAL_STREAM_WAIT_S,
                    )
                    for event in events:
                        cursor = event.seq
                        yield _terminal_sse_frame(event)
                    descriptor = session.descriptor()
                    if descriptor["state"] != "running" and cursor >= descriptor["last_seq"]:
                        return
                    if not events:
                        # Keep intermediaries from timing out a quiet interactive
                        # shell without inventing a sequenced client event.
                        yield ": keepalive\n\n"
            finally:
                session.close_stream()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            # A stream's body is the terminal's own bytes, so this call's
            # measurement answer travels in headers rather than in the body.
            headers=call.headers(extra={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}),
        )

    @route("POST", "/workspaces/{workspace_id}/terminals/{terminal_id}/input")
    async def terminal_input(
        self, workspace_id: str, terminal_id: str, body: TerminalInputRequest
    ) -> dict[str, Any]:
        """Write one keystroke chunk to an open terminal."""
        context = self._context
        scope = await context.endpoint_calls.run(context.keystroke_scope, workspace_id)
        try:
            data = base64.b64decode(body.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="terminal input is not valid base64"
            ) from exc
        if len(data) > int(context.settings.applied["terminal_input_max_kib"]) * 1024:
            raise HTTPException(status_code=413, detail="terminal input is too large")
        session = await context.endpoint_calls.run(
            context.terminals.get_for_control, scope, terminal_id
        )
        return await context.endpoint_calls.run(session.write, data, now=time.monotonic())

    @route("POST", "/workspaces/{workspace_id}/terminals/{terminal_id}/resize")
    async def resize_terminal(
        self, workspace_id: str, terminal_id: str, body: TerminalResizeRequest
    ) -> dict[str, Any]:
        """Resize one open terminal's window."""
        context = self._context
        scope = await context.endpoint_calls.run(context.keystroke_scope, workspace_id)
        session = await context.endpoint_calls.run(
            context.terminals.get_for_control, scope, terminal_id
        )
        return await context.endpoint_calls.run(
            session.resize,
            rows=body.rows,
            cols=body.cols,
            now=time.monotonic(),
        )

    @route("DELETE", "/workspaces/{workspace_id}/terminals/{terminal_id}")
    async def delete_terminal(self, workspace_id: str, terminal_id: str) -> dict[str, Any]:
        """Close one terminal."""
        context = self._context
        scope = await context.endpoint_calls.run(context.terminal_scope, workspace_id)
        return await context.endpoint_calls.run(context.terminals.delete, scope, terminal_id)


__all__ = ["TerminalRoutes"]
