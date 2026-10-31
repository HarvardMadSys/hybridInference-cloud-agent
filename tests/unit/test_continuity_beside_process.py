"""A turn's continuity report goes beside its process record, not after it.

The report is best-effort observability, and the turn used to wait out its
round trip on its own once the process record's had ended. Neither endpoint
reads what the other writes, so the two are sent at once now. These pin what
must not move with it: what each says, both having landed before the turn
sends anything else, the record's own failure coming first, and a lease the
report found lost still reaching the turn.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from tests.unit.test_agent_runner import _run_shared_boundary_case
from tests.unit.test_event_sender import _WAIT_S, _control, _Plane
from tests.unit.test_runner_host_settings import _run_new_chat

from cloud_agent_host import control_plane_client
from cloud_agent_host.claimed_job import LeaseLost
from cloud_agent_shared import operation_profiling


@pytest.fixture(autouse=True)
def _reset_profiling_cache():
    """Keep the profiler's process-wide cache from leaking between tests."""
    operation_profiling.reset_cache()
    yield
    operation_profiling.reset_cache()


_PROCESS = {
    "sandbox_generation": 1,
    "activation_generation": 3,
    "residency_generation": 11,
    "process_generation": 2,
}


def _answering(*, process: int | None = None, continuity: int | None = None, delay_s: float = 0):
    """Answer the process record and the continuity report with the given statuses."""
    landed = threading.Event()

    def answer(request: httpx.Request) -> int | None:
        if request.url.path.endswith("/continuity"):
            time.sleep(delay_s)
            landed.set()
            return continuity
        return process

    return answer, landed


@pytest.mark.parametrize(
    ("continuity", "said"),
    [
        (("hot", "live"), {"mode": "hot", "context_status": "live"}),
        (("warm_native", "restored"), {"mode": "warm_native", "context_status": "restored"}),
        ((None, "new"), {"context_status": "new"}),
    ],
)
def test_the_report_and_the_record_are_in_flight_at_once(continuity, said):
    """Each waits on the wire for the other, which only one sent at a time never would."""
    both = threading.Barrier(2, timeout=_WAIT_S)

    def answer(_request: httpx.Request) -> None:
        both.wait()

    plane = _Plane(answer)
    control = _control(plane)
    try:
        control.record_chat_process(**_PROCESS, continuity=continuity)
    finally:
        control.close()

    assert sorted(plane.requests, key=lambda request: request[0]) == [
        ("continuity", said),
        ("sandbox/process", _PROCESS),
    ]


def test_a_record_without_a_report_is_sent_alone():
    plane = _Plane()
    control = _control(plane)
    try:
        control.record_chat_process(**_PROCESS)
    finally:
        control.close()

    assert plane.requests == [("sandbox/process", _PROCESS)]


@pytest.mark.parametrize("process", [None, 500])
def test_the_report_has_landed_before_the_record_returns(process):
    """Whatever the turn sends next, an abandon that marks its context lost included."""
    answer, landed = _answering(process=process, delay_s=0.2)
    control = _control(_Plane(answer))
    try:
        if process is None:
            control.record_chat_process(**_PROCESS, continuity=("hot", "live"))
        else:
            with pytest.raises(httpx.HTTPStatusError):
                control.record_chat_process(**_PROCESS, continuity=("hot", "live"))
        assert landed.is_set()
    finally:
        control.close()


def test_a_lease_the_report_found_lost_reaches_the_turn():
    answer, _landed = _answering(continuity=409)
    control = _control(_Plane(answer))
    try:
        with pytest.raises(LeaseLost):
            control.record_chat_process(**_PROCESS, continuity=("warm_native", "restored"))
    finally:
        control.close()


def test_the_record_s_own_failure_comes_first():
    """As when the report was sent only once the record had succeeded."""
    answer, _landed = _answering(process=500, continuity=409)
    control = _control(_Plane(answer))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            control.record_chat_process(**_PROCESS, continuity=("hot", "live"))
    finally:
        control.close()


@pytest.mark.parametrize("failure", [503, httpx.ConnectError("control plane restarting")])
def test_any_other_report_failure_leaves_the_turn_alone(failure):
    def answer(request: httpx.Request) -> int | None:
        if not request.url.path.endswith("/continuity"):
            return None
        if isinstance(failure, Exception):
            raise failure
        return failure

    control = _control(_Plane(answer))
    try:
        control.record_chat_process(**_PROCESS, continuity=(None, "new"))
    finally:
        control.close()


def test_a_process_out_of_threads_sends_the_report_after_the_record(monkeypatch):
    class Unstartable(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(control_plane_client.threading, "Thread", Unstartable)
    answer, _landed = _answering(continuity=409)
    plane = _Plane(answer)
    control = _control(plane)
    try:
        with pytest.raises(LeaseLost):
            control.record_chat_process(**_PROCESS, continuity=("hot", "live"))
    finally:
        control.close()

    assert plane.endpoints() == ["sandbox/process", "continuity"]


@pytest.mark.parametrize(
    ("resident_context", "continuity"),
    [("live", ("hot", "live")), ("restored", ("warm_native", "restored"))],
)
def test_a_follow_up_reports_its_rung_with_its_process_record(
    monkeypatch, resident_context, continuity
):
    def configure(_control, supervisor, _finish):
        start = supervisor.ensure_shared_resident.side_effect

        def resident(*args, **kwargs):
            return {**start(*args, **kwargs), "context_status": resident_context}

        supervisor.ensure_shared_resident.side_effect = resident

    result, control, _supervisor, _finish = _run_shared_boundary_case(
        monkeypatch, 0, None, "claude-code", configure=configure
    )

    assert result == 0
    assert control.record_chat_process.call_args.kwargs["continuity"] == continuity
    control.report_continuity.assert_not_called()
    control.report_context_status.assert_not_called()


def test_a_new_chat_reports_its_context_without_a_rung(monkeypatch):
    run = _run_new_chat(monkeypatch, None)

    assert run.result == 0
    assert run.control.record_chat_process.call_args.kwargs["continuity"] == (None, "new")
    run.control.report_continuity.assert_not_called()
    run.control.report_context_status.assert_not_called()
