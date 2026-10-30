"""The pinned Codex and Claude Code CLIs, run on a Chat Completions provider through the helper.

What this proves, with the real binaries at the project's pins (Codex 0.145.0
in ``app-server`` mode as production drives it, Claude Code 2.1.220 in
resident stream-json mode):

- the argv and environment come from the production adapters
  (``prepare_live`` with a translated URL), and the helper is the zipapp the
  host installs, so the launcher really forks the service and execs the CLI;
- the provider receives only Chat Completions requests, with the provider key;
- the model calls a real tool (Codex ``exec_command``, Claude Code ``Bash``),
  the tool runs, and its output reaches the next Chat request as a ``tool``
  message, after which the CLI finishes its turn normally;
- the CLI's environment holds the member's token, never the provider key, and
  a token derived for another member is refused;
- when the CLI exits, its service is gone.

The provider is a local fake, keys are fixtures, CLI homes are temporary.
This is not a real-provider or Kata acceptance. Set ``CLOUD_AGENT_PINNED_CLI_DIR``
to an npm prefix holding ``@openai/codex@0.145.0`` and
``@anthropic-ai/claude-code@2.1.220``.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from tests.support.compat_cli import (
    FINAL_TEXT,
    NAMESPACED_TOOL,
    PROOF_TEXT,
    FakeChatUpstream,
    write_wrapper,
)

from cloud_agent.application.model_connections import model_connection_cipher, translator_token
from cloud_agent_host.compat_helper import helper_archive
from cloud_agent_host.runtimes import (
    ClaudeCodeRuntime,
    CodexRuntime,
    MessagesViaChatUrl,
    ResponsesViaChatUrl,
)
from cloud_agent_host.runtimes.base import COMPAT_LAUNCHER
from cloud_agent_shared.protocol_compat import ENV_TOKEN

pytestmark = pytest.mark.external

_KEY = "fixture-upstream-key-not-a-credential"
_MODEL = "fixture-chat-model"
_UPSTREAM = "https://chat-upstream.invalid/v1"
_MEMBER = "athr_compat_member"
#: A fixture platform key: the control plane's own derivation issues the tokens.
_CIPHER = model_connection_cipher("fixture-platform-secret-not-a-credential")


def _token(thread_id: str) -> str:
    return translator_token(
        _CIPHER,
        user_id="user-fixture",
        thread_id=thread_id,
        source="mconn_" + "f" * 32 + "@1",
        client_protocol="responses",
        model=_MODEL,
    )


_TIMEOUT_S = 180.0


def _pinned(name: str) -> str:
    root = os.environ.get("CLOUD_AGENT_PINNED_CLI_DIR")
    if not root:
        pytest.skip("CLOUD_AGENT_PINNED_CLI_DIR names no pinned CLI prefix")
    binary = Path(root) / "node_modules" / ".bin" / name
    if not binary.exists():
        pytest.skip(f"{binary} is not installed")
    return str(binary)


class _Lines:
    """The CLI's stdout, one line at a time, with a deadline."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        assert process.stdout is not None
        stdout = process.stdout

        def pump() -> None:
            for line in stdout:
                self._queue.put(line)
            self._queue.put(None)

        threading.Thread(target=pump, daemon=True).start()

    def until(self, predicate: Any, *, timeout: float = _TIMEOUT_S) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            line = self._queue.get(timeout=max(0.1, deadline - time.monotonic()))
            if line is None:
                raise AssertionError("the CLI closed its output before the turn finished")
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and predicate(value):
                return value


def _launch(
    argv: list[str], env: dict[str, str], binary: str, tmp_path: Path, port: int, home: Path
) -> subprocess.Popen[str]:
    assert argv[: len(COMPAT_LAUNCHER)] == list(COMPAT_LAUNCHER)
    cli = [binary, *argv[len(COMPAT_LAUNCHER) + 1 :]]
    zipapp = tmp_path / "model-protocol-translator.pyz"
    zipapp.write_bytes(helper_archive())
    wrapper = write_wrapper(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(workdir)], check=True)
    full_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        **env,
        ENV_TOKEN: _token(_MEMBER),
    }
    return subprocess.Popen(
        ["/usr/bin/python3", "-I", str(wrapper), str(zipapp), str(port), *cli],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workdir,
        env=full_env,
    )


