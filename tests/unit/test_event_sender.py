"""The runner's ordered event sender, against a control plane on a mock wire.

An event is numbered, redacted and encoded where it is reported, then uploaded
by one thread, in order, in batches. These pin what that must not change: the
order and the numbering, redaction before the event is queued, a bounded queue
that still pushes back on the turn, a lost lease and an exhausted retry budget
that still end it, and writes that end or interleave with the event log waiting
for the events reported before them. They also pin the rolling-deploy fallback:
a control plane without the batch endpoint is sent one event per request.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from cloud_agent_host import control_plane_client, event_sender
from cloud_agent_host.claimed_job import LeaseLost
from cloud_agent_host.control_plane_client import ControlPlane
from cloud_agent_host.runtimes import NormalizedEvent
from cloud_agent_shared.secret_redaction import SecretRedactor

_WAIT_S = 5.0


class _Plane:
    """A control plane that records every request, answering with ``answer``.

    ``answer`` sees each request and returns a status, or raises for a
    transport failure; ``None`` is a stored upload.
    """

    def __init__(self, answer: Any = None) -> None:
        self.answer = answer or (lambda _request: None)
        self.requests: list[tuple[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.removeprefix("/v1/agent/worker/jobs/ajob_1/")
        self.requests.append((endpoint, json.loads(request.content) if request.content else None))
        status = self.answer(request)
        return httpx.Response(status or 201, json={})

    def endpoints(self) -> list[str]:
        return [endpoint for endpoint, _body in self.requests]

    def uploads(self) -> list[list[int]]:
        """The numbers each event upload carried, in the order they were sent."""
        return [
            [event["event_seq"] for event in body["events"]]
            if endpoint == "events:batch"
            else [body["event_seq"]]
            for endpoint, body in self.requests
            if endpoint in {"events", "events:batch"}
        ]

    def events(self) -> list[dict[str, Any]]:
        """Every event an upload the control plane stored carried, in order."""
        return [
            event
            for endpoint, body in self.requests
            if endpoint in {"events", "events:batch"}
            for event in (body["events"] if endpoint == "events:batch" else [body])
        ]


class _Held:
    """Hold the first upload on the wire until the test lets it go."""

    def __init__(self, then: Any = None) -> None:
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.then = then or (lambda _request: None)

    def __call__(self, request: httpx.Request) -> Any:
        if not self.arrived.is_set():
            self.arrived.set()
            assert self.release.wait(_WAIT_S), "the test never released the first upload"
        return self.then(request)


def _control(plane: _Plane) -> ControlPlane:
    """A real worker client whose only wire is ``plane``."""
    control = ControlPlane("https://control.test", "test-token")
    control.close()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(plane))
    control.job_id = "ajob_1"
    return control


def _event(index: int, event_type: str = "message") -> NormalizedEvent:
    return NormalizedEvent(event_type, {"text": f"part {index}"})


def _virtual_clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Make the sender's retry waits advance a fake clock instead of real time."""
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(control_plane_client.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(control_plane_client.time, "sleep", sleep)
    return clock


def test_an_idle_sender_waits_briefly_for_the_events_that_follow(monkeypatch):
    """Events reported together share one upload; a drain cuts the wait short."""
    monkeypatch.setattr(event_sender, "_EVENT_BATCH_LINGER_S", _WAIT_S)
    plane = _Plane()
    control = _control(plane)
    try:
        for index in range(10):
            control.append_event(_event(index))
        control.flush()
    finally:
        control.close()

    assert plane.endpoints() == ["events:batch"]
    assert plane.uploads() == [list(range(1, 11))]
    assert [event["payload"]["text"] for event in plane.events()] == [
        f"part {index}" for index in range(10)
    ]


def test_events_reach_the_control_plane_without_a_drain(monkeypatch):
    monkeypatch.setattr(event_sender, "_EVENT_BATCH_LINGER_S", 0.01)
    stored = threading.Event()
    plane = _Plane(lambda _request: stored.set())
    control = _control(plane)
    try:
        control.append_event(_event(0))
        assert stored.wait(_WAIT_S)
    finally:
        control.close()

    assert plane.uploads() == [[1]]


def test_events_reported_during_an_upload_share_the_next_one():
    """A slow control plane gets fewer, larger uploads instead of a slower turn."""
    held = _Held()
    plane = _Plane(held)
    control = _control(plane)
    try:
        control.append_event(_event(0))
        assert held.arrived.wait(_WAIT_S)
        for index in range(1, 11):
            control.append_event(_event(index))
        held.release.set()
        control.flush()
    finally:
        control.close()

    assert plane.uploads() == [[1], list(range(2, 12))]


def _held_back(*events: NormalizedEvent) -> _Plane:
    """Upload ``events`` queued behind one upload held on the wire, and return the wire."""
    held = _Held()
    plane = _Plane(held)
    control = _control(plane)
    try:
        control.append_event(_event(0))
        assert held.arrived.wait(_WAIT_S)
        for event in events:
            control.append_event(event)
        held.release.set()
        control.flush()
    finally:
        control.close()
    return plane


def test_an_upload_carries_at_most_its_number_of_events():
    plane = _held_back(*(_event(index) for index in range(1, 251)))

    assert [len(upload) for upload in plane.uploads()] == [1, 100, 100, 50]
    assert [event["event_seq"] for event in plane.events()] == list(range(1, 252))


def test_an_upload_carries_at_most_its_bytes_and_a_larger_event_goes_alone():
    large = "x" * (100 * 1024)
    plane = _held_back(
        *(NormalizedEvent("tool_result", {"content": large}) for _ in range(3)),
        NormalizedEvent("tool_result", {"content": large * 3}),
    )

    assert plane.uploads() == [[1], [2, 3], [4], [5]]
    for _endpoint, body in plane.requests[1:3]:
        encoded = [
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) for event in body["events"]
        ]
        assert sum(len(item.encode()) for item in encoded) <= 256 * 1024


