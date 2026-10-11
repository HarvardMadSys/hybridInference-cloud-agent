"""Exercise quiet broker streams over real HTTP, including their watchdogs."""

from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from cloud_agent_host import runner
from cloud_agent_host.runner import LeaseLost
from cloud_agent_host.session_supervisor_client import (
    SessionSupervisorClient,
    SessionSupervisorError,
)


@pytest.fixture
def quiet_broker():
    """Emit one event, then stay silent until the test or runner releases it."""
    release = threading.Event()
    interrupted = threading.Event()
    event_sent = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path.endswith("/interrupt"):
                interrupted.set()
                release.set()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return
            assert self.path.endswith("/turn")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(
                b'{"kind":"event","event_type":"lifecycle","payload":{"phase":"init"}}\n'
            )
            self.wfile.flush()
            event_sent.set()
            if not release.wait(5):
                return
            result = {
                "kind": "result",
                "status": "cancelled" if interrupted.is_set() else "completed",
                "exit_code": 130 if interrupted.is_set() else 0,
                "resources": {"process_observation_complete": True},
            }
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(json.dumps(result).encode() + b"\n")

        def do_GET(self):
            assert self.path == "/sandboxes"
            # A lifecycle request must still have a bounded header wait.
            release.wait(5)
            self.send_response(200)
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(b'{"sandboxes":[]}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SessionSupervisorClient(
        f"http://127.0.0.1:{server.server_port}", "broker-token", timeout=0.1
    )
    try:
        yield SimpleNamespace(
            client=client, release=release, interrupted=interrupted, event_sent=event_sent
        )
    finally:
        release.set()
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_turn(client, *, heart=None, control=None, timeout_s=5):
    return runner._stream_shared_agent_turn(
        supervisor=client,
        job=SimpleNamespace(
            thread_id="athr_quiet",
            task_prompt="inspect the repository",
            model="model",
            reasoning_effort=None,
        ),
        control=control or SimpleNamespace(append_event=lambda _event: None),
        heart=heart or SimpleNamespace(cancel_requested=False, lease_lost=False),
        lease_token="lease",
        timeout_s=timeout_s,
    )


def test_quiet_turn_outlives_lifecycle_timeout(quiet_broker):
    """A model can think longer than the control API timeout and still finish."""
    timer = threading.Timer(0.5, quiet_broker.release.set)
    events = []

    def on_event(event):
        events.append((event.event_type, event.payload))
        if event.payload == {"phase": "init"}:
            timer.start()

    control = SimpleNamespace(append_event=on_event)
    try:
        code, _tail, errors, result = _run_turn(quiet_broker.client, control=control)
    finally:
        timer.cancel()
        timer.join(timeout=5)

    assert code == 0
    assert errors == []
    assert events == [
        ("lifecycle", {"phase": "turn_started"}),
        ("lifecycle", {"phase": "init"}),
    ]
    assert result["status"] == "completed"
    assert not quiet_broker.interrupted.is_set()

    # A stream-specific allowance must not leak to ordinary control requests.
    quiet_broker.release.clear()
    with pytest.raises(SessionSupervisorError) as caught:
        quiet_broker.client.list()
    assert isinstance(caught.value.__cause__, httpx.ReadTimeout)


@pytest.mark.parametrize("reason", ["deadline", "cancel", "lease_lost"])
def test_quiet_turn_remains_interruptible(quiet_broker, monkeypatch, reason):
    """Waiting for a quiet model must preserve the runner's active controls."""
    monkeypatch.setattr(runner, "_POLL_INTERVAL_S", 0.01)
    heart = SimpleNamespace(cancel_requested=False, lease_lost=False)

    def on_event(event):
        if event.payload != {"phase": "init"}:
            return
        if reason == "cancel":
            heart.cancel_requested = True
        elif reason == "lease_lost":
            heart.lease_lost = True

    kwargs = {
        "heart": heart,
        "control": SimpleNamespace(append_event=on_event),
        "timeout_s": 0.3 if reason == "deadline" else 5,
    }
    if reason == "lease_lost":
        with pytest.raises(LeaseLost):
            _run_turn(quiet_broker.client, **kwargs)
    else:
        code, _tail, _errors, result = _run_turn(quiet_broker.client, **kwargs)
        assert code == (124 if reason == "deadline" else 130)
        assert result["resources"]["process_observation_complete"] is True
    # The client can interrupt after consuming init before the server resumes
    # past its flush and records the send. Synchronize with that server thread.
    assert quiet_broker.event_sent.wait(1)
    assert quiet_broker.interrupted.is_set()
