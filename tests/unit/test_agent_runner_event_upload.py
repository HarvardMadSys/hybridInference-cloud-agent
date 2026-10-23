"""How a turn's events reach the control plane, and what a turn does when they cannot.

Events go up in batches while the turn runs. An upload that a replay can fix
is retried within its budget; one that lost the lease ends the turn at once,
and a turn whose events could not be recorded fails instead of settling. The
core module keeps the shared fakes and the autouse profiling-cache fixture.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from tests.unit.test_agent_runner import (  # noqa: F401 - the autouse pytest fixture
    _JOB,
    _Control,
    _event_control,
    _reset_profiling_cache,
    _run_shared_boundary_case,
    _Stream,
    _Supervisor,
    _virtual_clock,
)

from cloud_agent_host import control_plane_client, runner as runner_mod
from cloud_agent_host.runner import LeaseLost


def test_a_lease_an_event_upload_lost_ends_the_turn_at_once():
    """Uploads are not waited for, so the loop reads the sender's flag beside the heartbeat's."""

    class _Refused(_Control):
        def append_event(self, event):
            super().append_event(event)
            if event.event_type == "assistant":
                self.lease_lost = True

    supervisor = _Supervisor(
        [
            {"kind": "event", "event_type": "assistant", "payload": {"text": "one"}},
            {"kind": "event", "event_type": "assistant", "payload": {"text": "two"}},
            {"kind": "result", "status": "completed", "exit_code": 0},
        ]
    )
    control = _Refused()
    with pytest.raises(LeaseLost):
        runner_mod._stream_shared_agent_turn(
            supervisor=supervisor,
            job=_JOB,
            control=control,
            heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
            lease_token="lease",
            timeout_s=30,
        )

    assert supervisor.interrupted is True
    assert control.events == [
        ("lifecycle", {"phase": "turn_started"}),
        ("assistant", {"text": "one"}),
    ]


def test_question_change_frame_notifies_the_attempt_control_plane():
    supervisor = _Supervisor(
        [
            {"kind": "question_changed"},
            {"kind": "result", "status": "completed", "exit_code": 0},
        ]
    )
    control = _Control()

    code, _tail, errors, result, _reported = runner_mod._stream_shared_agent_turn(
        supervisor=supervisor,
        job=_JOB,
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        lease_token="lease",
        timeout_s=30,
    )

    assert code == 0 and not errors and result["status"] == "completed"
    assert control.question_hints == [_JOB.job_id]
    assert control.events == [("lifecycle", {"phase": "turn_started"})]


def _uploaded_events(request: httpx.Request) -> list[dict[str, Any]]:
    """The events one upload carries: a batch's, or the one a single post sends."""
    body = json.loads(request.content)
    return body["events"] if request.url.path.endswith("/events:batch") else [body]


def test_a_control_plane_restart_mid_turn_does_not_lose_the_turn(monkeypatch):
    """Events refused for ten seconds are replayed; the turn still settles.

    A deploy recreates the control-plane container, so connections are refused
    for a while. That used to escape the stream after one immediate retry, and
    the turn failed with its native context marked lost.
    """
    clock = _virtual_clock(monkeypatch)
    delivered: list[dict[str, Any]] = []
    refused: list[int] = []

    def endpoint(request):
        uploaded = _uploaded_events(request)
        assistant = [body["event_seq"] for body in uploaded if body["event_type"] == "assistant"]
        if assistant and clock.now < 10:
            refused.extend(assistant)
            raise httpx.ConnectError("connection refused", request=request)
        delivered.extend(uploaded)
        return httpx.Response(201, json={"event_ids": list(range(len(uploaded)))})

    events = _event_control(endpoint)
    stream = _Stream(
        [
            {"kind": "event", "event_type": "assistant", "payload": {"text": "hi"}},
            {"kind": "result", "status": "completed", "exit_code": 0},
        ]
    )
    stream_turn = runner_mod._stream_shared_agent_turn

    def configure(control, supervisor, finish):
        control.append_event.side_effect = events.append_event
        control.flush.side_effect = events.flush
        control.lease_lost = False
        # The real finish delivers whatever the turn still has queued first.
        finish.side_effect = lambda *_args, **_kwargs: events.flush()
        supervisor.start_shared_turn.return_value = stream
        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream_turn)

    try:
        result, control, supervisor, finish = _run_shared_boundary_case(
            monkeypatch, 0, None, "claude-code", configure=configure
        )
    finally:
        events.close()

    assert result == 0
    finish.assert_called_once()
    assert finish.call_args.args[0] == "succeeded"
    supervisor.abandon_shared.assert_not_called()
    control.abandon_chat_sandbox.assert_not_called()
    assert len(refused) > 2 and clock.now >= 10
    # Every try carried the same number, so the owner's stream holds it once.
    assistant = [body for body in delivered if body["event_type"] == "assistant"]
    assert [body["event_seq"] for body in assistant] == [refused[0]]
    assert set(refused) == {refused[0]}
    assert [body["event_seq"] for body in delivered] == list(range(1, len(delivered) + 1))


