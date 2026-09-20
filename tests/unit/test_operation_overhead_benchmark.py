"""The startup-overhead benchmark must time the instrumentation, not beside it.

``scripts/bench_operation_overhead.py`` compares cohorts that differ only in the
profiler's configuration, so the cohort delta is the instrumentation's cost --
but only if the timed window actually contains the instrumentation. The first
version did not: the clock started after the opening mark and stopped before the
closing marks, the fact-setting, the freeze and the hand-off enqueue, so 150 ms
injected into a mark left the reported sample at 0.012 ms.

That is not a rounding error, it is the whole comparison silently measuring the
container and exempting what it claims to measure. These tests pin the window
against the same reproduction: a known delay inside the collector has to appear
in the duration the benchmark reports.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_agent_host.shared_session_supervisor import (
    SharedSessionDescriptor,
    SharedSessionLease,
)
from cloud_agent_profiling import operation_profiling as profiling

#: The script is not a package; it is loaded by path the way a runner would.
_BENCH_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bench_operation_overhead.py"


def _bench():
    """Import the benchmark module once, registered so its dataclasses resolve."""
    import importlib.util
    import sys

    if "bench_operation_overhead" in sys.modules:
        return sys.modules["bench_operation_overhead"]
    spec = importlib.util.spec_from_file_location("bench_operation_overhead", _BENCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_operation_overhead"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def enabled(monkeypatch):
    """One live experiment covering the startup operation for one job."""
    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "bench-window-test",
                "operations": ["attempt.startup"],
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                "max_operations": 50,
                "target": {"job_id": "ajob_window"},
            }
        ),
    )
    profiling.reset_cache()
    yield
    profiling.reset_cache()


class _Lease:
    """A supervisor answer shaped as the broker sends it."""

    def __init__(self, token: str) -> None:
        self._lease = SharedSessionLease(
            thread_id="athr_window",
            origin_thread_id="athr_window",
            sandbox_id="asbx_window",
            container_id="container-window",
            container_name="freeinference-window",
            token=token,
            activation_generation=1,
            process_generation=1,
            context_status="new",
            resident_state="stopped",
            resident_slot_id=0,
            container_action="create",
            stages=[{"stage": "container_create", "duration_us": 1}],
            profiling_call_id="",
            profiling_recorded=True,
        )

    def as_lease(self, call_id: str | None) -> SharedSessionLease:
        """Return the lease the supervisor would build for this call."""
        return SharedSessionLease(**{**self._lease.__dict__, "profiling_call_id": call_id})


class _Supervisor:
    """The two calls the runner makes, answering the profiling protocol."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def acquire(self, _spec, **kwargs):
        """Answer an acquire, echoing the call id the caller sent."""
        call_id = (kwargs.get("profiling") or {}).get("call_id")
        self.calls.append(call_id)
        return _Lease("token-window").as_lease(call_id)

    def ensure_resident(self, _thread_id, _token, _launch, profiling=None):
        """Answer a resident start, echoing the call id the caller sent."""
        call_id = (profiling or {}).get("call_id")
        self.calls.append(call_id)
        return SharedSessionDescriptor(
            thread_id="athr_window",
            origin_thread_id="athr_window",
            sandbox_id="asbx_window",
            generation=1,
            container_id="container-window",
            container_name="freeinference-window",
            docker_status="running",
            running=True,
            group_busy=False,
            busy=False,
            terminal_count=0,
            operation_count=0,
            snapshotting=False,
            activation_generation=1,
            process_generation=1,
            residency_generation=1,
            resident_state="live",
            resident_slot_id=0,
            last_settled_job_id=None,
            native_session_id="native-window",
            idle_deadline_monotonic=None,
            context_status="new",
            resident_action="new",
            stages=[{"stage": "resident_start", "duration_us": 1}],
            profiling_call_id=call_id,
            profiling_recorded=True,
            profiling_truncated=False,
        )


def _session() -> SimpleNamespace:
    """The session fields the measured path reads."""
    return SimpleNamespace(
        job_id="ajob_window",
        attempt_id=7,
        execution_profile="trusted-v1",
    )


def _activate(bench, supervisor, *, cohort: str):
    """Run one measured activation against a sandbox that does no container work."""
    return bench._measured_activation(
        SimpleNamespace(spec=object()),
        supervisor,
        cohort=cohort,
        session=_session(),
        on_frozen=None,
        expected_container_id=None,
        has_settled_context=False,
        native_session_id=None,
        activation_generation=1,
    )


