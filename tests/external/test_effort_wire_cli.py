"""A chosen reasoning level reaches the outgoing request, with the pinned CLIs.

Claude Code 2.1.220 and Codex 0.145.0, launched by the production adapters
(``prepare_live``) as the resident drivers run them, against local fakes:

- **direct**: Claude Code on an Anthropic Messages endpoint puts the level in
  ``output_config.effort``; Codex on a Responses endpoint in
  ``reasoning.effort``. At Auto, Codex sends none and Claude Code sends its
  own default (``high``) -- the platform adds nothing either way;
- **translated**: behind the member's compatibility helper (the zipapp the
  host installs) on a Chat Completions upstream, every request carries
  ``reasoning_effort`` with the turn's level, and none at Auto, whatever the
  CLI's own effort field said.

The levels for the screenshot's pairing -- Claude Code on a DeepSeek-like
model served by a HybridInference gateway -- are not chosen here: they are
what Cloud Agent derives from the gateway's own listing
(``tests/fixtures/gateway_model_effort_listing``), and each is sent.

Fakes only: no real provider, no stored key, no deployment. Set
``CLOUD_AGENT_PINNED_CLI_DIR`` to an npm prefix holding the two pins.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from tests.external.test_model_protocol_compat_cli import (
    _KEY,
    _MODEL,
    _UPSTREAM,
    _codex_send,
    _codex_thread,
    _launch,
    _Lines,
    _pinned,
)
from tests.support.compat_cli import FakeChatUpstream

from cloud_agent.domain.model_connections import model_effort_offer
from cloud_agent.domain.model_efforts import ListingRead, effort_facts
from cloud_agent_host.runtimes import (
    ClaudeCodeRuntime,
    CodexRuntime,
    ExactBaseUrl,
    MessagesBaseUrl,
    MessagesViaChatUrl,
    ResponsesViaChatUrl,
    compat_effort_env,
)
from cloud_agent_host.runtimes.codex import NATIVE_LAUNCHER
from cloud_agent_shared.model_connections import PROTOCOLS

pytestmark = pytest.mark.external

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "gateway_model_effort_listing"
_DEEPSEEK_LIKE = "deepseek-like-reasoner"
_TIMEOUT_S = 120.0


class _Capture:
    """A provider that records every request and refuses it, so the CLI stops at once."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                del args

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                capture.requests.append({"path": self.path, "body": body})
                data = json.dumps(
                    {
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "captured"},
                    }
                ).encode()
                self.send_response(400)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["path"].split("?")[0] == path and r["body"]]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _record(name: str, requests: list[dict[str, Any]]) -> None:
    directory = os.environ.get("CLOUD_AGENT_EFFORT_EVIDENCE_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / f"{name}.json").write_text(json.dumps(requests, indent=1))


def _gateway_offer(runtime: str, protocols: list[str]) -> list[str]:
    """What Cloud Agent offers ``runtime`` for the gateway's DeepSeek-like model."""
    facts = effort_facts(
        [_DEEPSEEK_LIKE],
        anthropic=ListingRead(body=json.loads((_FIXTURES / "anthropic.json").read_text())),
        openai=ListingRead(body=json.loads((_FIXTURES / "openai.json").read_text())),
    )[_DEEPSEEK_LIKE]
    row = {
        "enabled": True,
        "deleted_at": None,
        "models": [_DEEPSEEK_LIKE],
        "config_revision": 1,
        "model_checks": {
            _DEEPSEEK_LIKE: {
                "revision": 1,
                "protocols": {
                    p: {"status": "supported" if p in protocols else "unsupported"}
                    for p in PROTOCOLS
                },
                "efforts": facts,
            }
        },
    }
    offer = model_effort_offer(row, _DEEPSEEK_LIKE, runtime)
    assert offer is not None and offer.status == "supported", offer
    return offer.levels


def _run(argv: list[str], env: dict[str, str], tmp_path: Path) -> subprocess.Popen[str]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(workdir)], check=True)
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workdir,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), **env},
    )


# -- Claude Code -----------------------------------------------------------------


def _claude_direct(tmp_path: Path, effort: str | None, model: str) -> list[dict[str, Any]]:
    binary = _pinned("claude")
    capture = _Capture()
    config = tmp_path / "home" / ".claude"
    config.mkdir(parents=True)
    runtime = ClaudeCodeRuntime()
    argv, env = runtime.prepare_live(
        workdir=str(tmp_path / "work"),
        model=model,
        model_base_url=MessagesBaseUrl(capture.origin),
        credential=_KEY,
        reasoning_effort=effort,
        config_dir=str(config),
    )
    process = _run([binary, *argv[1:]], env, tmp_path)
    try:
        lines = _Lines(process)
        assert process.stdin is not None
        process.stdin.write(runtime.user_message_line("hello") + "\n")
        process.stdin.flush()
        lines.until(lambda v: v.get("type") == "result", timeout=_TIMEOUT_S)
    finally:
        process.kill()
        process.wait(timeout=30)
        capture.close()
    bodies = capture.bodies("/v1/messages")
    assert bodies, capture.requests
    _record(f"claude-direct-{model}-{effort or 'auto'}", capture.requests)
    return bodies


@pytest.mark.parametrize("effort", ["low", "max", None])
def test_pinned_claude_code_direct_sends_the_level_as_output_config(tmp_path, effort):
    bodies = _claude_direct(tmp_path, effort, _MODEL)
    sent = {body.get("output_config", {}).get("effort") for body in bodies}
    # Auto adds no flag; the CLI's own default goes out, as it always did.
    assert sent == {effort or "high"}


