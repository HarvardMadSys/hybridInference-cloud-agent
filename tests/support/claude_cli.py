"""Drive a real, versioned claude-code CLI against a fake Anthropic gateway.

Shared by the two suites that measure the CLI rather than believe it:
``test_credential_rotation`` (does in-process key rotation work?) and
``test_runtime_stream_conformance`` (does our parser handle this version?).
Both need the same two pieces — an endpoint that settles a turn, and a headless
CLI process whose stream-json output can be read line by line — and the second
suite needs them at an *arbitrary* version, which is why the version is an
argument here rather than the pin it used to be.

Nothing in here is a mock of our own code. The gateway is the smallest thing
that satisfies the Anthropic Messages SSE surface; everything else is the real
binary, fetched by ``npx`` at the exact version asked for.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pathlib

#: A turn against the fake gateway settles in about a second; this is the
#: allowance for `npx` fetching a version that is not in the local cache yet.
TURN_TIMEOUT_S = 180.0


def sse(event: str, data: dict[str, Any]) -> bytes:
    """One server-sent event in the Messages streaming format."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class FakeAnthropicGateway:
    """Just enough of the Anthropic Messages SSE surface to settle a turn."""

    def __init__(self) -> None:
        """Bind a loopback port and start serving in a daemon thread."""
        self.keys: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.reply_text = "ok"
        self.response_status = 200
        self.response_statuses: deque[int] = deque()
        self._lock = threading.Lock()
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            # Name fixed by BaseHTTPRequestHandler, not by us.
            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                key = self.headers.get("x-api-key") or self.headers.get("authorization") or ""
                with recorder._lock:
                    recorder.keys.append(key.strip())
                    recorder.requests.append(request)
                    message_id = f"msg_{len(recorder.requests)}"
                    reply_text = recorder.reply_text
                    status = (
                        recorder.response_statuses.popleft()
                        if recorder.response_statuses
                        else recorder.response_status
                    )
                if status != 200:
                    failure = json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "authentication_error",
                                "message": "local test injected inference failure",
                            },
                        }
                    ).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(failure)))
                    self.end_headers()
                    self.wfile.write(failure)
                    return
                body = b"".join(
                    [
                        sse(
                            "message_start",
                            {
                                "type": "message_start",
                                "message": {
                                    "id": message_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                    "model": "fake-model",
                                    "stop_reason": None,
                                    "stop_sequence": None,
                                    "usage": {"input_tokens": 1, "output_tokens": 1},
                                },
                            },
                        ),
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
                                "delta": {"type": "text_delta", "text": reply_text},
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
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop serving."""
        self._server.shutdown()


class ClaudeCli:
    """One claude-code process, at a named version, in headless stream-json mode."""

    def __init__(
        self,
        *,
        version: str,
        workdir: pathlib.Path,
        config_dir: pathlib.Path,
        gateway_port: int,
        credential: str | None,
        model: str = "fake-model",
        resume_session_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        """Start the CLI. ``version`` is exact — this fetches that build."""
        self.version = version
        #: Every stream-json line this process emitted, parsed. The
        #: conformance suite reads the shapes; the rotation suite only needs
        #: the result, which ``turn`` returns.
        self.transcript: list[dict[str, Any]] = []
        argv = [
            "npx",
            "--yes",
            f"@anthropic-ai/claude-code@{version}",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--strict-mcp-config",
            "--permission-mode",
            "bypassPermissions",
            "--mcp-config",
            json.dumps({"mcpServers": {}}),
        ]
        if resume_session_id:
            argv[4:4] = ["--resume", resume_session_id]
        env = {
            **os.environ,
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{gateway_port}",
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            **(extra_env or {}),
        }
        env.pop("ANTHROPIC_API_KEY", None)
        if credential is not None:
            env["ANTHROPIC_API_KEY"] = credential
        self.process = subprocess.Popen(
            argv,
            cwd=workdir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def turn(self, text: str) -> dict[str, Any] | None:
        """Send one user message; return the result event, or None on death.

        Every line seen on the way is appended to ``transcript`` first, raw and
        in order, because that ordering is itself part of what the conformance
        suite checks.
        """
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                }
            )
            + "\n"
        )
        self.process.stdin.flush()
        deadline = time.time() + TURN_TIMEOUT_S
        while time.time() < deadline:
            try:
                line = self._lines.get(timeout=1.0)
            except queue.Empty:
                if self.process.poll() is not None:
                    return None
                continue
            if line is None:
                return None
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.transcript.append(event)
            if event.get("type") == "result":
                return event
        return None

    def close(self) -> None:
        """Kill the process and reap it."""
        self.process.kill()
        self.process.wait()