def test_a_delay_inside_the_collector_appears_in_the_reported_duration(
    monkeypatch, enabled, tmp_path
):
    """The reproduction from review: 150 ms in a mark must not vanish.

    The window has to cover the marks, the fragment registrations, the freeze
    and the enqueue. Timing only the supervisor call reported 0.012 ms while
    150 ms of instrumentation ran inside it.
    """
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, str(tmp_path / "window.jsonl"))
    bench = _bench()
    delay_s = 0.05
    marks = {"count": 0}
    real_mark = profiling.mark

    def slow_mark(measurement, name):
        if measurement is not None:
            marks["count"] += 1
            time.sleep(delay_s)
        return real_mark(measurement, name)

    monkeypatch.setattr(profiling, "mark", slow_mark)
    try:
        elapsed_ns, container_action, resident_action, measurement = _activate(
            bench, _Supervisor(), cohort="on-local"
        )
    finally:
        profiling.flush_sink()
        profiling.close_sink()

    assert measurement is not None, "the experiment must have selected this startup"
    assert marks["count"] >= 2, "both stage marks must run while the clock is open"
    injected_s = marks["count"] * delay_s
    assert elapsed_ns / 1e9 >= injected_s, (
        f"the reported startup took {elapsed_ns / 1e9:.4f}s while {injected_s:.4f}s of "
        "instrumentation ran inside it: the timed window excludes the very cost the "
        "cohort comparison is about"
    )
    assert container_action == "create" and resident_action == "new"


def test_the_collector_work_is_inside_the_window_and_not_only_the_marks(
    monkeypatch, enabled, tmp_path
):
    """A delay in the freeze and the enqueue is timed too, not just the marks.

    The hand-off enqueue is a cost the runner pays before the prompt goes out,
    so a window that stopped at the last mark would still under-report it.
    """
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, str(tmp_path / "window2.jsonl"))
    bench = _bench()
    delay_s = 0.05
    frozen = {"called": 0}
    real_frozen_record = profiling.frozen_record

    def slow_frozen_record(measurement, outcome, **kwargs):
        frozen["called"] += 1
        time.sleep(delay_s)
        return real_frozen_record(measurement, outcome, **kwargs)

    def on_frozen(measurement):
        """Stand in for the hand-off enqueue, and take time like one."""
        slow_frozen_record(measurement, measurement.frozen_outcome or "incomplete")

    monkeypatch.setattr(profiling, "frozen_record", slow_frozen_record)
    try:
        elapsed_ns, _container, _resident, measurement = _activate(
            bench, _Supervisor(), cohort="on-persist"
        )
    finally:
        profiling.flush_sink()
        profiling.close_sink()

    assert measurement is not None
    assert elapsed_ns / 1e9 >= delay_s, (
        "the freeze and the hand-off enqueue must sit inside the timed window; the "
        "runner pays for both before the prompt is dispatched"
    )


def test_the_profiling_context_reaches_the_supervisor(monkeypatch, enabled, tmp_path):
    """The supervisor must be handed the context, and its answer must be recorded.

    Without the context the container manager takes its unprofiled branch, so an
    ``on`` cohort measures a startup that collected none of the container-side
    sub-stages the runner collects -- and the record says so, with
    ``missing_fragments`` and ``collection_complete: false``.
    """
    sink = tmp_path / "window3.jsonl"
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, str(sink))
    bench = _bench()
    supervisor = _Supervisor()
    try:
        _elapsed, _container, _resident, measurement = _activate(
            bench, supervisor, cohort="on-local"
        )
        assert measurement is not None
        assert supervisor.calls and all(supervisor.calls), (
            "every supervisor call must carry a profiling call id, or the broker "
            "cannot answer the fragment at all"
        )
        record = profiling.frozen_record(measurement, "dispatched")
    finally:
        profiling.flush_sink()
        profiling.close_sink()

    assert record is not None
    assert record["collection_complete"] is True
    assert "broker" in record["producers"], "the broker's answer must be recorded"
    assert not record.get("missing_fragments"), (
        "an answered fragment must not be reported missing: the earlier revision passed "
        "the supervisor's dataclass straight to record_fragment, which only accepts a "
        "mapping, so every record claimed an uncollected broker"
    )
    written = [json.loads(line) for line in sink.read_text().splitlines() if line.strip()]
    assert written and written[0]["operation"] == "attempt.startup"


def test_the_bare_cohort_runs_no_collector_at_all(monkeypatch, enabled, tmp_path):
    """`bare` is the floor: no measurement, so nothing to time but the container."""
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, str(tmp_path / "window4.jsonl"))
    bench = _bench()

    elapsed_ns, _container, _resident, measurement = _activate(bench, _Supervisor(), cohort="bare")

    assert measurement is None, "the bare cohort must not open a measurement"
    assert elapsed_ns > 0


def test_the_cpu_denominator_counts_every_request_in_the_window():
    """The per-record CPU figure must divide by what the CPU was spent on.

    The clock starts before warmup and stops after the last timed request, so
    the window contains both. Dividing by the timed requests alone overstated
    the per-record cost by iterations/(warmup+iterations) -- 13.3% at the
    defaults, which the review measured as 689 us where 608 us was right.
    """
    bench = _bench()
    args = SimpleNamespace(warmup=20, iterations=150)

    assert bench._requests_in_window(args, rounds=4) == 4 * 170 * len(bench.SERVER_OPERATIONS)
    # The denominator must be strictly larger than the timed-only count, or the
    # warmup requests are being charged to nothing.
    assert bench._requests_in_window(args, rounds=4) > 4 * 150 * len(bench.SERVER_OPERATIONS)
    assert bench._requests_in_window(
        SimpleNamespace(warmup=0, iterations=150), rounds=1
    ) == 150 * len(bench.SERVER_OPERATIONS)