def test_a_turn_whose_events_cannot_be_recorded_fails_instead_of_settling(monkeypatch):
    """Uploads run beside the stream now, and the bounded retry still ends the turn.

    The turn is not left running unseen, nor settled as if its reply had been
    recorded: its outcome waits for its events, and the failure is reported
    through the same sender before the attempt ends.
    """
    clock = _virtual_clock(monkeypatch)
    delivered: list[dict[str, Any]] = []

    def endpoint(request):
        uploaded = _uploaded_events(request)
        if any(body["event_type"] == "assistant" for body in uploaded):
            raise httpx.ConnectError("connection refused", request=request)
        delivered.extend(uploaded)
        return httpx.Response(201, json={"event_ids": list(range(len(uploaded)))})

    events = _event_control(endpoint)
    stream = _Stream(
        [
            {"kind": "event", "event_type": "assistant", "payload": {"text": "hi"}},
            {"kind": "result", "status": "completed", "exit_code": 0},
        ]
    )
    stream_turn = runner_mod._stream_shared_agent_turn

    def configure(control, supervisor, finish):
        control.append_event.side_effect = events.append_event
        control.flush.side_effect = events.flush
        control.lease_lost = False
        finish.side_effect = lambda *_args, **_kwargs: events.flush()
        supervisor.start_shared_turn.return_value = stream
        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream_turn)

    try:
        result, control, supervisor, finish = _run_shared_boundary_case(
            monkeypatch, 0, None, "claude-code", configure=configure
        )
    finally:
        events.close()

    assert result == 2
    finish.assert_called_once()
    assert finish.call_args.args[0] == "failed"
    assert finish.call_args.args[1].endswith("connection refused")
    supervisor.settle_shared.assert_not_called()
    control.settle_chat_sandbox.assert_not_called()
    assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is True
    assert clock.now <= control_plane_client._EVENT_RETRY_BUDGET_S + runner_mod.DEFAULT_LEASE_TTL_S
    assert delivered[-1]["event_type"] == "error"


@pytest.mark.parametrize(
    ("answers", "outcome", "requests"),
    [
        ([httpx.ConnectError("refused"), 502, 503, 201], None, 4),
        ([408, 429, 500, 504, 201], None, 5),
        ([409], LeaseLost, 1),
        ([422], httpx.HTTPStatusError, 1),
        ([401], httpx.HTTPStatusError, 1),
    ],
)
def test_event_upload_retries_only_what_a_replay_can_fix(monkeypatch, answers, outcome, requests):
    """Transient refusals back off and replay; a fence or a rejection does not."""
    clock = _virtual_clock(monkeypatch)
    pending = list(answers)
    bodies: list[dict[str, Any]] = []

    def endpoint(request):
        bodies.extend(_uploaded_events(request))
        answer = pending.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"event_ids": [1]})

    control = _event_control(endpoint)
    event = runner_mod.NormalizedEvent("assistant", {"text": "hi"})
    try:
        if outcome is None:
            control.append_event(event)
            control.flush()
        else:
            with pytest.raises(outcome):
                control.append_event(event)
                control.flush()
    finally:
        control.close()

    assert len(bodies) == requests
    assert all(body["event_seq"] == 1 for body in bodies)
    assert clock.sleeps == ([1.0, 2.0, 4.0, 8.0][: requests - 1] if outcome is None else [])


def test_event_upload_gives_up_inside_its_budget(monkeypatch):
    """A control plane that never answers ends the turn before the lease would."""
    clock = _virtual_clock(monkeypatch)
    tries: list[float] = []

    def endpoint(request):
        tries.append(clock.now)
        raise httpx.ConnectError("refused", request=request)

    control = _event_control(endpoint)
    try:
        with pytest.raises(httpx.ConnectError):
            control.append_event(runner_mod.NormalizedEvent("assistant", {"text": "hi"}))
            control.flush()
    finally:
        control.close()

    assert len(tries) > 2
    assert max(clock.sleeps) == control_plane_client._EVENT_RETRY_MAX_DELAY_S
    assert clock.now <= control_plane_client._EVENT_RETRY_BUDGET_S < runner_mod.DEFAULT_LEASE_TTL_S