def _cli_environment(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    pairs = (entry.split(b"=", 1) for entry in raw.split(b"\0") if b"=" in entry)
    return {key.decode(): value.decode(errors="replace") for key, value in pairs}


def _post(origin: str, path: str, token: str) -> int:
    request = urllib.request.Request(
        origin + path,
        data=json.dumps({"model": _MODEL, "messages": []}).encode(),
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def _assert_member_boundary(pid: int, origin_var: str, path: str) -> str:
    """The CLI holds this member's token and not the key; another member's token fails."""
    environment = _cli_environment(pid)
    assert _KEY not in "\0".join(environment.values())
    assert not any(name.startswith("AGENT_COMPAT_") for name in environment)
    origin = environment[origin_var].removesuffix("/v1")
    assert origin.startswith("http://127.0.0.1:")
    # A sibling on the same key, its own token in hand, and one it guesses.
    assert _post(origin, path, _token("athr_other_member")) == 401
    assert _post(origin, path, _KEY) == 401
    assert _post(origin, path, "") == 401
    return origin


def _assert_service_gone(origin: str) -> None:
    host, port = origin.removeprefix("http://").split(":")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection((host, int(port)), timeout=1).close()
        except OSError:
            return
        time.sleep(0.2)
    raise AssertionError("the compatibility service outlived its CLI")


def _record(name: str, upstream: FakeChatUpstream) -> None:
    """With ``CLOUD_AGENT_COMPAT_EVIDENCE_DIR`` set, keep what the provider saw."""
    directory = os.environ.get("CLOUD_AGENT_COMPAT_EVIDENCE_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / f"{name}.json").write_text(json.dumps(upstream.requests, indent=1))


def _assert_tool_round_trip(upstream: FakeChatUpstream, workdir: Path) -> None:
    requests = upstream.chat_requests()
    assert requests, "the provider saw no Chat Completions request"
    assert upstream.unauthorized == 0
    assert all(r["path"] == "/v1/chat/completions" for r in upstream.requests)
    assert all(r["body"]["model"] == _MODEL for r in requests)
    tool_results = [
        message
        for request in requests
        for message in request["body"]["messages"]
        if message.get("role") == "tool"
    ]
    assert any(PROOF_TEXT in json.dumps(message) for message in tool_results), tool_results
    assert (workdir / "proof.txt").read_text() == PROOF_TEXT


def test_pinned_codex_app_server_runs_a_tool_on_a_chat_upstream(tmp_path: Path) -> None:
    binary = _pinned("codex")
    upstream = FakeChatUpstream(_KEY)
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    argv, env = CodexRuntime().prepare_live(
        workdir=str(tmp_path / "work"),
        model=_MODEL,
        model_base_url=ResponsesViaChatUrl(_UPSTREAM),
        credential=_KEY,
        session_state_dir=str(codex_home),
    )
    assert _KEY not in " ".join(argv)
    process = _launch(argv, env, binary, tmp_path, upstream.port, home)
    try:
        lines = _Lines(process)
        assert process.stdin is not None

        def send(value: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "translation-test",
                        "title": "Translation",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True, "requestAttestation": False},
                },
            }
        )
        lines.until(lambda v: v.get("id") == 1)
        send({"method": "initialized"})
        send(
            {
                "id": 2,
                "method": "thread/start",
                "params": {
                    "model": _MODEL,
                    "modelProvider": "hybridinference",
                    "cwd": str(tmp_path / "work"),
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                },
            }
        )
        thread = lines.until(lambda v: v.get("id") == 2)["result"]["thread"]["id"]
        origin = _assert_member_boundary(process.pid, "OPENAI_BASE_URL", "/v1/responses")
        send(
            {
                "id": 3,
                "method": "turn/start",
                "params": {
                    "threadId": thread,
                    "input": [{"type": "text", "text": "Write the proof file."}],
                    "model": _MODEL,
                },
            }
        )
        completed = lines.until(lambda v: v.get("method") == "turn/completed")
        assert completed["params"]["turn"]["status"] == "completed", completed
        _record("codex-0.145.0", upstream)
        _assert_tool_round_trip(upstream, tmp_path / "work")
        first = upstream.chat_requests()[0]["body"]
        # Namespaced function tools (sub-agents) are offered flat, not dropped.
        names = [tool["function"]["name"] for tool in first["tools"]]
        assert "exec_command" in names
        assert any(name.startswith("multi_agent_v1__") for name in names), names
    finally:
        process.kill()
        process.wait(timeout=30)
        upstream.close()
    _assert_service_gone(origin)


