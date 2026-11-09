"""A direct plan's request meter: the CLI's requests forwarded unchanged, and measured.

The service runs in-process with its connection factory pointed at a plain-HTTP
fake provider (``UPSTREAM_CONNECTION``, settable only from Python). The launcher
runs for real, from the zipapp the host installs, in front of a CLI stand-in.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest

from cloud_agent_host import model_passthrough, protocol_translator
from cloud_agent_host.member_setup.compat_helper import helper_archive
from cloud_agent_host.model_passthrough import PassthroughBinding, _PassthroughServer
from cloud_agent_host.protocol_translator import LaunchError
from cloud_agent_shared.protocol_compat import ORIGIN_PLACEHOLDER, TOKEN_PLACEHOLDER
from cloud_agent_shared.request_prefix import read_record

if TYPE_CHECKING:
    from pathlib import Path

_KEY = "sk-owner-key-0123456789"
_SSE = b'event: message_start\ndata: {"type":"message_start"}\n\nevent: message_stop\ndata: {}\n\n'


class _Provider:
    """A plain-HTTP provider that records each request and answers as scripted."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self.respond = self.stream
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                del args

            def setup(self) -> None:
                super().setup()
                provider.connections += 1

            def _record(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                provider.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": self.rfile.read(length),
                    }
                )
                provider.respond(self)

            do_GET = do_POST = _record

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def stream(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("request-id", "req_1")
        handler.send_header("transfer-encoding", "chunked")
        handler.end_headers()
        for piece in (_SSE[:20], _SSE[20:]):
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            handler.wfile.flush()
        handler.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def provider(monkeypatch):
    fake = _Provider()

    def connect(host, port, timeout=None, context=None):
        assert (host, port) == ("provider.example", 443)
        return http.client.HTTPConnection("127.0.0.1", fake.port, timeout=timeout)

    monkeypatch.setattr(model_passthrough, "UPSTREAM_CONNECTION", connect)
    yield fake
    fake.server.shutdown()


@pytest.fixture
def records(monkeypatch):
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(
        model_passthrough,
        "emit_record",
        lambda line: written.append(read_record(line.decode().rstrip("\n"))) or True,
    )
    return written


@pytest.fixture
def meter(provider, records):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    server = _PassthroughServer(sock, PassthroughBinding(host="provider.example", port=443))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield sock.getsockname()[1]
    server.shutdown()
    server.server_close()


def _body() -> bytes:
    return json.dumps(
        {
            "model": "claude-x",
            "system": "You are helpful.",
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    ).encode()


def _post(connection: http.client.HTTPConnection, path: str, body: bytes, **headers: str):
    connection.request(
        "POST",
        path,
        body=body,
        headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01", **headers},
    )
    response = connection.getresponse()
    return response, response.read()


def test_a_request_is_forwarded_unchanged_and_its_prefix_measured(meter, provider, records):
    connection = http.client.HTTPConnection("127.0.0.1", meter)
    response, data = _post(connection, "/provider.example/anthropic/v1/messages?beta=true", _body())
    assert response.status == 200
    assert data == _SSE
    assert response.getheader("request-id") == "req_1"
    assert response.getheader("transfer-encoding") == "chunked"
    (sent,) = provider.requests
    assert sent["path"] == "/anthropic/v1/messages?beta=true"
    assert sent["body"] == _body()
    # The CLI's own key goes upstream in its own header; the hop's are the meter's.
    assert sent["headers"]["x-api-key"] == _KEY
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["headers"]["host"] == "provider.example"
    (record,) = records
    assert record["protocol"] == "anthropic_messages" and record["mode"] == "passthrough"
    assert record["system"]["chars"] == len("You are helpful.")
    assert record["tools"]["count"] == 1 and record["entries"][0][0] == "Read"


def test_a_provider_error_reaches_the_cli_as_it_was_sent(meter, provider):
    def refuse(handler: BaseHTTPRequestHandler) -> None:
        body = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
        handler.send_response(429)
        handler.send_header("retry-after", "7")
        handler.send_header("content-length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    provider.respond = refuse
    connection = http.client.HTTPConnection("127.0.0.1", meter)
    response, data = _post(connection, "/provider.example/v1/messages", _body())
    assert response.status == 429
    assert response.getheader("retry-after") == "7"
    assert b"rate_limit_error" in data


def test_only_paths_under_the_bound_authority_are_answered(meter, provider):
    for path in ("/v1/messages", "/provider.example.evil/v1/messages", "/other.example/v1"):
        connection = http.client.HTTPConnection("127.0.0.1", meter)
        response, _ = _post(connection, path, _body())
        assert response.status == 404
    assert provider.requests == []


def test_a_kept_connection_keeps_its_upstream_one(meter, provider, records):
    connection = http.client.HTTPConnection("127.0.0.1", meter)
    for _ in range(3):
        response, _ = _post(connection, "/provider.example/v1/messages", _body())
        assert response.status == 200
    assert len(provider.requests) == 3 and provider.connections == 1
    # One whole record, then repeats of the same prefix.
    assert len(records) == 3 and records[1] == {"fp": records[0]["fp"], "repeat": True}


def test_an_upstream_connection_closed_while_idle_is_replaced(meter, provider):
    # Idle means closed before the next request: on a busy runner the
    # provider's thread could close only once that request was on its way,
    # which is the race of a close crossing a request, not this case.
    closed = threading.Event()

    def answer_and_close(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("content-length", "2")
        handler.end_headers()
        handler.wfile.write(b"{}")
        handler.wfile.flush()
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.close_connection = True
        closed.set()

    provider.respond = answer_and_close
    connection = http.client.HTTPConnection("127.0.0.1", meter)
    for _ in range(2):
        closed.clear()
        response, data = _post(connection, "/provider.example/v1/messages", _body())
        assert (response.status, data) == (200, b"{}")
        assert closed.wait(10)
    assert provider.connections == 2


def test_a_chunked_request_goes_upstream_with_its_length(meter, provider):
    connection = http.client.HTTPConnection("127.0.0.1", meter)
    connection.putrequest("POST", "/provider.example/v1/chat/completions")
    connection.putheader("transfer-encoding", "chunked")
    connection.putheader("content-type", "application/json")
    connection.endheaders()
    body = json.dumps({"model": "m", "messages": [{"role": "system", "content": "s"}]}).encode()
    for piece in (body[:10], body[10:]):
        connection.send(b"%x\r\n%s\r\n" % (len(piece), piece))
    connection.send(b"0\r\n\r\n")
    response = connection.getresponse()
    response.read()
    (sent,) = provider.requests
    assert sent["body"] == body
    assert sent["headers"]["content-length"] == str(len(body))
    assert "transfer-encoding" not in sent["headers"]


def test_other_requests_pass_unmeasured(meter, provider, records):
    connection = http.client.HTTPConnection("127.0.0.1", meter)
    connection.request("GET", "/provider.example/v1/models", headers={"authorization": "Bearer k"})
    response = connection.getresponse()
    assert response.status == 200
    response.read()
    _post(connection, "/provider.example/v1/messages/count_tokens", _body())
    assert [request["method"] for request in provider.requests] == ["GET", "POST"]
    assert records == []


def test_an_unreadable_body_is_forwarded_all_the_same(meter, provider, records):
    connection = http.client.HTTPConnection("127.0.0.1", meter)
    response, _ = _post(connection, "/provider.example/v1/messages", b"{not json")
    assert response.status == 200
    assert provider.requests[0]["body"] == b"{not json"
    assert records == []


@pytest.mark.parametrize(
    "changes",
    [
        {"AGENT_COMPAT_UPSTREAM_URL": "http://provider.example"},
        {"AGENT_COMPAT_UPSTREAM_URL": "https://provider.example/v1"},
        {"AGENT_COMPAT_UPSTREAM_URL": "https://user@provider.example"},
        {"AGENT_COMPAT_UPSTREAM_KEY": _KEY},
        {"AGENT_COMPAT_TOKEN": "t" * 64},
        {"AGENT_COMPAT_CLIENT": "responses"},
    ],
)
def test_a_passthrough_launch_carries_an_origin_and_nothing_secret(changes):
    env = {
        "AGENT_COMPAT_CLIENT": "passthrough",
        "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example:8443",
        "KEEP": "1",
    }
    binding = PassthroughBinding.from_launch(dict(env))
    assert binding.authority == "provider.example:8443"
    remaining = dict(env)
    PassthroughBinding.from_launch(remaining)
    assert remaining == {"KEEP": "1"}
    with pytest.raises(LaunchError):
        PassthroughBinding.from_launch({**env, **changes})


def test_the_cli_reaches_its_provider_directly_when_the_meter_cannot_start(monkeypatch):
    def unbindable(make):
        raise OSError("no loopback")

    started: list[tuple[list[str], dict[str, str]]] = []

    class _Exec(Exception):
        pass

    def execvpe(path, argv, env):
        started.append((argv, env))
        raise _Exec

    monkeypatch.setattr(protocol_translator, "_start_service", unbindable)
    monkeypatch.setattr(protocol_translator.os, "execvpe", execvpe)
    # The CLI's side of the fork restores signals Python ignores; not this test process's.
    monkeypatch.setattr(protocol_translator.signal, "signal", lambda number, handler: None)
    monkeypatch.setattr(
        protocol_translator.os,
        "environ",
        {
            "AGENT_COMPAT_CLIENT": "passthrough",
            "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example",
            "ANTHROPIC_BASE_URL": f"{ORIGIN_PLACEHOLDER}/provider.example/anthropic",
        },
    )
    with pytest.raises(_Exec):
        protocol_translator.launch(["cli", "--url", f"{ORIGIN_PLACEHOLDER}/provider.example/v1"])
    ((argv, env),) = started
    assert argv == ["cli", "--url", "https://provider.example/v1"]
    assert env == {"ANTHROPIC_BASE_URL": "https://provider.example/anthropic"}


def test_a_passthrough_launch_refuses_a_token_placeholder(monkeypatch):
    monkeypatch.setattr(
        protocol_translator.os,
        "environ",
        {
            "AGENT_COMPAT_CLIENT": "passthrough",
            "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example",
            "ANTHROPIC_API_KEY": TOKEN_PLACEHOLDER,
        },
    )
    with pytest.raises(LaunchError, match="token"):
        protocol_translator.launch(["cli"])


_WRAPPER = """\
import http.client, sys
zipapp, port = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, zipapp)
import cloud_agent_host.model_passthrough as passthrough
import cloud_agent_host.protocol_translator as launcher

def fake_upstream(host, _port, timeout=None, context=None):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)

passthrough.UPSTREAM_CONNECTION = fake_upstream
sys.exit(launcher.main(sys.argv[3:]))
"""

_CLI = """\
import json, os, sys, urllib.request
request = urllib.request.Request(
    os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages",
    data=sys.argv[1].encode(),
    headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "content-type": "application/json"},
)
with urllib.request.urlopen(request) as answer:
    print(json.dumps({"status": answer.status, "body": answer.read().decode()}))
"""


def test_the_launcher_puts_the_meter_in_front_of_the_cli(tmp_path: Path, provider):
    zipapp = tmp_path / "model-protocol-translator.pyz"
    zipapp.write_bytes(helper_archive())
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(_WRAPPER)
    cli = tmp_path / "cli.py"
    cli.write_text(_CLI)
    env = {
        "PATH": os.environ["PATH"],
        "AGENT_COMPAT_CLIENT": "passthrough",
        "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example",
        "ANTHROPIC_BASE_URL": f"{ORIGIN_PLACEHOLDER}/provider.example/anthropic",
        "ANTHROPIC_API_KEY": _KEY,
    }
    run = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            str(wrapper),
            str(zipapp),
            str(provider.port),
            sys.executable,
            str(cli),
            _body().decode(),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["body"] == _SSE.decode()
    (sent,) = provider.requests
    assert sent["path"] == "/anthropic/v1/messages"
    assert sent["headers"]["x-api-key"] == _KEY
    # The record is on the CLI's stderr, where the host reads it; nothing else is.
    # Split on newlines only: ``splitlines`` also breaks at the marker's record separator.
    lines = [line for line in run.stderr.split("\n") if line]
    (record,) = [read_record(line) for line in lines]
    assert record["system"]["chars"] == len("You are helpful.")


def test_an_idle_connection_is_closed_only_when_it_ended_or_spoke():
    left, right = socket.socketpair()
    try:
        assert not model_passthrough._closed(left)
        right.sendall(b"x")
        assert model_passthrough._closed(left)
    finally:
        left.close()
        right.close()
    left, right = socket.socketpair()
    right.close()
    assert model_passthrough._closed(left)
    left.close()
    assert model_passthrough._closed(None)


def test_an_answer_that_breaks_off_ends_the_clis_answer_at_once(meter, provider):
    def short(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", "100")
        handler.end_headers()
        handler.wfile.write(b'{"partial": "answ')
        handler.wfile.flush()
        handler.close_connection = True

    provider.respond = short
    connection = http.client.HTTPConnection("127.0.0.1", meter, timeout=10)
    connection.request("POST", "/provider.example/v1/messages", body=_body())
    response = connection.getresponse()
    # Short at once, as from the provider itself -- not a wait until the timeout.
    with pytest.raises(http.client.IncompleteRead):
        response.read()


def test_a_redirect_on_the_origins_path_leads_back_through_the_meter(meter, provider):
    def moved(handler: BaseHTTPRequestHandler) -> None:
        if handler.path != "/v1/messages":
            provider.stream(handler)
            return
        handler.send_response(307)
        handler.send_header("location", "/v2/messages")
        handler.send_header("content-length", "0")
        handler.end_headers()

    provider.respond = moved
    connection = http.client.HTTPConnection("127.0.0.1", meter)
    response, _ = _post(connection, "/provider.example/v1/messages", _body())
    assert response.status == 307
    location = response.getheader("location")
    assert location == "/provider.example/v2/messages"
    response, body = _post(connection, location, _body())
    assert response.status == 200 and body == _SSE
    assert provider.requests[-1]["path"] == "/v2/messages"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/v2/messages", "/provider.example/v2/messages"),
        ("/", "/provider.example/"),
        ("https://other.example/v1", "https://other.example/v1"),
        ("//other.example/v1", "//other.example/v1"),
        ("messages", "messages"),
    ],
)
def test_only_a_location_on_the_origins_path_is_pointed_back(location, expected):
    binding = PassthroughBinding(host="provider.example", port=443)
    assert binding.client_location(location) == expected


_BROKEN_SERVICE = """\
import sys
zipapp = sys.argv[1]
sys.path.insert(0, zipapp)
import cloud_agent_host.model_passthrough as passthrough
import cloud_agent_host.protocol_translator as launcher

def broken(self, sock, binding):
    raise RuntimeError("no trust store")

passthrough._PassthroughServer.__init__ = broken
sys.exit(launcher.main(sys.argv[2:]))
"""

_PRINT_URL = "import os; print(os.environ['ANTHROPIC_BASE_URL'])"


def _run_broken(tmp_path: Path, env: dict[str, str], *cli: str) -> subprocess.CompletedProcess[str]:
    zipapp = tmp_path / "model-protocol-translator.pyz"
    zipapp.write_bytes(helper_archive())
    wrapper = tmp_path / "broken.py"
    wrapper.write_text(_BROKEN_SERVICE)
    return subprocess.run(
        ["/usr/bin/python3", "-I", str(wrapper), str(zipapp), *cli],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_a_meter_that_fails_after_the_fork_still_lets_the_cli_reach_its_provider(
    tmp_path: Path,
):
    run = _run_broken(
        tmp_path,
        {
            "AGENT_COMPAT_CLIENT": "passthrough",
            "AGENT_COMPAT_UPSTREAM_URL": "https://provider.example",
            "ANTHROPIC_BASE_URL": f"{ORIGIN_PLACEHOLDER}/provider.example/anthropic",
        },
        sys.executable,
        "-c",
        _PRINT_URL,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "https://provider.example/anthropic"
    assert "reaches its provider directly" in run.stderr


def test_a_translator_that_fails_after_the_fork_stops_the_launch(tmp_path: Path):
    script = tmp_path / "translated.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import cloud_agent_host.protocol_translator as launcher\n"
        "def broken(sock):\n"
        "    raise RuntimeError('no trust store')\n"
        "try:\n"
        "    launcher._start_service(broken)\n"
        "except OSError:\n"
        "    print('refused')\n"
    )
    zipapp = tmp_path / "model-protocol-translator.pyz"
    zipapp.write_bytes(helper_archive())
    run = subprocess.run(
        ["/usr/bin/python3", "-I", str(script), str(zipapp)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Not an origin for a service that is not there: the CLI is never pointed at it.
    assert run.stdout.strip() == "refused", run.stderr