def test_the_screenshots_pairing_sends_every_level_the_gateway_declares(tmp_path):
    """Claude Code, a DeepSeek-like model on a gateway's Messages endpoint."""
    levels = _gateway_offer("claude-code", ["anthropic_messages", "chat_completions"])
    assert levels == ["high", "max"]
    for level in levels:
        bodies = _claude_direct(tmp_path / level, level, _DEEPSEEK_LIKE)
        assert {body["output_config"]["effort"] for body in bodies} == {level}
        assert {body["model"] for body in bodies} == {_DEEPSEEK_LIKE}


def _claude_translated(tmp_path: Path, effort: str | None) -> list[dict[str, Any]]:
    binary = _pinned("claude")
    upstream = FakeChatUpstream(_KEY)
    home = tmp_path / "home"
    config = home / ".claude"
    config.mkdir(parents=True)
    runtime = ClaudeCodeRuntime()
    # As the resident driver builds it: a template at Auto with no model,
    # then the model slot, the level beside it and the helper told the level.
    argv, env = runtime.prepare_live(
        workdir=str(tmp_path / "work"),
        model="",
        model_base_url=MessagesViaChatUrl(_UPSTREAM),
        credential=_KEY,
        config_dir=str(config),
    )
    slot = argv.index("--model") + 1
    argv[slot] = _MODEL
    argv[slot + 1 : slot + 1] = runtime.effort_args(effort)
    env.update(
        ANTHROPIC_MODEL=_MODEL,
        ANTHROPIC_SMALL_FAST_MODEL=_MODEL,
        CLAUDE_CODE_SUBAGENT_MODEL=_MODEL,
    )
    env = compat_effort_env(env, effort)
    process = _launch(argv, env, binary, tmp_path, upstream.port, home)
    try:
        lines = _Lines(process)
        assert process.stdin is not None
        process.stdin.write(runtime.user_message_line("Write the proof file.") + "\n")
        process.stdin.flush()
        result = lines.until(lambda v: v.get("type") == "result", timeout=_TIMEOUT_S)
        assert result.get("subtype") == "success", result
    finally:
        process.kill()
        process.wait(timeout=30)
        upstream.close()
    _record(f"claude-translated-{effort or 'auto'}", upstream.requests)
    requests = upstream.chat_requests()
    assert requests
    return [request["body"] for request in requests]


@pytest.mark.parametrize("effort", ["high", None])
def test_pinned_claude_code_translated_sends_the_level_on_every_chat_request(tmp_path, effort):
    bodies = _claude_translated(tmp_path, effort)
    if effort is None:
        # The CLI still sent ``output_config.effort: high``; it is not forwarded.
        assert all("reasoning_effort" not in body for body in bodies)
    else:
        assert {body.get("reasoning_effort") for body in bodies} == {effort}
    assert all("output_config" not in body for body in bodies)


# -- Codex -------------------------------------------------------------------------


def _codex_turn(process: subprocess.Popen[str], lines: _Lines, tmp_path: Path, effort) -> dict:
    thread = _codex_thread(process, lines, tmp_path)
    _codex_send(
        process,
        {
            "id": 3,
            "method": "turn/start",
            "params": {
                "threadId": thread,
                "input": [{"type": "text", "text": "Write the proof file."}],
                "model": _MODEL,
                # As ``CodexAppServerSession._start_protocol_turn`` sends it.
                "effort": effort,
            },
        },
    )
    return lines.until(lambda v: v.get("method") == "turn/completed", timeout=_TIMEOUT_S)


@pytest.mark.parametrize("effort", ["low", "xhigh", None])
def test_pinned_codex_direct_sends_the_level_as_reasoning_effort(tmp_path, effort):
    binary = _pinned("codex")
    capture = _Capture()
    codex_home = tmp_path / "home" / ".codex"
    codex_home.mkdir(parents=True)
    argv, env = CodexRuntime().prepare_live(
        workdir=str(tmp_path / "work"),
        model=_MODEL,
        model_base_url=ExactBaseUrl(capture.origin + "/v1"),
        credential=_KEY,
        reasoning_effort=effort,
        session_state_dir=str(codex_home),
    )
    assert argv[: len(NATIVE_LAUNCHER)] == list(NATIVE_LAUNCHER)
    process = _run([binary, *argv[len(NATIVE_LAUNCHER) + 1 :]], env, tmp_path)
    try:
        _codex_turn(process, _Lines(process), tmp_path, effort)
    finally:
        process.kill()
        process.wait(timeout=30)
        capture.close()
    _record(f"codex-direct-{effort or 'auto'}", capture.requests)
    bodies = capture.bodies("/v1/responses")
    assert bodies, capture.requests
    assert {(body.get("reasoning") or {}).get("effort") for body in bodies} == {effort}


@pytest.mark.parametrize("effort", ["medium", None])
def test_pinned_codex_translated_sends_the_level_on_every_chat_request(tmp_path, effort):
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
        reasoning_effort=effort,
        session_state_dir=str(codex_home),
    )
    process = _launch(argv, env, binary, tmp_path, upstream.port, home)
    try:
        completed = _codex_turn(process, _Lines(process), tmp_path, effort)
        assert completed["params"]["turn"]["status"] == "completed", completed
    finally:
        process.kill()
        process.wait(timeout=30)
        upstream.close()
    _record(f"codex-translated-{effort or 'auto'}", upstream.requests)
    bodies = [request["body"] for request in upstream.chat_requests()]
    assert bodies
    if effort is None:
        assert all("reasoning_effort" not in body for body in bodies)
    else:
        assert {body.get("reasoning_effort") for body in bodies} == {effort}