def test_pinned_claude_code_runs_a_tool_on_a_chat_upstream(tmp_path: Path) -> None:
    binary = _pinned("claude")
    upstream = FakeChatUpstream(_KEY)
    home = tmp_path / "home"
    config = home / ".claude"
    config.mkdir(parents=True)
    runtime = ClaudeCodeRuntime()
    argv, env = runtime.prepare_live(
        workdir=str(tmp_path / "work"),
        model="",
        model_base_url=MessagesViaChatUrl(_UPSTREAM),
        credential=_KEY,
        config_dir=str(config),
    )
    # As the resident driver pins the model: by the ``--model`` slot, behind the launcher.
    argv[argv.index("--model") + 1] = _MODEL
    env.update(
        ANTHROPIC_MODEL=_MODEL,
        ANTHROPIC_SMALL_FAST_MODEL=_MODEL,
        CLAUDE_CODE_SUBAGENT_MODEL=_MODEL,
    )
    assert _KEY not in " ".join(argv)
    process = _launch(argv, env, binary, tmp_path, upstream.port, home)
    try:
        lines = _Lines(process)
        assert process.stdin is not None
        process.stdin.write(runtime.user_message_line("Write the proof file.") + "\n")
        process.stdin.flush()
        lines.until(lambda v: v.get("type") == "system" and v.get("subtype") == "init")
        origin = _assert_member_boundary(process.pid, "ANTHROPIC_BASE_URL", "/v1/messages")
        result = lines.until(lambda v: v.get("type") == "result")
        assert result.get("subtype") == "success" and not result.get("is_error"), result
        assert FINAL_TEXT in str(result.get("result")), result
        _record("claude-code-2.1.220", upstream)
        _assert_tool_round_trip(upstream, tmp_path / "work")
    finally:
        process.kill()
        process.wait(timeout=30)
        upstream.close()
    _assert_service_gone(origin)


def test_pinned_codex_runs_a_namespaced_tool_under_its_own_name(tmp_path: Path) -> None:
    """A flat ``multi_agent_v1__close_agent`` call reaches Codex as ``close_agent`` in its namespace.

    Codex answers its own sub-agent tool -- here refusing an unknown agent id --
    rather than an unknown-tool error, and that answer goes back upstream under
    the flat name the model called.
    """
    binary = _pinned("codex")
    upstream = FakeChatUpstream(_KEY, prefer_tool=NAMESPACED_TOOL)
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    argv, env = CodexRuntime().prepare_live(
        workdir=str(tmp_path / "work"),
        model=_MODEL,
        model_base_url=ResponsesViaChatUrl(_UPSTREAM),
        credential=_KEY,
        session_state_dir=str(codex_home),
    )
    process = _launch(argv, env, binary, tmp_path, upstream.port, home)
    try:
        lines = _Lines(process)
        thread = _codex_thread(process, lines, tmp_path)
        _codex_send(
            process,
            {
                "id": 3,
                "method": "turn/start",
                "params": {
                    "threadId": thread,
                    "input": [{"type": "text", "text": "Close the agent."}],
                    "model": _MODEL,
                },
            },
        )
        completed = lines.until(lambda v: v.get("method") == "turn/completed")
        assert completed["params"]["turn"]["status"] == "completed", completed
        _record("codex-0.145.0-namespaced", upstream)
        second = upstream.chat_requests()[-1]["body"]["messages"]
        call = next(m for m in second if m.get("tool_calls"))["tool_calls"][0]
        result = next(m for m in second if m["role"] == "tool")
        # History replays under the same flat name, paired with its result.
        assert call["function"]["name"] == NAMESPACED_TOOL
        assert result["tool_call_id"] == call["id"]
        assert "unsupported call" not in result["content"].lower(), result
        assert "agent-that-does-not-exist" in result["content"] or "not found" in (
            result["content"].lower()
        ), result
    finally:
        process.kill()
        process.wait(timeout=30)
        upstream.close()


def _codex_send(process: subprocess.Popen[str], value: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(value) + "\n")
    process.stdin.flush()


def _codex_thread(process: subprocess.Popen[str], lines: _Lines, tmp_path: Path) -> str:
    """Initialize the app-server and start a thread, as the resident driver does."""
    _codex_send(
        process,
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "translation-test", "title": "Translation", "version": "1"},
                "capabilities": {"experimentalApi": True, "requestAttestation": False},
            },
        },
    )
    lines.until(lambda v: v.get("id") == 1)
    _codex_send(process, {"method": "initialized"})
    _codex_send(
        process,
        {
            "id": 2,
            "method": "thread/start",
            "params": {
                "model": _MODEL,
                "modelProvider": "hybridinference",
                "cwd": str(tmp_path / "work"),
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
            },
        },
    )
    return str(lines.until(lambda v: v.get("id") == 2)["result"]["thread"]["id"])