@pytest.mark.parametrize("status", [404, 405])
def test_a_control_plane_without_batches_is_sent_one_event_per_request(status):
    """A rolling deploy can pair this runner with a control plane that predates batches."""
    plane = _Plane(lambda request: status if request.url.path.endswith(":batch") else None)
    control = _control(plane)
    try:
        control.append_event(_event(0))
        control.flush()
        control.append_event(_event(1))
        control.append_event(_event(2))
        control.flush()
    finally:
        control.close()

    # Asked once; nothing was stored by the refused batch, so its event is sent again.
    assert plane.endpoints() == ["events:batch", "events", "events", "events"]
    assert [event["event_seq"] for event in plane.events()][1:] == [1, 2, 3]


def test_an_append_raises_what_the_sender_gave_up_on_once(monkeypatch):
    """An append waiting for room learns of the refused upload, and the next one is sent."""
    monkeypatch.setattr(event_sender, "_EVENT_QUEUE_MAX_EVENTS", 1)
    refused = threading.Event()
    held = _Held(lambda _request: None if refused.is_set() else 422)
    plane = _Plane(held)
    control = _control(plane)
    try:
        control.append_event(_event(0))
        assert held.arrived.wait(_WAIT_S)
        control.append_event(_event(1))
        timer = threading.Timer(0.05, held.release.set)
        timer.start()
        with pytest.raises(httpx.HTTPStatusError) as raised:
            # The sender holds its fill, so this waits until the upload ahead is refused.
            control.append_event(_event(2))
        timer.join()
        assert raised.value.response.status_code == 422
        refused.set()
        # The turn that sees the failure still reports it through the same sender.
        control.append_event(NormalizedEvent("error", {"text": "events could not be recorded"}))
        control.flush()
    finally:
        control.close()

    assert plane.uploads() == [[1], [4]]
    assert plane.events()[-1]["event_type"] == "error"


@pytest.mark.parametrize("bound", ["_EVENT_QUEUE_MAX_EVENTS", "_EVENT_QUEUE_MAX_BYTES"])
def test_an_append_waits_while_the_sender_holds_its_fill(monkeypatch, bound):
    """The queue is bounded by count and by bytes, so a stuck sender still pushes back."""
    size = len(control_plane_client._event_json("message", {"text": "part 1"}, 1))
    monkeypatch.setattr(event_sender, bound, 2 if bound.endswith("EVENTS") else 2 * size)
    held = _Held()
    plane = _Plane(held)
    control = _control(plane)
    try:
        control.append_event(_event(0))
        assert held.arrived.wait(_WAIT_S)
        control.append_event(_event(1))
        control.append_event(_event(2))
        waiting = threading.Thread(target=control.append_event, args=(_event(3),))
        waiting.start()
        waiting.join(0.2)
        assert waiting.is_alive(), "the append went past a full queue"
        held.release.set()
        waiting.join(_WAIT_S)
        assert not waiting.is_alive()
        control.flush()
    finally:
        control.close()

    assert [event["event_seq"] for event in plane.events()] == [1, 2, 3, 4]


def test_a_lost_lease_fails_every_later_append_and_the_finish():
    plane = _Plane(lambda request: 409 if request.url.path.endswith(":batch") else None)
    control = _control(plane)
    try:
        control.append_event(_event(0))
        with pytest.raises(LeaseLost):
            control.flush()
        assert control.lease_lost is True
        with pytest.raises(LeaseLost):
            control.append_event(_event(1))
        with pytest.raises(LeaseLost):
            control.finish("failed", "the lease is gone")
    finally:
        control.close()

    assert plane.endpoints() == ["events:batch"], "nothing more is sent once the lease is lost"


