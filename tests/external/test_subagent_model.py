"""The pinned Claude CLI keeps delegated work inside a one-model grant.

This is a real-wire test because the bug lives in Claude Code's own model
selection, after our runtime has launched it.  The fake gateway forces one
Agent call and records both the parent and child request models.

Run by hand with ``pytest -m external tests/external/test_subagent_model.py``
after a Claude Code version bump.
"""

from __future__ import annotations

import json
import os
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


def _pinned_cli_version() -> str:
    dockerfile = (_REPO_ROOT / "deploy/sandbox/Dockerfile.agent-sandbox").read_text()
    match = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert match, "the sandbox Dockerfile no longer pins CLAUDE_CODE_VERSION"
    return match.group(1)


def _message_start(message_id: str, model: str) -> bytes:
    return sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    )


def _answer(message_id: str, text: str, model: str) -> bytes:
    return b"".join(
        [
            _message_start(message_id, model),
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


def _delegate(model: str, tool_model: str | None) -> bytes:
    return b"".join(
        [
            _message_start("msg_parent_delegate", model),
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
                                "run_in_background": False,
                                **({"model": tool_model} if tool_model else {}),
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

    def __init__(self, model: str, tool_model: str | None = None) -> None:
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
                        response = _delegate(model, tool_model)
                    elif request_no == 2:
                        response = _answer("msg_child", "inspected", model)
                    else:
                        response = _answer("msg_parent_done", "done", model)
                # Enforce the same one-model boundary as an inference grant.
                if request.get("model") != model:
                    response = json.dumps(
                        {
                            "type": "error",
                            "error": {"type": "permission_error", "message": "model not granted"},
                        }
                    ).encode()
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return
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


@pytest.mark.parametrize(
    "model", ["deepseek-v4-flash", "glm-5.1", "claude-sonnet-private-deployment"]
)
@pytest.mark.parametrize("setting", ["unset", "inherit", "runtime"])
def test_subagent_model_against_a_one_model_grant(tmp_path, monkeypatch, model, setting):
    """The non-Claude controls must reproduce a rejected delegated request."""
    _run_delegation(tmp_path, monkeypatch, model, setting)


def test_concrete_override_also_beats_the_agent_tool_model(tmp_path, monkeypatch):
    _run_delegation(tmp_path, monkeypatch, "deepseek-v4-flash", "runtime", tool_model="opus")


def _run_delegation(tmp_path, monkeypatch, model, setting, tool_model=None):
    for key in tuple(os.environ):
        if key.startswith("ANTHROPIC_DEFAULT_") or key == "CLAUDE_CODE_SUBAGENT_MODEL":
            monkeypatch.delenv(key)
    gateway = _SubagentGateway(model, tool_model)
    workdir = tmp_path / "work"
    config = tmp_path / "config"
    workdir.mkdir()
    config.mkdir()
    _argv, runtime_env = ClaudeCodeRuntime().prepare(
        workdir=str(workdir),
        task_prompt="delegate once",
        model=model,
        gateway_base_url=f"http://127.0.0.1:{gateway.port}",
        credential="one-model-grant",
    )
    if setting == "unset":
        runtime_env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
    elif setting == "inherit":
        runtime_env["CLAUDE_CODE_SUBAGENT_MODEL"] = "inherit"
    cli = ClaudeCli(
        version=_pinned_cli_version(),
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="one-model-grant",
        model=model,
        extra_env=runtime_env,
    )
    try:
        result = cli.turn("delegate this inspection")
    finally:
        cli.close()
        gateway.close()

    assert result is not None
    assert len(gateway.requests) >= 3, "Claude Code did not launch and rejoin the subagent"
    request_models = {request["model"] for request in gateway.requests}
    returned = [
        block
        for request in gateway.requests[2:]
        for message in request.get("messages", [])
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result" and block.get("tool_use_id") == "toolu_delegate"
    ]
    assert returned, "the parent never received the delegated result"
    if setting == "runtime" or model.startswith("claude-"):
        assert request_models == {model}
        assert "inspected" in json.dumps(returned)
        assert not result.get("is_error")
    else:
        assert request_models - {model}, "the baseline did not reproduce an ungranted model"
        assert "model not granted" in json.dumps(returned)
        assert "inspected" not in json.dumps(returned)
