"""A stuck platform hook cannot hold a tool call in the pinned Claude CLI.

The helper itself has a 25 ms non-blocking stdin deadline and bounded output
paths. This test covers the independent outer fence: the exact Claude Code
version baked into the sandbox must cancel a command hook at our configured
timeout and then execute the tool. A settings-shape unit test cannot prove
that runtime behavior.

Run by hand with ``pytest -m external tests/external/test_tool_hook_passivity.py``
after a Claude Code version bump.
"""

from __future__ import annotations

import json
import pathlib
import re
import shlex
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from cloud_agent_host.tool_windows import TOOL_MARK_SANDBOX_PATH, hook_settings
from tests.support.claude_cli import ClaudeCli, sse

pytestmark = pytest.mark.external

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _pinned_cli_version() -> str:
    dockerfile = (_REPO_ROOT / "deploy/sandbox/Dockerfile.agent-sandbox").read_text()
    match = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert match, "the sandbox Dockerfile no longer pins CLAUDE_CODE_VERSION"
    return match.group(1)


def _message_start() -> bytes:
    return sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_hook_test",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "fake-model",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    )


def _tool_response() -> bytes:
    return b"".join(
        [
            _message_start(),
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_hook_timeout",
                        "name": "Bash",
                        "input": {},
                    },
                },
            ),
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"command":"printf hook-ok"}',
                    },
                },
            ),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            sse("message_stop", {"type": "message_stop"}),
        ]
    )


def _answer_response() -> bytes:
    return b"".join(
        [
            _message_start(),
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "done"},
                },
            ),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            sse("message_stop", {"type": "message_stop"}),
        ]
    )


class _ToolGateway:
    """Force one Bash call, then settle after its tool result arrives."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.request_times: list[float] = []
        self._lock = threading.Lock()
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length))
                with recorder._lock:
                    recorder.requests.append(request)
                    recorder.request_times.append(time.monotonic())
                    response = (
                        _tool_response() if len(recorder.requests) == 1 else _answer_response()
                    )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()


@pytest.fixture(autouse=True)
def _needs_npx():
    if shutil.which("npx") is None:
        pytest.skip("npx is required to run the pinned CLI")


@pytest.mark.parametrize("hook_event", ["PreToolUse", "PostToolUse"])
def test_a_timed_out_platform_hook_does_not_block_the_tool(tmp_path, hook_event):
    gateway = _ToolGateway()
    workdir = tmp_path / "work"
    config = tmp_path / "config"
    workdir.mkdir()
    config.mkdir()
    hook_started = tmp_path / "hook-started"
    settings = hook_settings(TOOL_MARK_SANDBOX_PATH)
    stuck_hook = settings["hooks"][hook_event][0]["hooks"][0]
    stuck_hook["command"] = f"printf started > {shlex.quote(str(hook_started))}; sleep 10"
    (config / "settings.json").write_text(json.dumps(settings))

    cli = ClaudeCli(
        version=_pinned_cli_version(),
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="hook-test-key",
    )
    try:
        result = cli.turn("run the requested tool")
    finally:
        cli.close()
        gateway.close()

    assert result is not None
    assert hook_started.read_text() == "started", "the deliberately stuck hook never ran"
    assert len(gateway.requests) == 2, "the timed-out hook prevented the tool result round trip"
    elapsed = gateway.request_times[1] - gateway.request_times[0]
    assert elapsed < 5, f"the one-second hook timeout delayed the tool result by {elapsed:.1f}s"
    second_messages = gateway.requests[1].get("messages") or []
    assert "hook-ok" in json.dumps(second_messages), "Bash did not execute after the hook timeout"