def test_a_given_up_upload_is_retried_whole_within_its_budget(monkeypatch):
    clock = _virtual_clock(monkeypatch)
    tries: list[float] = []

    def refuse(request: httpx.Request) -> None:
        tries.append(clock.now)
        raise httpx.ConnectError("refused", request=request)

    plane = _Plane(refuse)
    control = _control(plane)
    try:
        control.append_event(_event(0))
        control.append_event(_event(1))
        with pytest.raises(httpx.ConnectError):
            control.flush()
    finally:
        control.close()

    assert len(tries) > 2
    assert all(upload == plane.uploads()[0] for upload in plane.uploads())
    assert clock.now <= control_plane_client._EVENT_RETRY_BUDGET_S


def test_finish_stores_the_turns_events_and_preview_tails_before_it_ends():
    """The error a failing turn reported precedes its end, and a held-back tail is not lost."""
    plane = _Plane()
    control = _control(plane)
    control._secret_values = {"TOKEN": "supersecretvalue"}
    control._redactor = SecretRedactor(control._secret_values)
    try:
        # "super" might begin the secret, so the preview redactor holds it back.
        control.append_event(
            NormalizedEvent("message_delta", {"preview_id": "p1", "delta": "hello super"})
        )
        control.append_event(NormalizedEvent("error", {"text": "the agent exited"}))
        control.finish("failed", "the agent exited")
    finally:
        control.close()

    assert plane.endpoints()[-1] == "finish"
    assert [(event["event_type"], event["payload"]) for event in plane.events()] == [
        ("message_delta", {"preview_id": "p1", "delta": "hello "}),
        ("error", {"text": "the agent exited"}),
        ("message_delta", {"preview_id": "p1", "delta": "super"}),
    ]
    assert [event["event_seq"] for event in plane.events()] == [1, 2, 3]


def test_a_drain_that_was_given_up_on_still_ends_the_attempt(monkeypatch):
    """An event that cannot be recorded must not strand the attempt as running."""
    clock = _virtual_clock(monkeypatch)

    def refuse_events(request: httpx.Request) -> None:
        if request.url.path.endswith(":batch"):
            raise httpx.ConnectError("refused", request=request)

    plane = _Plane(refuse_events)
    control = _control(plane)
    try:
        control.append_event(NormalizedEvent("error", {"text": "the agent exited"}))
        control.finish("failed", "the agent exited")
    finally:
        control.close()

    assert plane.endpoints()[-1] == "finish"
    assert plane.endpoints().count("events:batch") > 2
    assert clock.now <= control_plane_client._EVENT_RETRY_BUDGET_S


def test_an_event_is_redacted_when_it_is_reported_not_when_it_is_sent():
    held = _Held()
    plane = _Plane(held)
    control = _control(plane)
    control._secret_values = {"TOKEN": "supersecretvalue"}
    control._redactor = SecretRedactor(control._secret_values)
    try:
        control.append_event(_event(0))
        assert held.arrived.wait(_WAIT_S)
        payload = {"content": "TOKEN=supersecretvalue"}
        control.append_event(NormalizedEvent("tool_result", payload))
        # Neither a redactor swapped afterwards nor a changed payload reaches the upload.
        control._redactor = SecretRedactor({})
        payload["content"] = "changed after it was reported"
        held.release.set()
        control.flush()
    finally:
        control.close()

    assert plane.events()[1]["payload"] == {"content": "TOKEN=[secret:TOKEN]"}


def test_writes_that_end_or_interleave_with_the_log_follow_the_events_before_them():
    """The terminal endpoints write events of their own, and a release ends the attempt."""
    plane = _Plane()
    control = _control(plane)
    try:
        control.append_event(NormalizedEvent("lifecycle", {"phase": "started"}))
        control.suspend_terminals("workspace_preparing")
        control.append_event(NormalizedEvent("lifecycle", {"phase": "checked_out"}))
        control.resume_terminals()
        control.append_event(NormalizedEvent("lifecycle", {"phase": "setup"}))
        assert control.release_turn("sandbox_busy") is True
    finally:
        control.close()

    assert plane.endpoints() == [
        "events:batch",
        "terminals/suspend",
        "events:batch",
        "terminals/resume",
        "events:batch",
        "release",
    ]


def test_closing_stops_the_sender_and_drops_what_it_still_holds():
    held = _Held()
    plane = _Plane(held)
    control = _control(plane)
    control.append_event(_event(0))
    assert held.arrived.wait(_WAIT_S)
    control.append_event(_event(1))
    sender = control._events._thread
    assert sender is not None
    control.close()
    held.release.set()
    sender.join(_WAIT_S)

    assert not sender.is_alive()
    assert plane.uploads() == [[1]]
