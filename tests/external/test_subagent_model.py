"""The pinned Claude CLI keeps delegated work inside a one-model grant.

This is a real-wire test because the bug lives in Claude Code's own model
selection, after our runtime has launched it.  The fake gateway forces one
Agent call and records both the parent and child request models.

Run by hand with ``pytest -m external tests/external/test_subagent_model.py``
after a Claude Code version bump.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from cloud_agent_host.runtimes import ClaudeCodeRuntime
from tests.support.claude_cli import ClaudeCli, sse

pytestmark = pytest.mark.external

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GRANTED_MODEL = "claude-sonnet-private-deployment"


def _pinned_cli_version() -> str:
    dockerfile = (_REPO_ROOT / "deploy/sandbox/Dockerfile.agent-sandbox").read_text()
    match = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert match, "the sandbox Dockerfile no longer pins CLAUDE_CODE_VERSION"
    return match.group(1)


def _message_start(message_id: str) -> bytes:
    return sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": _GRANTED_MODEL,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    )


def _answer(message_id: str, text: str) -> bytes:
    return b"".join(
        [
            _message_start(message_id),
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
                    "delta": {"type": "text_delta", "text": text},
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


def _delegate() -> bytes:
    return b"".join(
        [
            _message_start("msg_parent_delegate"),
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_delegate",
                        "name": "Agent",
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
                        "partial_json": json.dumps(
                            {
                                "description": "Inspect the workspace",
                                "prompt": "Reply with the word inspected.",
                                "subagent_type": "Explore",
                            }
                        ),
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


class _SubagentGateway:
    """Delegate once, settle the child, then settle the parent."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
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
                    request_no = len(recorder.requests)
                    if request_no == 1:
                        response = _delegate()
                    elif request_no == 2:
                        response = _answer("msg_child", "inspected")
                    else:
                        response = _answer("msg_parent_done", "done")
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


def test_subagent_inherits_the_exact_parent_model(tmp_path):
    gateway = _SubagentGateway()
    workdir = tmp_path / "work"
    config = tmp_path / "config"
    workdir.mkdir()
    config.mkdir()
    _argv, runtime_env = ClaudeCodeRuntime().prepare(
        workdir=str(workdir),
        task_prompt="delegate once",
        model=_GRANTED_MODEL,
        gateway_base_url=f"http://127.0.0.1:{gateway.port}",
        credential="one-model-grant",
    )
    cli = ClaudeCli(
        version=_pinned_cli_version(),
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="one-model-grant",
        model=_GRANTED_MODEL,
        extra_env=runtime_env,
    )
    try:
        result = cli.turn("delegate this inspection")
    finally:
        cli.close()
        gateway.close()

    assert result is not None
    assert len(gateway.requests) >= 3, "Claude Code did not launch and rejoin the subagent"
    assert {request["model"] for request in gateway.requests} == {_GRANTED_MODEL}
