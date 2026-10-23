"""The host's own control-plane calls keep one connection instead of opening one each.

An idle host claims, waits and claims again, and each call used to open a
connection and build an SSL context of its own. These run against a real local
HTTP/1.1 server, because a connection that is reused is only visible to one.
"""

from __future__ import annotations

import http.server
import threading
from typing import TYPE_CHECKING, Any

import pytest

from cloud_agent_host import host_control

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def control_plane() -> Iterator[tuple[str, list[int], list[tuple[str, str]]]]:
    """A local control plane with no work, counting connections and credentials."""
    connections: list[int] = []
    seen: list[tuple[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            connections.append(1)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, self.headers["Authorization"]))
            body = b'{"token": "t"}' if self.path.endswith("/wait") else b"null"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", connections, seen
    finally:
        server.shutdown()
        server.server_close()


def _idle_cycle(url: str, client: Any = None) -> None:
    """One empty claim and the wait after it."""
    claimed = host_control.claim_payload(
        base_url=url, host_credential="aht.host", worker_id="w", lease_ttl=120.0, client=client
    )
    assert claimed is None
    token = host_control.wait_for_work(
        base_url=url, host_credential="aht.host", since=None, timeout_s=5.0, client=client
    )
    assert token == "t"


def test_claims_waits_and_a_release_share_one_connection(control_plane) -> None:
    url, connections, seen = control_plane
    with host_control.keepalive_client() as client:
        for _ in range(3):
            _idle_cycle(url, client)
        assert host_control.release_turn(
            base_url=url,
            worker_token="ajt.turn",
            job_id="ajob_1",
            reason="launch_failed",
            client=client,
        )

    assert len(connections) == 1
    # Credentials still travel per request: the release sends its turn's token.
    assert seen[-1] == ("/v1/agent/worker/jobs/ajob_1/release", "Bearer ajt.turn")
    assert {credential for _path, credential in seen[:-1]} == {"Bearer aht.host"}


def test_without_the_hosts_client_each_call_opens_its_own(control_plane) -> None:
    url, connections, _seen = control_plane
    for _ in range(3):
        _idle_cycle(url)
    assert len(connections) == 6


def test_the_host_lets_an_idle_connection_go_before_the_control_plane_does() -> None:
    """uvicorn closes an idle connection after its keep-alive timeout.

    A request sent on a connection the server is closing is lost with it, so the
    host must let go first. The control plane runs uvicorn at its default.
    """
    import uvicorn

    server_idle_s = uvicorn.Config(app="cloud_agent.app:create_app").timeout_keep_alive
    keepalive = host_control._CONTROL_PLANE_LIMITS.keepalive_expiry
    assert keepalive is not None
    assert keepalive < server_idle_s
