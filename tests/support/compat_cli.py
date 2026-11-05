"""A fake Chat Completions provider, and the shipped compatibility helper pointed at it.

Shared by the protocol compatibility tests that run the real pinned CLIs.
Nothing here imitates our own code: the helper is the zipapp the host installs
(``compat_helper.helper_archive``), run by a test-only wrapper that replaces
its one connection factory with a plain-HTTP connection to this fake. The
production launch path, which always dials HTTPS, has no such seam to set.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

PROOF_TEXT = "translation-proof-7f3a"
FINAL_TEXT = "The tool wrote the proof file."

#: Run as ``python3 -I wrapper.py <zipapp> <port> <CLI argv...>``.
WRAPPER = """\
import http.client, sys
zipapp, port = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, zipapp)
import cloud_agent_host.protocol_translator as server

def fake_upstream(host, _port, timeout=None, context=None):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)

server.UPSTREAM_CONNECTION = fake_upstream
sys.exit(server.main(sys.argv[3:]))
"""


def write_wrapper(directory: Path) -> Path:
    """The test-only launcher wrapper, written beside the zipapp."""
    path = directory / "compat_wrapper.py"
    path.write_text(WRAPPER)
    return path


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-fixture",
        "object": "chat.completion.chunk",
        "model": "fixture-chat-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _usage() -> bytes:
    payload = {
        "id": "chatcmpl-fixture",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


#: A namespaced Codex tool the fake can call instead, by its flat Chat name.
NAMESPACED_TOOL = "multi_agent_v1__close_agent"


def _tool_call(
    tools: list[dict[str, Any]], prefer: str | None = None
) -> tuple[str, dict[str, Any]] | None:
    names = {(tool.get("function") or {}).get("name") for tool in tools}
    if prefer is not None and prefer in names:
        return prefer, {"target": "agent-that-does-not-exist"}
    command = f"printf {PROOF_TEXT} > proof.txt && cat proof.txt"
    if "exec_command" in names:
        return "exec_command", {"cmd": command}
    if "Bash" in names:
        return "Bash", {"command": command, "description": "Write the proof file"}
    return None


class FakeChatUpstream:
    """Just enough Chat Completions to run one tool and answer with its result."""

    def __init__(self, key: str, *, prefer_tool: str | None = None) -> None:
        """Serve on a loopback port; ``key`` is the bearer it expects."""
        self.key = key
        self.prefer_tool = prefer_tool
        self.requests: list[dict[str, Any]] = []
        self.unauthorized = 0
        self._lock = threading.Lock()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                del args

            def do_POST(self) -> None:  # name fixed by BaseHTTPRequestHandler
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
                with upstream._lock:
                    upstream.requests.append(
                        {
                            "path": self.path,
                            "authorization": self.headers.get("authorization"),
                            "body": body,
                        }
                    )
                if self.headers.get("authorization") != f"Bearer {upstream.key}":
                    upstream.unauthorized += 1
                    self.send_response(401)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                upstream._answer(self, body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()

    def _answer(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        messages = body.get("messages") or []
        frames: list[bytes]
        call = _tool_call(body.get("tools") or [], self.prefer_tool)
        if messages and messages[-1].get("role") == "tool":
            frames = [
                _chunk({"role": "assistant", "content": ""}),
                _chunk({"content": FINAL_TEXT}),
                _chunk({}, "stop"),
            ]
        elif call is not None:
            name, arguments = call
            frames = [
                _chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call_fixture_{len(self.requests)}",
                                "type": "function",
                                "function": {"name": name, "arguments": ""},
                            }
                        ],
                    }
                ),
                _chunk(
                    {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(arguments)}}]}
                ),
                _chunk({}, "tool_calls"),
            ]
        else:
            frames = [_chunk({"role": "assistant", "content": "ok"}), _chunk({}, "stop")]
        if not body.get("stream"):
            self._answer_json(handler, frames)
            return
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.end_headers()
        for frame in [*frames, _usage(), b"data: [DONE]\n\n"]:
            handler.wfile.write(frame)
            handler.wfile.flush()
        # Keep the connection open after [DONE]: the helper must not wait for EOF.
        handler.close_connection = True

    def _answer_json(self, handler: BaseHTTPRequestHandler, frames: list[bytes]) -> None:
        text = "".join(
            (json.loads(frame[6:])["choices"][0]["delta"].get("content") or "")
            for frame in frames
            if frame.startswith(b"data: {")
        )
        data = json.dumps(
            {
                "id": "chatcmpl-fixture",
                "object": "chat.completion",
                "model": "fixture-chat-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text or "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            }
        ).encode()
        handler.send_response(200)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def chat_requests(self) -> list[dict[str, Any]]:
        """Every authorized request at the Chat Completions path."""
        with self._lock:
            return [
                request
                for request in self.requests
                if request["path"].endswith("/chat/completions")
                and request["authorization"] == f"Bearer {self.key}"
            ]
