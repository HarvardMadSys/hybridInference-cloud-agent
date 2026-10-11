"""Host-side reporting of the whole-turn member execution window.

Two things are pinned here, and neither is "the numbers are right": the
15-second checkpoint cadence that keeps a one-second census from becoming a
one-second upload, and the runner's discipline when it forwards a snapshot.
The second is mostly about refusals. A report is fenced on the attempt's live
lease, it is never retried, a structurally broken snapshot is dropped rather
than posted, and a turn the broker did not measure is left absent instead of
being filled with zeros that a reader could not tell from a measured zero.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.member_resources import CheckpointSchedule
from cloud_agent_host.runner import ClaimedJob, ControlPlane, LeaseLost
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.sandbox import ContainerBackend

_JOB = ClaimedJob(
    job_id="ajob_1",
    attempt_id=7,
    attempt_no=1,
    repo="o/n",
    base_sha="a" * 40,
    task_prompt="do it",
    runtime="claude-code",
    model="model",
    worker_token="ajt.a.b",
    sandbox_token="grant",
    thread_id="athr_1",
    runtime_version=runner_mod.SHARED_SANDBOX_RUNTIME_PINS["claude-code"],
)


def _snapshot(**overrides: Any) -> dict[str, Any]:
    """Return one complete cumulative snapshot as the broker frames it."""
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "measurement_id": "00000000-0000-4000-8000-000000000001",
        "report_seq": 1,
        "state": "final",
        "scope": "member_execution_window",
        "source": "kata-guest-member-window-v1",
        "window": {"duration_ms": 1_000, "observed_through_ms": 1_000},
        "cpu": {"status": "available", "method": "live_and_exit_task_counters", "usec": 1_000},
        "memory": {
            "status": "available",
            "method": "sampled_member_pss",
            "peak_bytes": 4_096,
            "sample_interval_ms": 1_000,
        },
        "storage_io": {"status": "unavailable", "method": "task_io_accounting"},
    }
    snapshot.update(overrides)
    return snapshot


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _control() -> ControlPlane:
    """A real client whose placeholder transport the caller replaces."""
    control = ControlPlane("https://control.test", "test-token")
    control.close()
    control.job_id = "ajob_1"
    return control


def _json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _stamp() -> dict[str, Any]:
    return {
        "sandbox_image_digest": "sha256:shared-image",
        "runtime_versions": dict(runner_mod.SHARED_SANDBOX_RUNTIME_PINS),
        "runtime_versions_available": {
            key: [value] for key, value in runner_mod.SHARED_SANDBOX_RUNTIME_PINS.items()
        },
        "helper_versions": dict(runner_mod.SHARED_SANDBOX_HELPER_PINS),
    }


def _reservation(seed: dict[str, Any]) -> dict[str, Any]:
    """Answer a reservation request the way the control plane does."""
    return {
        **seed,
        "thread_id": _JOB.thread_id,
        "generation": 1,
        "activation_generation": 3,
        "resident_slot_id": 0,
    }


def _run_terminal_turn(
    monkeypatch, frame: dict[str, Any] | None, *, control: ControlPlane | None = None
) -> SimpleNamespace:
    """Drive one successful shared turn to its terminal transition.

    The heavy collaborators are mocked so the subject is the runner's own
    terminal path: what it forwards, and in which order. An optional
    ``control`` is used as given, so a caller can hold the real client.
    """
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    manager = Mock()
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: manager)
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(runner_mod, "_resident_base_ready", lambda *_args: True)
    prepared = SimpleNamespace(import_into=lambda *_args, **_kwargs: "b" * 40, discard=lambda: None)
    monkeypatch.setattr(runner_mod, "_prefetch_retained_source", lambda **_kwargs: prepared)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    if control is None:
        control = Mock(spec=ControlPlane)
        control.reserve_chat_sandbox.side_effect = _reservation
        control.mark_chat_sandbox_running.return_value = {}
    supervisor = Mock()
    supervisor.acquire_shared.return_value = {
        "token": "lease",
        "container_id": "same-container",
        "activation_generation": 3,
        "context_status": "new",
        "resident_state": "live",
    }
    supervisor.initial_archive_status.return_value = {
        "enabled": False,
        "prepared": False,
        "stored": False,
    }
    supervisor.capture_initial_archive.return_value = {
        "enabled": False,
        "stored": False,
    }
    supervisor.ensure_shared_resident.return_value = {
        "context_status": "new",
        "process_generation": 1,
    }
    supervisor.settle_shared.return_value = {"native_session_id": "native-new"}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod,
        "_stream_shared_agent_turn",
        lambda **_kwargs: (0, "", [], frame),
    )
    finish = Mock()

    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=replace(
            _JOB,
            runtime_version=runner_mod.SHARED_SANDBOX_RUNTIME_PINS["claude-code"],
        ),
        control=control,
        heart=SimpleNamespace(),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
    )
    return SimpleNamespace(result=result, control=control, supervisor=supervisor, finish=finish)


def _control_calls(run: SimpleNamespace) -> list[str]:
    """The terminal control calls, in the order the runner made them."""
    watched = {"report_resources", "report_turn_resources", "settle_chat_sandbox"}
    return [name for name, _args, _kwargs in run.control.mock_calls if name in watched]


def test_the_first_check_is_due_so_a_short_turn_still_checkpoints_once():
    schedule = CheckpointSchedule(clock=_Clock())
    assert schedule.due() is True


def test_a_checkpoint_is_due_only_after_the_full_interval():
    clock = _Clock()
    schedule = CheckpointSchedule(interval_s=15.0, clock=clock)
    assert schedule.due() is True
    schedule.mark_sent()

    clock.now = 14.999
    assert schedule.due() is False
    clock.now = 15.0
    assert schedule.due() is True


def test_an_interval_never_fires_twice_without_a_new_mark():
    """Checking often must not multiply uploads: only ``mark_sent`` restarts it."""
    clock = _Clock()
    schedule = CheckpointSchedule(interval_s=15.0, clock=clock)
    assert schedule.due() is True
    schedule.mark_sent()
    for _ in range(20):
        assert schedule.due() is False

    clock.now = 15.0
    assert schedule.due() is True
    # Still due until the caller records that it sent something. Reading is
    # not sending, and the caller owns that ordering.
    assert schedule.due() is True
    schedule.mark_sent()
    assert schedule.due() is False


def test_a_clock_that_stands_still_cannot_make_the_schedule_raise():
    schedule = CheckpointSchedule(interval_s=15.0, clock=_Clock())
    assert schedule.due() is True
    schedule.mark_sent()
    assert schedule.due() is False


def test_a_cumulative_checkpoint_and_a_final_snapshot_share_one_endpoint():
    """The same method serves both, so the body's own state is what differs."""
    paths: list[str] = []
    bodies: list[dict[str, Any]] = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        bodies.append(_json_body(request))
        return httpx.Response(200, json={"ok": True})

    control = _control()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    try:
        checkpoint = _snapshot(state="collecting", report_seq=9)
        final = _snapshot(state="final", report_seq=10)
        control.report_turn_resources(checkpoint)
        control.report_turn_resources(final)
    finally:
        control.close()

    assert paths == ["/v1/agent/worker/jobs/ajob_1/turn-resources"] * 2
    assert [body["state"] for body in bodies] == ["collecting", "final"]
    assert [body["report_seq"] for body in bodies] == [9, 10]
    assert bodies[0] == checkpoint and bodies[1] == final


