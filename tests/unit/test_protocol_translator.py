"""The member's loopback service and its launcher, against a local fake upstream.

The service is run in-process with its one connection factory pointed at a
plain-HTTP fake (``UPSTREAM_CONNECTION``, settable only from Python). The
launcher is run for real, from the zipapp the host installs, in front of a
small CLI stand-in.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest
from tests.support.compat_cli import write_wrapper

from cloud_agent_host import protocol_translator as compat_server
from cloud_agent_host.compat_helper import helper_archive
from cloud_agent_host.protocol_translator import Binding, LaunchError, _Server
from cloud_agent_shared.protocol_compat import ORIGIN_PLACEHOLDER, TOKEN_PLACEHOLDER

if TYPE_CHECKING:
    from pathlib import Path

_KEY = "fixture-upstream-key-0123456789"
#: Tokens as the control plane issues them; the service derives none itself.
_TOKEN = "a" * 64
_SIBLING = "b" * 64


def _sse(*payloads: dict[str, Any], done: bool = True) -> bytes:
    body = b"".join(f"data: {json.dumps(p)}\n\n".encode() for p in payloads)
    return body + (b"data: [DONE]\n\n" if done else b"")


def _chunk(content: str | None = None, finish: str | None = None) -> dict[str, Any]:
    delta = {"content": content} if content is not None else {}
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class _Upstream:
    """A plain-HTTP Chat provider whose answer each test scripts."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.respond = lambda handler, body: self.stream(
            handler, _sse(_chunk("hi"), _chunk(None, "stop"))
        )
        self.closed_by_client = threading.Event()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                del args

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                upstream.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("authorization"),
                        "body": body,
                    }
                )
                upstream.respond(self, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def stream(handler: BaseHTTPRequestHandler, data: bytes, *, hold_open: float = 0.0) -> None:
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.end_headers()
        handler.wfile.write(data)
        handler.wfile.flush()
        time.sleep(hold_open)
        handler.close_connection = True


@pytest.fixture
def upstream(monkeypatch):
    fake = _Upstream()

    def connect(host, port, timeout=None, context=None):
        assert (host, port) == ("provider.example", 443)
        return http.client.HTTPConnection("127.0.0.1", fake.port, timeout=timeout)

    monkeypatch.setattr(compat_server, "UPSTREAM_CONNECTION", connect)
    yield fake
    fake.server.shutdown()


def _binding(client: str) -> Binding:
    env = {
        "AGENT_COMPAT_CLIENT": client,
        "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example/prefix/v1",
        "AGENT_COMPAT_UPSTREAM_KEY": _KEY,
        "AGENT_COMPAT_MODEL": "m",
        "AGENT_COMPAT_TOKEN": _TOKEN,
    }
    return Binding.from_launch(env, ["cli"])


@pytest.fixture(params=["responses", "anthropic_messages"])
def service(request, upstream):
    binding = _binding(request.param)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    server = _Server(sock, binding)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield binding, sock.getsockname()[1]
    server.shutdown()
    server.server_close()


def _path(binding: Binding) -> str:
    return "/v1/responses" if binding.client == "responses" else "/v1/messages"


def _body(binding: Binding, **changes: Any) -> dict[str, Any]:
    if binding.client == "responses":
        return {"model": "m", "input": "hi", "stream": True, **changes}
    return {
        "model": "m",
        "max_tokens": 10,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        **changes,
    }


def _call(port: int, path: str, *, token: str | None, body: Any = None, method: str = "POST"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    return response.status, response.read().decode()


def test_only_this_members_token_is_accepted(service, upstream):
    binding, port = service
    token = _TOKEN
    assert _call(port, _path(binding), token=None, body=_body(binding))[0] == 401
    sibling = _SIBLING
    assert _call(port, _path(binding), token=sibling, body=_body(binding))[0] == 401
    assert _call(port, _path(binding), token=_KEY, body=_body(binding))[0] == 401
    # No other resource: unauthenticated callers learn nothing, the member gets 404.
    assert _call(port, "/v1/models", token=None, method="GET")[0] == 401
    assert _call(port, "/v1/models", token=token, method="GET")[0] == 404
    assert _call(port, "/v1/chat/completions", token=token, body=_body(binding))[0] == 404
    assert upstream.requests == []


def test_a_request_goes_to_the_bound_upstream_with_the_key(service, upstream):
    binding, port = service
    status, text = _call(port, _path(binding), token=_TOKEN, body=_body(binding))
    assert status == 200
    (sent,) = upstream.requests
    assert sent["path"] == "/prefix/v1/chat/completions"
    assert sent["authorization"] == f"Bearer {_KEY}"
    assert sent["body"]["model"] == "m" and sent["body"]["stream"] is True
    assert ("response.completed" if binding.client == "responses" else "message_stop") in text
    assert _KEY not in text


def test_the_model_cannot_be_chosen_by_the_caller(service, upstream):
    binding, port = service
    status, text = _call(
        port,
        _path(binding),
        token=_TOKEN,
        body=_body(binding, model="expensive"),
    )
    assert status == 400 and "serves only the model" in text
    assert upstream.requests == []


def test_a_provider_error_is_answered_without_its_body(service, upstream):
    binding, port = service

    def refuse(handler, body):
        data = json.dumps({"error": {"message": f"bad key {_KEY}"}}).encode()
        handler.send_response(401)
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    upstream.respond = refuse
    status, text = _call(port, _path(binding), token=_TOKEN, body=_body(binding))
    assert status == 401 and "answered HTTP 401" in text
    assert _KEY not in text


def test_the_answer_ends_at_done_even_if_the_provider_keeps_the_connection(service, upstream):
    binding, port = service
    upstream.respond = lambda handler, body: upstream.stream(
        handler, _sse(_chunk("hi"), _chunk(None, "stop")), hold_open=8.0
    )
    started = time.monotonic()
    status, text = _call(port, _path(binding), token=_TOKEN, body=_body(binding))
    assert status == 200 and time.monotonic() - started < 4.0
    assert ("response.completed" if binding.client == "responses" else "message_stop") in text


@pytest.mark.parametrize(
    "data",
    [
        _sse(_chunk("partial"), done=False),
        _sse(_chunk("x", "stop")).replace(b"data: [DONE]", b"data: {not json"),
        _sse(_chunk("x", "stop"), done=False) + b"data: [1, 2]\n\n",
    ],
    ids=["truncated", "malformed-after-finish", "non-object-after-finish"],
)
def test_a_broken_stream_is_a_failure_not_a_success(service, upstream, data):
    binding, port = service
    upstream.respond = lambda handler, body: upstream.stream(handler, data)
    _status, text = _call(port, _path(binding), token=_TOKEN, body=_body(binding))
    if binding.client == "responses":
        assert "response.failed" in text and "response.completed" not in text
    else:
        assert "event: error" in text and "message_stop" not in text


def test_a_cancelled_request_closes_the_upstream_request_promptly(service, upstream):
    binding, port = service
    seen_close: list[float] = []

    def slow(handler, body):
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.end_headers()
        handler.wfile.write(_sse(_chunk("thinking"), done=False))
        handler.wfile.flush()
        connection = handler.connection
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            readable, _, _ = select.select([connection], [], [], 0.1)
            if readable and not connection.recv(1, socket.MSG_PEEK):
                seen_close.append(time.monotonic())
                break
        handler.close_connection = True

    upstream.respond = slow
    client = socket.create_connection(("127.0.0.1", port))
    data = json.dumps(_body(binding)).encode()
    client.sendall(
        f"POST {_path(binding)} HTTP/1.1\r\nhost: x\r\ncontent-type: application/json\r\n"
        f"authorization: Bearer {_TOKEN}\r\ncontent-length: {len(data)}\r\n\r\n".encode()
        + data
    )
    received = b""
    client.settimeout(10)
    while b"thinking" not in received:
        chunk = client.recv(65536)
        assert chunk, "the service closed before relaying the first delta"
        received += chunk
    closed_at = time.monotonic()
    client.close()
    deadline = time.monotonic() + 5
    while not seen_close and time.monotonic() < deadline:
        time.sleep(0.05)
    assert seen_close, "the provider request stayed open after the CLI cancelled"
    assert seen_close[0] - closed_at < 3.0


def _wait_for_client_close(handler, seen: list[float]) -> None:
    connection = handler.connection
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        readable, _, _ = select.select([connection], [], [], 0.1)
        if readable and not connection.recv(1, socket.MSG_PEEK):
            seen.append(time.monotonic())
            return


def _cancel_after(port: int, binding: Binding, marker: bytes | None) -> float:
    """Send a request, read up to ``marker`` (or nothing), close; when it closed."""
    client = socket.create_connection(("127.0.0.1", port))
    data = json.dumps(_body(binding)).encode()
    client.sendall(
        f"POST {_path(binding)} HTTP/1.1\r\nhost: x\r\ncontent-type: application/json\r\n"
        f"authorization: Bearer {_TOKEN}\r\ncontent-length: {len(data)}\r\n\r\n".encode()
        + data
    )
    received = b""
    client.settimeout(10)
    while marker is not None and marker not in received:
        chunk = client.recv(65536)
        assert chunk
        received += chunk
    if marker is None:
        time.sleep(0.5)
    closed_at = time.monotonic()
    client.close()
    return closed_at


def _assert_seen_soon(seen: list[float], closed_at: float) -> None:
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert seen, "the provider request stayed open after the CLI cancelled"
    assert seen[0] - closed_at < 3.0


def test_a_cancel_before_the_provider_answers_closes_its_request(service, upstream):
    binding, port = service
    seen: list[float] = []
    # A provider still prefilling: no headers yet.
    upstream.respond = lambda handler, body: _wait_for_client_close(handler, seen)
    _assert_seen_soon(seen, _cancel_after(port, binding, None))


def test_a_cancel_closes_a_kept_alive_chunked_provider_stream(service, upstream):
    binding, port = service
    seen: list[float] = []

    def chunked(handler, body):
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("transfer-encoding", "chunked")
        handler.end_headers()
        data = _sse(_chunk("thinking"), done=False)
        handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        handler.wfile.flush()
        _wait_for_client_close(handler, seen)

    upstream.respond = chunked
    _assert_seen_soon(seen, _cancel_after(port, binding, b"thinking"))


def test_a_non_streamed_request_is_answered_whole(service, upstream):
    binding, port = service

    def whole(handler, body):
        data = json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode()
        handler.send_response(200)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    upstream.respond = whole
    status, text = _call(port, _path(binding), token=_TOKEN, body=_body(binding, stream=False))
    assert status == 200 and "done" in text
    assert "stream" not in upstream.requests[0]["body"]


def test_a_launch_without_its_token_key_or_https_upstream_starts_nothing():
    base = {
        "AGENT_COMPAT_CLIENT": "responses",
        "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example/v1",
        "AGENT_COMPAT_UPSTREAM_KEY": _KEY,
        "AGENT_COMPAT_MODEL": "m",
        "AGENT_COMPAT_TOKEN": _TOKEN,
    }
    for missing in ("AGENT_COMPAT_TOKEN", "AGENT_COMPAT_UPSTREAM_KEY", "AGENT_COMPAT_CLIENT"):
        with pytest.raises(LaunchError):
            Binding.from_launch({k: v for k, v in base.items() if k != missing}, ["cli"])
    # Not a token the control plane issued.
    with pytest.raises(LaunchError):
        Binding.from_launch({**base, "AGENT_COMPAT_TOKEN": "short"}, ["cli"])
    with pytest.raises(LaunchError):
        Binding.from_launch(
            {**base, "AGENT_COMPAT_UPSTREAM_URL": "http://provider.example/v1"}, ["cli"]
        )
    with pytest.raises(LaunchError, match="argument"):
        Binding.from_launch(dict(base), ["cli", f"--key={TOKEN_PLACEHOLDER}"])
    env = {**base, "AGENT_COMPAT_MODEL": "", "OTHER": "kept"}
    binding = Binding.from_launch(env, ["claude", "--model", "pinned-model"])
    assert binding.model == "pinned-model" and env == {"OTHER": "kept"}
    assert _KEY not in repr(binding) and binding.token not in repr(binding)


_CLI = """\
import json, os, sys, time
with open(sys.argv[1], "w") as out:
    json.dump({"argv": sys.argv, "env": dict(os.environ), "pid": os.getpid()}, out)
time.sleep(float(sys.argv[2]))
"""


def test_the_launcher_becomes_the_cli_and_its_service_leaves_with_it(tmp_path: Path):
    zipapp = tmp_path / "model-protocol-translator.pyz"
    zipapp.write_bytes(helper_archive())
    cli = tmp_path / "cli.py"
    cli.write_text(_CLI)
    report = tmp_path / "report.json"
    env = {
        "PATH": os.environ["PATH"],
        "AGENT_COMPAT_CLIENT": "anthropic_messages",
        "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example/v1",
        "AGENT_COMPAT_UPSTREAM_KEY": _KEY,
        "AGENT_COMPAT_MODEL": "m",
        "AGENT_COMPAT_TOKEN": _TOKEN,
        "ANTHROPIC_BASE_URL": ORIGIN_PLACEHOLDER,
        "ANTHROPIC_API_KEY": TOKEN_PLACEHOLDER,
    }
    process = subprocess.Popen(
        [
            "/usr/bin/python3",
            "-I",
            str(zipapp),
            sys.executable,
            str(cli),
            str(report),
            "3",
            ORIGIN_PLACEHOLDER,
        ],
        env=env,
    )
    deadline = time.monotonic() + 10
    while not report.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    seen = json.loads(report.read_text())
    # The process the host spawned is the CLI itself.
    assert seen["pid"] == process.pid
    origin = seen["env"]["ANTHROPIC_BASE_URL"]
    assert origin.startswith("http://127.0.0.1:") and seen["argv"][-1] == origin
    assert seen["env"]["ANTHROPIC_API_KEY"] == _TOKEN
    assert not any(name.startswith("AGENT_COMPAT_") for name in seen["env"])
    assert _KEY not in json.dumps(seen)
    port = int(origin.rsplit(":", 1)[1])
    assert _call(port, "/v1/messages", token=None, body={})[0] == 401
    process.wait(timeout=10)
    _assert_closed(port)


def test_the_service_leaves_when_the_cli_is_killed(tmp_path: Path):
    zipapp = tmp_path / "model-protocol-translator.pyz"
    zipapp.write_bytes(helper_archive())
    cli = tmp_path / "cli.py"
    cli.write_text(_CLI)
    report = tmp_path / "report.json"
    process = subprocess.Popen(
        ["/usr/bin/python3", "-I", str(zipapp), sys.executable, str(cli), str(report), "60"],
        env={
            "PATH": os.environ["PATH"],
            "AGENT_COMPAT_CLIENT": "responses",
            "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example/v1",
            "AGENT_COMPAT_UPSTREAM_KEY": _KEY,
            "AGENT_COMPAT_MODEL": "m",
            "AGENT_COMPAT_TOKEN": _TOKEN,
            "OPENAI_BASE_URL": f"{ORIGIN_PLACEHOLDER}/v1",
        },
    )
    deadline = time.monotonic() + 10
    while not report.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    port = int(
        json.loads(report.read_text())["env"]["OPENAI_BASE_URL"].rsplit(":", 1)[1].split("/")[0]
    )
    process.kill()
    process.wait(timeout=10)
    _assert_closed(port)


#: The two signals CPython ignores from its start.
_PYTHON_IGNORES = (1 << (signal.SIGPIPE - 1)) | (1 << (signal.SIGXFSZ - 1))
_DISPOSITIONS = ["grep", "-E", "^Sig(Ign|Blk):", "/proc/self/status"]
#: A CLI stand-in that reports what its one child, the service, ignores.
_SERVICE_IGNORES = """\
import os, re
me = os.getpid()
with open(f"/proc/{me}/task/{me}/children") as children:
    (service,) = children.read().split()
with open(f"/proc/{service}/status") as status:
    print(re.search(r"^SigIgn:\\t([0-9a-f]+)$", status.read(), re.M).group(1))
"""


def _run_cli(argv: list[str]) -> str:
    run = subprocess.run(
        argv,
        env={
            "PATH": os.environ["PATH"],
            "AGENT_COMPAT_CLIENT": "responses",
            "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example/v1",
            "AGENT_COMPAT_UPSTREAM_KEY": _KEY,
            "AGENT_COMPAT_MODEL": "m",
            "AGENT_COMPAT_TOKEN": _TOKEN,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="reads dispositions from /proc")
def test_the_cli_gets_a_direct_launchs_signals_and_the_service_keeps_pythons(tmp_path: Path):
    """CPython ignores SIGPIPE and SIGXFSZ, and an exec would hand that to the CLI.

    ``grep`` stands in for a native CLI: nothing resets a disposition on its way.
    """
    zipapp = tmp_path / "model-protocol-translator.pyz"
    zipapp.write_bytes(helper_archive())
    helper = ["/usr/bin/python3", "-I", str(zipapp)]

    direct = _run_cli(_DISPOSITIONS)
    behind = _run_cli([*helper, *_DISPOSITIONS])
    service = _run_cli([*helper, sys.executable, "-I", "-c", _SERVICE_IGNORES])

    assert re.findall(r"^Sig(?:Ign|Blk):", behind, re.M) == ["SigBlk:", "SigIgn:"]
    assert behind == direct
    # The service is the launcher's Python, where a closed client stays an error.
    assert int(service, 16) & _PYTHON_IGNORES == _PYTHON_IGNORES


def test_the_wrapper_seam_exists_only_in_test_code(tmp_path: Path):
    # The production launcher module defaults to HTTPS; only test code swaps it.
    assert compat_server.UPSTREAM_CONNECTION is http.client.HTTPSConnection
    assert "UPSTREAM_CONNECTION" in write_wrapper(tmp_path).read_text()


def _assert_closed(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError:
            return
        time.sleep(0.1)
    raise AssertionError("the compatibility service outlived its CLI")
