"""Mid-turn input against the pinned CLIs, through the production drivers.

Claude Code and Codex run from the built sandbox image (``AGENT_STEER_TEST_IMAGE``,
or ``MCP_TEST_SANDBOX_IMAGE``) against a fake model, launched and driven exactly
as the sandbox manager drives them: the production driver, ``docker exec -i`` as the
member uid, one thread reading the turn's stream. The message arrives from a
second thread through the driver's own seam, ``HarnessTurn.inject`` — the call
the sandbox manager makes for the control plane's "send now".

What is pinned is the rule for mid-turn input — the model gets the message
before its next tool call, and a turn that answers without one never gets it:

- Claude, with a tool running: the turn's next model request carries the
  message, the stream says where the turn took it, and the turn ends once — the
  message did not become a turn of its own after the first one's result.
- Claude, before the model calls a tool: the message is held, written when the
  model does, and taken the same way.
- Claude, answering without a tool call: nothing is written, and no turn of its
  own follows.
- Claude, reading one too late: the turn it opens by itself is stopped, and the
  next turn reads only its own answer.
- Codex: ``turn/steer`` is accepted, the stream says so, and the next request
  of the *same* turn id carries the message.

Linux hosts set ``STEER_TEST_HOST`` to an address the container can reach (the
docker bridge gateway, e.g. 172.17.0.1); Docker Desktop's default is
``host.docker.internal``.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest
from tests.support.claude_cli import sse

from cloud_agent_host.drivers import (
    InputOutcome,
    LaunchContext,
    PendingInput,
    TurnInput,
    get_driver,
)
from cloud_agent_host.sandbox import _PopenProcess
from cloud_agent_shared.shared_protocol import USER_MESSAGE_EVENT

if TYPE_CHECKING:
    import pathlib

pytestmark = pytest.mark.external

_TOOL_SECONDS = 6
_WAIT_S = 120


def _image() -> str:
    image = os.getenv("AGENT_STEER_TEST_IMAGE") or os.getenv("MCP_TEST_SANDBOX_IMAGE")
    if not image:
        pytest.skip("set AGENT_STEER_TEST_IMAGE to the built sandbox image")
    return image


class _Model:
    """A fake model for one turn: records every request, answers by position.

    ``first`` is what the turn's first request gets: ``tool`` asks for a shell
    command that runs ``_TOOL_SECONDS``; ``hold`` answers with text only once
    ``release`` is set; ``hold_tool`` asks for the command once released. Every
    later request gets plain text, ``reply-<n>``, at once.
    """

    def __init__(self, first: str) -> None:
        self.first = first
        self.requests: list[dict[str, Any]] = []
        self.first_arrived = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), self._handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    def close(self) -> None:
        self._server.shutdown()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        model = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                if path.endswith("/count_tokens"):
                    self._send_json({"input_tokens": 1})
                    return
                with model._lock:
                    model.requests.append(body)
                    number = len(model.requests)
                if number == 1:
                    model.first_arrived.set()
                    if model.first.startswith("hold"):
                        model.release.wait(_WAIT_S)
                tool = number == 1 and model.first in {"tool", "hold_tool"}
                if path.endswith("/messages"):
                    self._anthropic(number, tool)
                else:
                    self._responses(number, tool)

            def log_message(self, *_args: Any) -> None:
                pass

            def _send_json(self, value: dict[str, Any]) -> None:
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _stream(self, raw: bytes) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

            def _anthropic(self, number: int, tool: bool) -> None:
                if tool:
                    block = {"type": "tool_use", "id": "toolu_steer_1", "name": "Bash", "input": {}}
                    delta = {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            {"command": f"sleep {_TOOL_SECONDS}", "description": "wait"}
                        ),
                    }
                else:
                    block = {"type": "text", "text": ""}
                    delta = {"type": "text_delta", "text": f"reply-{number}"}
                events = [
                    (
                        "message_start",
                        {
                            "message": {
                                "id": f"msg_{number}",
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "model": "test-model",
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            }
                        },
                    ),
                    ("content_block_start", {"index": 0, "content_block": block}),
                    ("content_block_delta", {"index": 0, "delta": delta}),
                    ("content_block_stop", {"index": 0}),
                    (
                        "message_delta",
                        {
                            "delta": {
                                "stop_reason": "tool_use" if tool else "end_turn",
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": 1},
                        },
                    ),
                    ("message_stop", {}),
                ]
                self._stream(b"".join(sse(kind, {"type": kind, **data}) for kind, data in events))

            def _responses(self, number: int, tool: bool) -> None:
                if tool:
                    item = {
                        "id": f"fc_{number}",
                        "call_id": f"call_{number}",
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": f"sleep {_TOOL_SECONDS}"}),
                        "status": "completed",
                    }
                else:
                    item = {
                        "id": f"msg_{number}",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": f"reply-{number}", "annotations": []}
                        ],
                    }
                response = {
                    "id": f"resp_{number}",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "test-model",
                    "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                events = [
                    (
                        "response.created",
                        {"response": {**response, "status": "in_progress", "output": []}},
                    ),
                    ("response.output_item.added", {"output_index": 0, "item": item}),
                    ("response.output_item.done", {"output_index": 0, "item": item}),
                    ("response.completed", {"response": response}),
                ]
                raw = b"".join(
                    (
                        f"event: {kind}\ndata: "
                        + json.dumps({"type": kind, "sequence_number": seq, **data})
                        + "\n\n"
                    ).encode()
                    for seq, (kind, data) in enumerate(events)
                )
                self._stream(raw)

        return Handler


def _docker(*args: str, timeout: int = 60) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class _Sandbox:
    """One retained-style container from the shipped image, member 0's view of it."""

    def __init__(self, image: str, trace: pathlib.Path) -> None:
        self.trace = trace
        self.container = _docker(
            "run",
            "-d",
            "--label",
            "io.hybridinference.smoke.run=steer-" + uuid.uuid4().hex[:8],
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            image,
            "sleep",
            "600",
        )
        member = ("exec", "--user", "11001:10001", self.container)
        _docker(
            *member,
            "install",
            "-d",
            "-m",
            "0700",
            "/agent-state/steer-test",
            "/home/agent-member-0/activation",
        )
        _docker(*member, "git", "init", "/workspace")
        _docker(
            *member,
            "git",
            "-C",
            "/workspace",
            "-c",
            "safe.directory=/workspace",
            "-c",
            "user.name=Steer test",
            "-c",
            "user.email=steer@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        )

    def close(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=60)

    def context(self, model: _Model) -> LaunchContext:
        host = os.environ.get("STEER_TEST_HOST", "host.docker.internal")
        trace = self.trace
        container = self.container

        class TracedProcess(_PopenProcess):
            def lines(self):
                for line in super().lines():
                    with trace.open("a") as output:
                        output.write(line)
                    yield line

            def write_line(self, text: str) -> None:
                with trace.open("a") as output:
                    output.write("SEND " + text + "\n")
                super().write_line(text)

        def spawn(argv: list[str], env: dict[str, str]) -> _PopenProcess:
            command = ["docker", "exec", "-i", "--user", "11001:10001", "--workdir", "/workspace"]
            env = {
                **env,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "/workspace",
            }
            child_env = {**os.environ, **env, "HOME": "/home/agent-member-0"}
            for key in [*env, "HOME"]:
                command.extend(["--env", key])
            process = subprocess.Popen(
                [*command, container, *argv],
                env=child_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            return TracedProcess(process, container=container)

        return LaunchContext(
            workdir="/workspace",
            gateway_base_url=f"http://{host}:{model.port}",
            credential="test-model-key",
            provides_isolation=True,
            state_dir="/agent-state/steer-test",
            session_state_dir="/agent-state/steer-test",
            activation_dir="/home/agent-member-0/activation",
            spawn_live=spawn,
        )


class _Stream:
    """The sandbox manager's side of a turn: one thread reading its events to the end."""

    def __init__(self, turn: Any) -> None:
        self.turn = turn
        self.kinds: list[str] = []
        self.taken: list[dict[str, Any]] = []
        self.saw_tool = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        for event in self.turn.iter_events():
            self.kinds.append(event.event_type)
            if event.event_type == "tool_use":
                self.saw_tool.set()
            if event.event_type == USER_MESSAGE_EVENT:
                self.taken.append(dict(event.payload))

    def join(self) -> None:
        self._thread.join(_WAIT_S)
        assert not self._thread.is_alive(), "the turn did not end"


@pytest.fixture
def sandbox(tmp_path):
    box = _Sandbox(_image(), tmp_path / "trace.log")
    try:
        yield box
    finally:
        box.close()


def _carries(request: dict[str, Any], marker: str) -> bool:
    return marker in json.dumps(request)


def _sent(sandbox: Any, text: str) -> bool:
    """Whether ``text`` was ever written to the CLI."""
    return any(
        line.startswith("SEND ") and text in line for line in sandbox.trace.read_text().splitlines()
    )


def test_claude_folds_a_message_sent_while_a_tool_runs_into_the_same_turn(sandbox):
    model = _Model("tool")
    try:
        session = get_driver("claude-code").launch_live(sandbox.context(model))
        turn = session.start_turn(TurnInput(prompt="run the slow check", model="test-model"))
        stream = _Stream(turn)
        assert stream.saw_tool.wait(_WAIT_S), "the model's tool call never reached the turn"
        marker = f"STEER-MARKER-{uuid.uuid4().hex[:8]}"

        outcome = turn.inject(f"also check the docs {marker}", message_id="apmsg_1")

        assert outcome == InputOutcome(True)
        stream.join()
        result = turn.wait()
        assert result.status == "completed", result
        # A turn that became two would have asked the model a third time.
        time.sleep(5)
        assert len(model.requests) == 2, [_carries(r, marker) for r in model.requests]
        assert not _carries(model.requests[0], marker)
        assert _carries(model.requests[1], marker)
        assert stream.kinds.count("turn_completed") + stream.kinds.count("turn_failed") == 1
        # The stream says the turn took it, before the answer that read it.
        assert stream.taken == [
            {"text": f"also check the docs {marker}", "pending_message_id": "apmsg_1"}
        ]
        assert stream.kinds.index(USER_MESSAGE_EVENT) < stream.kinds.index("message")
        session.close()
    finally:
        model.close()


def test_claude_holds_a_message_until_the_model_calls_a_tool(sandbox):
    model = _Model("hold_tool")
    try:
        session = get_driver("claude-code").launch_live(sandbox.context(model))
        turn = session.start_turn(TurnInput(prompt="take a look", model="test-model"))
        stream = _Stream(turn)
        assert model.first_arrived.wait(_WAIT_S)
        marker = f"STEER-MARKER-{uuid.uuid4().hex[:8]}"

        # The model is still thinking: the message is kept, not written yet.
        assert turn.inject(f"also check the docs {marker}", message_id="apmsg_1") == (
            InputOutcome(True)
        )
        assert not _sent(sandbox, marker)
        model.release.set()

        stream.join()
        assert turn.wait().status == "completed"
        time.sleep(5)
        # Written once the model called its tool, and read before its next step.
        assert len(model.requests) == 2
        assert not _carries(model.requests[0], marker)
        assert _carries(model.requests[1], marker)
        assert [taken["pending_message_id"] for taken in stream.taken] == ["apmsg_1"]
        assert stream.kinds.count("turn_completed") == 1
        session.close()
    finally:
        model.release.set()
        model.close()


def test_claude_never_gives_a_message_to_a_turn_that_answers_without_a_tool(sandbox):
    model = _Model("hold")
    try:
        session = get_driver("claude-code").launch_live(sandbox.context(model))
        turn = session.start_turn(TurnInput(prompt="just answer", model="test-model"))
        stream = _Stream(turn)
        assert model.first_arrived.wait(_WAIT_S)
        marker = f"STEER-MARKER-{uuid.uuid4().hex[:8]}"

        outcome = turn.inject(f"this belongs to the next turn {marker}", message_id="apmsg_1")

        assert outcome == InputOutcome(True)
        model.release.set()
        stream.join()
        assert turn.wait().status == "completed"
        time.sleep(5)
        # Never written, so no turn of its own followed the answer.
        assert not _sent(sandbox, marker)
        assert len(model.requests) == 1
        assert stream.taken == []
        session.close()
    finally:
        model.release.set()
        model.close()


def test_a_message_claude_read_too_late_leaves_the_next_turn_clean(sandbox):
    model = _Model("hold")
    try:
        session = get_driver("claude-code").launch_live(sandbox.context(model))
        turn = session.start_turn(TurnInput(prompt="just answer", model="test-model"))
        stream = _Stream(turn)
        assert model.first_arrived.wait(_WAIT_S)
        marker = f"STEER-MARKER-{uuid.uuid4().hex[:8]}"
        # As if a tool had just ended when it was written: the CLI will not
        # read it until after its answer, and would answer it on its own.
        turn._written.append(PendingInput("apmsg_1", f"too late {marker}"))
        session.send_to_turn(f"too late {marker}")
        model.release.set()

        stream.join()
        first = turn.wait()
        assert first.status == "completed"
        assert first.final_output == "reply-1"
        assert stream.taken == []
        assert "interrupt" in sandbox.trace.read_text()

        # The next turn reads its own answer, not the one it would have inherited.
        second = session.start_turn(TurnInput(prompt="and now this", model="test-model"))
        answered = second.wait()
        assert answered.status == "completed", answered
        assert answered.final_output == f"reply-{len(model.requests)}"
        assert _carries(model.requests[-1], "and now this")
        session.close()
    finally:
        model.release.set()
        model.close()


def test_codex_steers_the_running_turn_into_its_next_request(sandbox):
    model = _Model("hold")
    try:
        session = get_driver("codex").launch_live(sandbox.context(model))
        turn = session.start_turn(TurnInput(prompt="take your time", model="test-model"))
        stream = _Stream(turn)
        assert model.first_arrived.wait(_WAIT_S), "the turn never asked the model"
        marker = f"STEER-MARKER-{uuid.uuid4().hex[:8]}"

        outcome = turn.inject(f"also check the docs {marker}", message_id="apmsg_1")

        assert outcome == InputOutcome(True)
        model.release.set()
        stream.join()
        assert turn.wait().status == "completed"
        assert [taken["pending_message_id"] for taken in stream.taken] == ["apmsg_1"]
        carrying = [request for request in model.requests if _carries(request, marker)]
        assert carrying, "no request carried the steered message"
        assert not _carries(model.requests[0], marker)
        turn_ids = {
            (request.get("client_metadata") or {}).get("turn_id")
            for request in (model.requests[0], carrying[0])
        }
        # One turn: the steered text went into the same turn's next request.
        assert len(turn_ids) == 1 and None not in turn_ids, turn_ids
        session.close()
    finally:
        model.release.set()
        model.close()
