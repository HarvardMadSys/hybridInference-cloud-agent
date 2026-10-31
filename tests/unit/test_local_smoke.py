"""The local smoke proves first-run setup, or fails: it never passes on nothing.

Each case runs the smoke's own ``Client`` against a stand-in control plane on a
loopback port. The stand-in sets its session cookie with the real
``set_local_session_cookie``, so a smoke that looks for any other cookie name
fails here exactly as it would against a real stack.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from deploy.local import smoke
from starlette.responses import Response

from cloud_agent.accounts.config import (
    ENV_SESSION_COOKIE_SECURE,
    LOCAL_SESSION_COOKIE,
    LOCAL_SESSION_COOKIE_INSECURE,
)
from cloud_agent.accounts.session_cookie import set_local_session_cookie

CODE = "ABCD-EFGH-JKLM"


class _ControlPlane:
    """What the stand-in answers: whether setup is pending and how setup replies."""

    def __init__(self, *, pending: Any, setup: tuple[int, dict[str, Any]], cookie: bool = True):
        self.pending = pending
        self.setup = setup
        self.cookie = cookie
        self.setup_bodies: list[dict[str, Any]] = []


def _serve(plane: _ControlPlane, monkeypatch: pytest.MonkeyPatch):
    # The local stack serves plain HTTP, so its cookie is the plain name.
    monkeypatch.setenv(ENV_SESSION_COOKIE_SECURE, "false")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def _answer(self, status: int, body: dict[str, Any], headers=()) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/api/v1/auth/options":
                options = {} if plane.pending is None else {"setup_required": plane.pending}
                self._answer(200, options)
            else:
                self._answer(404, {})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            plane.setup_bodies.append(json.loads(self.rfile.read(length) or b"{}"))
            status, body = plane.setup
            headers = []
            if status == 200 and plane.cookie:
                response = Response()
                set_local_session_cookie(response, "cals1.session-token")
                headers = [
                    (name.decode(), value.decode())
                    for name, value in response.raw_headers
                    if name == b"set-cookie"
                ]
            self._answer(status, body, headers)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, smoke.Client(f"http://127.0.0.1:{server.server_address[1]}")


@pytest.fixture
def control_plane(monkeypatch):
    servers = []

    def start(plane: _ControlPlane) -> smoke.Client:
        server, client = _serve(plane, monkeypatch)
        servers.append(server)
        return client

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _complete() -> tuple[int, dict[str, Any]]:
    message = "This deployment has already been set up."
    return 409, {"detail": {"error": {"type": "setup_complete", "message": message}}}


def test_the_smoke_looks_for_the_account_session_cookies():
    assert {LOCAL_SESSION_COOKIE, LOCAL_SESSION_COOKIE_INSECURE} == smoke.SESSION_COOKIES


def test_setup_with_the_logged_code_creates_the_administrator_and_signs_it_in(control_plane):
    plane = _ControlPlane(pending=True, setup=(200, {"id": "01JADMIN000000000000000000"}))
    client = control_plane(plane)
    assert smoke.setup_admin(client, CODE) == smoke.CREATED
    assert client.signed_in()
    assert {cookie.name for cookie in client.cookies} == {LOCAL_SESSION_COOKIE_INSECURE}
    assert plane.setup_bodies[0]["setup_code"] == CODE


def test_setup_that_sets_no_account_cookie_fails(control_plane):
    plane = _ControlPlane(pending=True, setup=(200, {"id": "x"}), cookie=False)
    with pytest.raises(RuntimeError, match="no account session cookie"):
        smoke.setup_admin(control_plane(plane), CODE)


def test_pending_setup_without_a_logged_code_fails_and_asks_nothing(control_plane):
    """A stack with no administrator, and no code in its log, is not a passing stack."""
    plane = _ControlPlane(pending=True, setup=_complete())
    with pytest.raises(RuntimeError, match="no setup code"):
        smoke.setup_admin(control_plane(plane), "")
    assert plane.setup_bodies == []


def test_setup_already_done_passes_only_on_the_servers_setup_complete(control_plane):
    plane = _ControlPlane(pending=False, setup=_complete())
    client = control_plane(plane)
    assert smoke.setup_admin(client, "") == smoke.COMPLETE
    assert not client.signed_in()


@pytest.mark.parametrize(
    "answer",
    [
        (409, {"detail": {"error": {"type": "conflict"}}}),
        (403, {"detail": {"error": {"type": "invalid_setup_code"}}}),
        (500, {}),
        (200, {"id": "x"}),
    ],
)
def test_any_other_answer_to_finished_setup_fails(control_plane, answer):
    with pytest.raises(RuntimeError, match="did not say it is complete"):
        smoke.setup_admin(control_plane(_ControlPlane(pending=False, setup=answer)), CODE)


@pytest.mark.parametrize("pending", [None, "yes"])
def test_options_that_do_not_say_whether_setup_is_pending_fail(control_plane, pending):
    with pytest.raises(RuntimeError, match="do not say whether setup is pending"):
        smoke.setup_admin(control_plane(_ControlPlane(pending=pending, setup=_complete())), CODE)


@pytest.mark.parametrize("answer", [(403, {"detail": {"error": {"type": "invalid_setup_code"}}})])
def test_a_refused_code_fails(control_plane, answer):
    with pytest.raises(RuntimeError, match="setup failed: 403"):
        smoke.setup_admin(control_plane(_ControlPlane(pending=True, setup=answer)), CODE)