def test_a_lost_lease_is_fatal_and_never_swallowed():
    def endpoint(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "lease_lost"})

    control = _control()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    try:
        with pytest.raises(LeaseLost):
            control.report_turn_resources(_snapshot())
    finally:
        control.close()


def test_a_platform_error_never_fails_a_turn_that_already_ran():
    def endpoint(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "internal error"})

    control = _control()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    try:
        control.report_turn_resources(_snapshot())
    finally:
        control.close()


def test_a_transport_error_never_fails_a_turn_that_already_ran():
    def endpoint(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("control plane unreachable", request=request)

    control = _control()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    try:
        control.report_turn_resources(_snapshot())
    finally:
        control.close()


@pytest.mark.parametrize(
    "payload",
    [
        # No summary at all, and the shapes a frame key can hold instead.
        None,
        "unavailable",
        ["window"],
        {},
        # A shape written by a schema this runner does not speak.
        {**_snapshot(), "schema_version": 2},
        # Fragments: bodies the endpoint would reject as malformed.
        {"schema_version": 1, "window": {}, "cpu": {}, "memory": {}, "storage_io": {}},
        {key: value for key, value in _snapshot().items() if key != "storage_io"},
        {key: value for key, value in _snapshot().items() if key != "report_seq"},
    ],
)
def test_an_unpostable_snapshot_is_not_released_for_posting(payload):
    """A rejected report reads as a platform error, which is worse than silence."""
    assert runner_mod._reportable_turn_resources(payload) is None


def test_a_structurally_invalid_snapshot_never_reaches_the_wire():
    bodies: list[dict[str, Any]] = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        bodies.append(_json_body(request))
        return httpx.Response(200, json={"ok": True})

    broken = {key: value for key, value in _snapshot().items() if key != "measurement_id"}
    control = _control()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    try:
        reportable = runner_mod._reportable_turn_resources(broken)
        assert reportable is None
        # One valid report proves the transport and the method are working, so
        # the empty assertion below is about the guard and not a dead client.
        control.report_turn_resources(_snapshot())
    finally:
        control.close()

    assert len(bodies) == 1
    assert bodies[0]["measurement_id"] == "00000000-0000-4000-8000-000000000001"


def test_the_reported_snapshot_is_the_broker_payload_unchanged():
    payload = _snapshot(state="collecting", report_seq=4)
    assert runner_mod._reportable_turn_resources(payload) is payload


def test_the_terminal_frame_snapshot_is_posted_before_the_lease_is_finished(monkeypatch):
    """The report is fenced on the live lease, so it cannot follow ``finish``."""
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    payload = _snapshot()
    run = _run_terminal_turn(
        monkeypatch,
        # The broker nests the summary inside `resources`; a top-level key is
        # a shape it never produces, and reading that was the bug.
        {"kind": "result", "status": "completed", "resources": {"turn_resources": payload}},
    )

    assert run.result == 0
    run.control.report_turn_resources.assert_called_once_with(payload)
    run.finish.assert_called_once()
    # The legacy rollup, then the member window, then the settlement that ends
    # the running state. ``finish`` is a mocked callback, so the order that
    # matters here is the control client's.
    assert _control_calls(run) == [
        "report_resources",
        "report_turn_resources",
        "settle_chat_sandbox",
    ]


def test_a_turn_the_broker_did_not_measure_posts_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    run = _run_terminal_turn(
        monkeypatch, {"kind": "result", "status": "completed", "resources": {}}
    )

    assert run.result == 0
    run.control.report_turn_resources.assert_not_called()
    # The legacy rollup still goes out: silence about the member window is not
    # silence about the turn's tool and process telemetry.
    run.control.report_resources.assert_called_once()


def test_a_structurally_invalid_frame_snapshot_is_not_posted(monkeypatch):
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    broken = {key: value for key, value in _snapshot().items() if key != "measurement_id"}
    run = _run_terminal_turn(
        monkeypatch,
        {"kind": "result", "status": "completed", "resources": {"turn_resources": broken}},
    )

    assert run.result == 0
    run.control.report_turn_resources.assert_not_called()
    run.finish.assert_called_once()


def test_a_running_checkpoint_is_posted_as_collecting_during_the_stream(monkeypatch):
    """A checkpoint is reported when it arrives, not held back for the final.

    The terminal snapshot is deliberately not in this stream: it is the
    runner's ``finally`` that posts that one, and the checkpoint has to have
    crossed the wire while the turn was still running -- and while ``final``
    is still an open question -- for a crash to leave anything behind.
    """
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    bodies: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def endpoint(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            events.append(_json_body(request))
            return httpx.Response(200, json={"ok": True})
        assert request.url.path.endswith("/turn-resources")
        bodies.append(_json_body(request))
        return httpx.Response(200, json={"ok": True})

    control = _control()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    checkpoint = _snapshot(
        state="collecting",
        report_seq=11,
        window={"duration_ms": 0, "observed_through_ms": 0},
    )
    frames = iter(
        [
            {"kind": "resource_checkpoint", "turn_resources": checkpoint},
            {"kind": "result", "status": "completed", "resources": {}},
        ]
    )

    class Broker:
        def frames(self):
            return frames

        def close(self):
            return None

    supervisor = Mock()
    supervisor.start_shared_turn.return_value = Broker()
    # The heartbeat thread is not the subject here, and a no-op keeps the
    # stream from reaching a control plane that does not exist on the wire.
    monkeypatch.setattr(runner_mod, "Heartbeater", Mock())
    try:
        exit_code, _tail, _errors, _result_frame = runner_mod._stream_shared_agent_turn(
            supervisor=supervisor,
            job=_JOB,
            control=control,
            heart=SimpleNamespace(lease_lost=False, cancel_requested=False),
            lease_token="lease",
            timeout_s=30,
        )
    finally:
        control.close()

    assert exit_code == 0
    assert events == [
        {"event_type": "lifecycle", "payload": {"phase": "turn_started"}, "event_seq": 1}
    ]
    assert len(bodies) == 1
    assert bodies[0]["state"] == "collecting"
    assert bodies[0]["report_seq"] == 11
    assert bodies[0]["window"]["observed_through_ms"] == 0


def test_the_broker_and_the_runner_agree_about_where_the_summary_lives(monkeypatch):
    """The frame contract between the two host modules, pinned by both sides.

    The broker nests the summary under ``resources``; the runner used to read
    the frame's top level, so every real turn reported nothing while a
    hand-built fixture kept the test green. This asserts the producer's actual
    key path and the consumer's read of it in one place.
    """
    from cloud_agent_host.guest_observer import GuestObservation
    from cloud_agent_host.shared_session_supervisor import SharedTurnTelemetry
    from cloud_agent_host.tool_windows import ToolWindowTracker
    from cloud_agent_host.workspace_broker import build_turn_resources

    payload = _snapshot()
    telemetry = SharedTurnTelemetry(
        observation=GuestObservation(
            source="kata-guest-ebpf-v1",
            complete=True,
            lost_events=0,
            truncated=False,
        ),
        turn_resources=payload,
    )

    # The real producer, not a frame written by hand: a source-text assertion
    # passed while the summary was still emitted from the wrong place.
    produced = build_turn_resources(telemetry, ToolWindowTracker())

    assert produced["turn_resources"] == payload
    run = _run_terminal_turn(
        monkeypatch,
        {"kind": "result", "status": "completed", "resources": produced},
    )

    run.control.report_turn_resources.assert_called_once_with(payload)
