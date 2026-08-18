"""Per-turn credentials against the *real*, pinned claude-code CLI.

Two contracts, both measured rather than believed (review finding 1):

1. **In-process rotation never takes effect.** The ``apiKeyHelper`` result is
   fetched once per process; ``CLAUDE_CODE_API_KEY_HELPER_TTL_MS`` (unset, 0
   and 1 alike) changes nothing, and a 401 on a revoked key produces an error
   result with no refetch and no retry. This is *why* hot turns restart the
   CLI per turn. If a CLI upgrade ever makes this test fail, in-process
   rotation has become real and the live multi-turn session can be
   reconsidered — that is the one way this guard is allowed to break.
2. **Per-turn restart carries each attempt's own grant.** Turn two runs
   ``--resume`` in a fresh process whose environment holds the new grant, and
   the fake gateway sees the two turns' requests under two different keys —
   the property the whole billing story stands on.

The pinned version is read from the sandbox Dockerfile, so this suite always
measures the CLI the image actually ships.
"""

from __future__ import annotations

import json
import pathlib
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.external

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TURN_TIMEOUT_S = 90.0


def _pinned_cli_version() -> str:
    dockerfile = (_REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox").read_text()
    match = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert match, "the sandbox Dockerfile no longer pins CLAUDE_CODE_VERSION"
    return match.group(1)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class _FakeGateway:
    """Just enough of the Anthropic Messages SSE surface to settle a turn."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self._lock = threading.Lock()
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                key = self.headers.get("x-api-key") or self.headers.get("authorization") or ""
                with recorder._lock:
                    recorder.keys.append(key.strip())
                body = b"".join(
                    [
                        _sse(
                            "message_start",
                            {
                                "type": "message_start",
                                "message": {
                                    "id": "msg_1",
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
                        _sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""},
                            },
                        ),
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": "ok"},
                            },
                        ),
                        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
                        _sse(
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": 1},
                            },
                        ),
                        _sse("message_stop", {"type": "message_stop"}),
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
        self._server.shutdown()


class _Cli:
    """One pinned-CLI process in headless stream-json mode."""

    def __init__(
        self,
        *,
        workdir: pathlib.Path,
        config_dir: pathlib.Path,
        gateway_port: int,
        credential: str | None,
        resume_session_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        argv = [
            "npx",
            "--yes",
            f"@anthropic-ai/claude-code@{_pinned_cli_version()}",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "fake-model",
            "--strict-mcp-config",
            "--permission-mode",
            "bypassPermissions",
            "--mcp-config",
            json.dumps({"mcpServers": {}}),
        ]
        if resume_session_id:
            argv[4:4] = ["--resume", resume_session_id]
        env = {
            **__import__("os").environ,
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

    def turn(self, text: str) -> dict | None:
        """Send one user message; return the result event, or None."""
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
        deadline = time.time() + _TURN_TIMEOUT_S
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
            if event.get("type") == "result":
                return event
        return None

    def close(self) -> None:
        self.process.kill()
        self.process.wait()


@pytest.fixture
def gateway():
    fake = _FakeGateway()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _needs_npx():
    if shutil.which("npx") is None:
        pytest.skip("npx is required to run the pinned CLI")


def _config_with_helper(base: pathlib.Path, credential_value: str) -> pathlib.Path:
    config = base / "claude"
    config.mkdir(parents=True, exist_ok=True)
    credential = config / "credential"
    credential.write_text(credential_value + "\n")
    helper = config / "helper.sh"
    helper.write_text(f'#!/bin/sh\ncat "{credential}"\n')
    helper.chmod(0o755)
    (config / "settings.json").write_text(json.dumps({"apiKeyHelper": str(helper)}))
    return config


def test_in_process_rotation_never_takes_effect(tmp_path, gateway):
    """The disproof this architecture stands on. Allowed to break only when a
    CLI upgrade makes in-process rotation real — then reconsider live sessions."""
    config = _config_with_helper(tmp_path, "grant-TURN-A")
    workdir = tmp_path / "ws"
    workdir.mkdir()
    cli = _Cli(workdir=workdir, config_dir=config, gateway_port=gateway.port, credential=None)
    try:
        assert cli.turn("say ok") is not None
        first_turn_requests = len(gateway.keys)
        assert set(gateway.keys) == {"grant-TURN-A"}

        (config / "credential").write_text("grant-TURN-B\n")
        assert cli.turn("say ok again") is not None
    finally:
        cli.close()

    second_turn_keys = set(gateway.keys[first_turn_requests:])
    assert second_turn_keys == {"grant-TURN-A"}, (
        "the pinned CLI re-read the helper mid-process: in-process rotation has "
        "become real — revisit the per-turn-restart decision (design §13)"
    )


def test_per_turn_restart_carries_each_attempts_own_grant(tmp_path, gateway):
    """Review finding 1's fix, asserted end to end: two turns, two processes,
    two grants — and the second turn resumes the first one's session."""
    config = tmp_path / "claude"
    config.mkdir()
    workdir = tmp_path / "ws"
    workdir.mkdir()

    first = _Cli(
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="grant-ATTEMPT-1",
    )
    try:
        result = first.turn("remember the number 41")
        assert result is not None
        session_id = result.get("session_id")
        assert session_id
    finally:
        first.close()
    boundary = len(gateway.keys)
    assert set(gateway.keys[:boundary]) == {"grant-ATTEMPT-1"}

    second = _Cli(
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="grant-ATTEMPT-2",
        resume_session_id=session_id,
    )
    try:
        assert second.turn("now add one") is not None
    finally:
        second.close()

    assert gateway.keys[boundary:], "the resumed turn made no model calls"
    assert set(gateway.keys[boundary:]) == {"grant-ATTEMPT-2"}
