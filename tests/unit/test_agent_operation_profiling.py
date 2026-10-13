"""Unit tests for the opt-in platform operation profiler.

The contract these tests defend is the one the design promises: off by default,
off cheaply, and a record only for a selected root operation that reached a
known boundary.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from cloud_agent_shared import operation_profiling as profiling


class _FakeClock:
    """A controllable stand-in for :mod:`time` inside the profiler."""

    def __init__(self, start_ns: int = 1_000_000_000_000) -> None:
        self.now_ns = start_ns

    def advance(self, micros: int) -> None:
        """Move the clock forward by ``micros`` microseconds."""
        self.now_ns += micros * 1000

    def monotonic_ns(self) -> int:
        """Return the frozen monotonic value."""
        return self.now_ns

    def monotonic(self) -> float:
        """Return the frozen monotonic value in seconds."""
        return self.now_ns / 1_000_000_000

    def sleep(self, seconds: float) -> None:
        """Yield to the export thread and move the clock like a real sleep."""
        time.sleep(0.001)
        self.now_ns += int(seconds * 1_000_000_000)

    def time(self) -> float:
        """Return the real wall clock: only the monotonic clock is faked."""
        return time.time()


@pytest.fixture(autouse=True)
def _clean_profiling_state(monkeypatch):
    """Every test starts from a process that has never read the switch."""
    monkeypatch.delenv(profiling.ENV_PROFILING_ENABLED, raising=False)
    monkeypatch.delenv(profiling.ENV_PROFILING_RUN, raising=False)
    monkeypatch.delenv(profiling.ENV_PROFILING_SINK, raising=False)
    profiling.reset_cache()
    yield
    profiling.reset_cache()


@pytest.fixture
def clock(monkeypatch):
    """Replace the profiler's clock so durations are deterministic."""
    fake = _FakeClock()
    monkeypatch.setattr(profiling, "time", fake)
    return fake


def _expiry(minutes: int = 60) -> str:
    """Return an ISO-8601 expiry that is still in the future."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _enable(monkeypatch, **overrides) -> dict:
    """Turn the switch on with a valid run descriptor."""
    run = {
        "run_id": "run-1",
        "operations": ["attempt.startup"],
        "expires_at": _expiry(),
        "max_operations": 10,
    }
    run.update(overrides)
    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(profiling.ENV_PROFILING_RUN, json.dumps(run))
    profiling.reset_cache()
    return run


def _records(caplog) -> list[dict]:
    """Return every emitted record captured from the log.

    Publishing is asynchronous -- the log line and the sink write both leave
    the attempt thread -- so reading the log means draining the exporter first.
    """
    profiling.flush_sink()
    found = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("agent_operation_profile "):
            found.append(json.loads(message.split(" ", 1)[1]))
    return found


def test_profiling_is_off_unless_the_switch_is_on(monkeypatch, caplog):
    """No switch means no collector, no id, and no record."""
    assert profiling.reason() == "not_enabled"
    assert profiling.active_run() is None
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None

    # Every helper tolerates the `None` the off path hands around.
    profiling.mark(None, "checkout")
    profiling.set_attributes(None, job_id="ajob_1")
    profiling.discard(None)
    profiling.finish(None, outcome="dispatched")
    with profiling.span(None, "claim"):
        pass
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, "/nonexistent/sink.jsonl")
    assert _records(caplog) == []


def test_switch_on_without_a_usable_run_stays_off(monkeypatch, caplog):
    """A malformed experiment must not disturb the business path."""
    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(profiling.ENV_PROFILING_RUN, "{not json")
    profiling.reset_cache()

    assert profiling.reason() == "not_enabled"
    assert profiling.start_operation(operation="attempt.startup") is None
    assert "continuing without profiling" in caplog.text


@pytest.mark.parametrize(
    "run",
    [
        {},
        {"run_id": "r"},
        {"run_id": "r", "operations": [], "expires_at": _expiry(), "max_operations": 5},
        {
            "run_id": "r",
            "operations": ["attempt.startup"],
            "expires_at": "soon",
            "max_operations": 5,
        },
        {
            "run_id": "r",
            "operations": ["attempt.startup"],
            "expires_at": _expiry(-5),
            "max_operations": 5,
        },
        {"run_id": "r", "operations": ["attempt.startup"], "expires_at": _expiry()},
        {
            "run_id": "r",
            "operations": ["attempt.startup"],
            "expires_at": _expiry(),
            "max_operations": 0,
        },
        {
            "run_id": "r",
            "operations": ["attempt.startup"],
            "expires_at": _expiry(),
            "max_operations": 5,
            # An unknown filter would silently never match, so it is refused
            # when the run is read rather than producing an empty experiment.
            "target": {"sandbxo_id": "typo"},
        },
    ],
)
def test_run_descriptor_requires_scope_expiry_and_a_cap(monkeypatch, run):
    """A run missing its scope, its expiry, or its cap does not take effect."""
    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(profiling.ENV_PROFILING_RUN, json.dumps(run))
    profiling.reset_cache()

    assert profiling.active_run() is None
    assert profiling.start_operation(operation="attempt.startup") is None


def test_only_listed_operations_are_selected(monkeypatch):
    """Selecting one root operation must not start collecting others."""
    _enable(monkeypatch, operations=["terminal.open"])

    assert profiling.start_operation(operation="attempt.startup") is None
    assert profiling.start_operation(operation="terminal.open") is not None


def test_target_host_is_applied_before_claim(monkeypatch):
    """A host-scoped experiment never measures another host's attempts."""
    _enable(monkeypatch, target={"host": "host-a"})

    assert profiling.start_operation(operation="attempt.startup", host="host-b") is None
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is not None


def test_bind_drops_a_measurement_that_falls_out_of_scope(monkeypatch, caplog):
    """Facts only known after claim can still exclude the attempt."""
    _enable(monkeypatch, target={"thread_id": "athr_wanted"})
    caplog.set_level(logging.INFO, logger=profiling.__name__)

    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert profiling.bind(measurement, thread_id="athr_other", job_id="ajob_1") is False
    profiling.discard(measurement)
    assert _records(caplog) == []

    kept = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert profiling.bind(kept, thread_id="athr_wanted", job_id="ajob_1") is True
    profiling.mark(kept, "validate")
    profiling.mark(kept, "reserve")
    profiling.finish(kept, outcome="dispatched")
    assert len(_records(caplog)) == 1


def test_a_target_field_that_is_missing_is_a_mismatch(monkeypatch, caplog):
    """Unknown is not a pass: a scoped run must match on every field."""
    _enable(monkeypatch, target={"sandbox_id": "asbx_wanted"})
    caplog.set_level(logging.INFO, logger=profiling.__name__)

    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    # Claims for a chat with no reserved sandbox report `sandbox_id=None`.
    assert profiling.bind(measurement, job_id="ajob_1", sandbox_id=None) is False
    profiling.discard(measurement)
    assert _records(caplog) == []

    kept = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert profiling.bind(kept, job_id="ajob_1", sandbox_id="asbx_wanted") is True


def test_a_scoped_measurement_that_was_never_bound_is_not_reported(monkeypatch, caplog):
    """A run that scopes itself must not report an unproven attempt."""
    _enable(monkeypatch, target={"job_id": "ajob_wanted"})
    caplog.set_level(logging.INFO, logger=profiling.__name__)

    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "validate")
    profiling.finish(measurement, outcome="dispatched")

    assert _records(caplog) == []
    assert "dropped an unbound measurement" in caplog.text


def test_an_unscoped_run_does_not_require_binding(monkeypatch, caplog):
    """Without a target there is nothing to prove, so the record is emitted."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)

    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "validate")
    profiling.finish(measurement, outcome="dispatched")

    assert len(_records(caplog)) == 1


def test_budget_limits_how_many_root_operations_are_selected(monkeypatch):
    """The cap is per process in this slice, and it is enforced."""
    _enable(monkeypatch, max_operations=2)

    assert profiling.start_operation(operation="attempt.startup") is not None
    assert profiling.start_operation(operation="attempt.startup") is not None
    assert profiling.start_operation(operation="attempt.startup") is None


def test_a_discarded_measurement_returns_its_budget_slot(monkeypatch):
    """An empty claim, or one outside the run's target, costs the run nothing."""
    _enable(monkeypatch, max_operations=1)

    empty_claim = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert empty_claim is not None
    profiling.discard(empty_claim)
    assert profiling.frozen_record(empty_claim, "incomplete") is None

    real_attempt = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert real_attempt is not None, "an empty claim must not consume the only slot"
    profiling.finish(real_attempt, outcome="dispatched")

    # A record that was actually produced does spend the slot.
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None


def test_a_finished_measurement_keeps_its_budget_slot(monkeypatch):
    """Committing a record is what spends the budget."""
    _enable(monkeypatch, max_operations=1)

    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None
    profiling.mark(measurement, "validate")
    profiling.finish(measurement, outcome="failed", failure_stage="checkout")

    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None


def test_storage_and_export_reuse_the_same_frozen_record(monkeypatch, caplog):
    """One measurement id must not acquire two observed_at values."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    stored = profiling.frozen_record(measurement, "dispatched")
    profiling.finish(measurement, outcome="dispatched")

    assert stored is not None
    assert stored["start_boundary"] == "runner.claim.request"
    assert stored["end_boundary"] == "supervisor.prompt.dispatched"
    assert _records(caplog) == [stored]
    assert profiling.frozen_record(measurement, "dispatched") is stored


def test_spans_record_nesting_and_duration(monkeypatch, caplog, clock):
    """Each span carries its own interval and its parent."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    with profiling.span(measurement, "claim"):
        clock.advance(1_500)
        with profiling.span(measurement, "db_claim"):
            clock.advance(400)
    profiling.finish(measurement, outcome="dispatched")

    spans = _records(caplog)[0]["spans"]
    # The outer span covers the inner one, so it is 1500 + 400 microseconds.
    assert [(item["name"], item["duration_us"], item["outcome"]) for item in spans] == [
        ("claim", 1_900, "ok"),
        ("db_claim", 400, "ok"),
    ]
    assert spans[0].get("parent_span_id") is None
    assert spans[1]["span_id"] != spans[0]["span_id"]
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]
    assert spans[0]["start_offset_us"] == 0
    assert spans[1]["start_offset_us"] == 1_500


def test_a_failed_span_is_kept_and_the_error_propagates(monkeypatch, caplog, clock):
    """A raising body still closes its span, marked as an error."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    with pytest.raises(RuntimeError), profiling.span(measurement, "checkout"):
        clock.advance(700)
        raise RuntimeError("clone failed")
    profiling.finish(measurement, outcome="failed", failure_stage="checkout")

    record = _records(caplog)[0]
    assert record["spans"][0]["outcome"] == "error"
    assert record["spans"][0]["duration_us"] == 700
    assert record["startup_outcome"] == "failed"
    assert record["failure_stage"] == "checkout"


def test_stages_are_derived_from_consecutive_marks(monkeypatch, caplog, clock):
    """Every stage gets a duration, including the one still running at the end."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    for name, advance in (
        ("validate", 100),
        ("reserve", 900),
        ("supervisor_acquire", 50),
        ("sandbox_running", 250),
    ):
        profiling.mark(measurement, name)
        clock.advance(advance)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert [(item["stage"], item["duration_us"]) for item in record["stages"]] == [
        ("validate", 100),
        ("reserve", 900),
        ("supervisor_acquire", 50),
        ("sandbox_running", 250),
    ]
    assert [item["name"] for item in record["milestones"]] == [
        "validate",
        "reserve",
        "supervisor_acquire",
        "sandbox_running",
    ]
    # The closing boundary is not a stage of its own.
    assert record["duration_us"] == 1_300


def test_the_stage_that_was_running_when_it_failed_keeps_its_duration(monkeypatch, caplog, clock):
    """A stage that ends by failing is measured, not dropped.

    Regression: durations came only from consecutive mark pairs, so the stage
    running at `finish()` had none -- and that is exactly the stage a slow
    startup is sitting in. A seven second checkout reported an empty timeline.
    """
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.mark(measurement, "validate")
    clock.advance(100)
    profiling.mark(measurement, "checkout")
    clock.advance(7_000_000)
    profiling.finish(measurement, outcome="failed", failure_stage="checkout")

    record = _records(caplog)[0]
    assert [(item["stage"], item["duration_us"]) for item in record["stages"]] == [
        ("validate", 100),
        ("checkout", 7_000_000),
    ]
    assert record["duration_us"] == 7_000_100
    assert record["failure_stage"] == "checkout"


def test_a_single_stage_is_still_measured(monkeypatch, caplog, clock):
    """One mark and a failure is a timeline, not an empty list."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.mark(measurement, "checkout")
    clock.advance(7_000_000)
    profiling.finish(measurement, outcome="failed", failure_stage="checkout")

    record = _records(caplog)[0]
    assert record["stages"] == [
        {"stage": "checkout", "start_offset_us": 0, "duration_us": 7_000_000}
    ]


def test_span_ceiling_drops_the_overflow_and_says_so(monkeypatch, caplog, clock):
    """A pathological attempt is truncated, not unbounded."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    for index in range(profiling.MAX_SPANS + 5):
        with profiling.span(measurement, f"step_{index}"):
            clock.advance(1)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert len(record["spans"]) == profiling.MAX_SPANS
    assert record["dropped_spans"] == 5
    assert record["measurement_complete"] is False


def test_record_reaches_the_log_and_the_jsonl_sink(monkeypatch, caplog, tmp_path):
    """The sink is what an external benchmark reads back."""
    sink = tmp_path / "profile.jsonl"
    _enable(monkeypatch)
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, str(sink))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.mark(measurement, "validate")
    profiling.mark(measurement, "reserve")
    profiling.set_attributes(measurement, job_id="ajob_1", startup_path="new_group")
    profiling.finish(measurement, outcome="dispatched")
    profiling.flush_sink()

    logged = _records(caplog)
    written = [json.loads(line) for line in sink.read_text().splitlines()]
    assert len(logged) == 1
    assert logged == written
    assert written[0]["facts"]["job_id"] == "ajob_1"
    assert written[0]["profiling_run_id"] == "run-1"
    assert written[0]["producer"] == "runner"
    assert written[0]["clock_domain_id"] == profiling.CLOCK_DOMAIN_ID
    assert written[0]["measurement_complete"] is True


def test_publishing_leaves_the_attempt_thread(monkeypatch, caplog, tmp_path):
    """Logging and the sink write both run elsewhere, so neither can stall startup.

    Regression: the log line stayed on the attempt thread, so a blocking log
    handler was charged to the startup the profile exists to explain.
    """
    sink = tmp_path / "profile.jsonl"
    _enable(monkeypatch)
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, str(sink))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    publishers: list[threading.Thread] = []
    real_publish = profiling._Exporter._publish

    def recording_publish(self, line, path):
        publishers.append(threading.current_thread())
        real_publish(self, line, path)

    monkeypatch.setattr(profiling._Exporter, "_publish", recording_publish)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.mark(measurement, "validate")
    profiling.finish(measurement, outcome="dispatched")
    profiling.flush_sink()

    assert publishers, "the record was never published"
    assert publishers[0] is not threading.current_thread()
    assert publishers[0].name == "operation-profile-sink"
    assert json.loads(sink.read_text().splitlines()[0])["measurement_id"]


def test_a_blocking_log_handler_does_not_stall_the_attempt(monkeypatch, caplog):
    """The attempt thread never waits on a log handler, however slow.

    Proved by the handler rather than by a tight clock: ``finish()`` returns
    while the handler it would have waited on is still blocked. The ceiling is
    a backstop for a bounded wait, set where scheduling noise on a loaded CI
    runner cannot reach it and the five-second block still fails.
    """
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    release = threading.Event()
    entered = threading.Event()
    returned = threading.Event()

    class BlockingHandler(logging.Handler):
        def emit(self, record):
            entered.set()
            release.wait(5)
            returned.set()

    blocker = BlockingHandler()
    profiling_logger = logging.getLogger(profiling.__name__)
    profiling_logger.addHandler(blocker)
    try:
        measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
        profiling.mark(measurement, "validate")
        started = time.monotonic()
        profiling.finish(measurement, outcome="dispatched")
        elapsed = time.monotonic() - started
        handler_returned_first = returned.is_set()
        handler_reached = entered.wait(5)
    finally:
        release.set()
        profiling_logger.removeHandler(blocker)
        profiling.flush_sink()

    assert handler_reached, "the record never reached the blocking handler"
    assert not handler_returned_first, "finish() waited for the blocked handler to return"
    assert elapsed < 1.0, f"finish() waited {elapsed:.3f}s on a blocked handler"


def test_profiling_off_never_starts_the_exporter(monkeypatch, caplog):
    """The default deployment pays for none of this, thread or file."""
    assert profiling.start_operation(operation="attempt.startup") is None
    profiling.flush_sink()
    profiling.close_sink()

    assert profiling._EXPORTER is None


def test_a_full_queue_drops_records_instead_of_blocking(monkeypatch, tmp_path, caplog):
    """A sink that cannot keep up loses records and says so, and never waits."""
    caplog.set_level(logging.WARNING, logger=profiling.__name__)
    release = threading.Event()
    entered = threading.Event()

    def stalled_write(self, line, sink):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(profiling._Exporter, "_publish", stalled_write)
    writer = profiling._Exporter(maxsize=4)
    try:
        writer.offer('{"warm":true}', None)
        # Wait until the writer thread holds that line and is parked in the
        # write, so the queue is provably empty and the count is deterministic.
        assert entered.wait(5)
        started = time.monotonic()
        for index in range(14):
            writer.offer(f'{{"n":{index}}}', None)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert elapsed < 1.0, "offering to a stalled sink must not block the caller"
    assert writer.dropped == 10
    writer.close(timeout=1.0)
    assert "dropped 10 record(s)" in caplog.text


def test_an_unwritable_sink_does_not_break_the_operation(monkeypatch, caplog, tmp_path):
    """Losing the export target must never fail the measured work."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    _enable(monkeypatch)
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, str(blocker / "profile.jsonl"))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.finish(measurement, outcome="dispatched")
    profiling.flush_sink()

    assert len(_records(caplog)) == 1
    assert "is unavailable" in caplog.text


def test_a_lapsed_run_stops_collecting_and_marks_the_record_partial(monkeypatch, caplog, clock):
    """Expiry is enforced while collecting, not only when a measurement starts.

    Regression: a measurement that started before the deadline kept recording
    past it and was still reported as a complete sample.
    """
    _enable(monkeypatch, expires_at=_expiry(minutes=1))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    profiling.mark(measurement, "validate")
    clock.advance(10_000)
    clock.advance(120 * 1_000_000)  # past the run's one-minute deadline

    profiling.mark(measurement, "reserve")
    profiling.set_attributes(measurement, job_id="ajob_1")
    with profiling.span(measurement, "late_span"):
        clock.advance(5_000)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert [item["name"] for item in record["milestones"]] == ["validate"]
    assert record["spans"] == []
    assert "job_id" not in record["facts"]
    assert record["measurement_complete"] is False
    assert record["facts"]["profiling_stopped"] == "expired"
    # What was collected before the deadline is reported, and nothing after it:
    # the window closes at the deadline, not at the moment finish() was called.
    deadline_us = measurement.deadline_offset_us()
    assert record["stages"] == [
        {"stage": "validate", "start_offset_us": 0, "duration_us": deadline_us}
    ]
    assert record["duration_us"] == deadline_us
    assert deadline_us < 120_000_000


def test_a_claim_returning_after_expiry_keeps_a_partial_attributed_sample(
    monkeypatch, caplog, clock
):
    """A claim selected while live is not erased when it crosses the deadline."""
    _enable(monkeypatch, expires_at=_expiry(minutes=1), target={"job_id": "ajob_1"})
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup")
    assert measurement is not None
    with profiling.span(measurement, "claim"):
        clock.advance(120 * 1_000_000)

    assert profiling.bind(measurement, job_id="ajob_1") is True
    profiling.finish(
        measurement,
        outcome="incomplete",
        failure_stage="expired_before_handoff",
    )

    record = _records(caplog)[0]
    assert record["facts"]["job_id"] == "ajob_1"
    assert record["collection_complete"] is False
    assert record["facts"]["profiling_stopped"] == "expired"


def test_finishing_after_the_deadline_is_partial_even_with_no_late_calls(
    monkeypatch, caplog, clock
):
    """A measurement that outlives its run is partial, whatever it recorded."""
    _enable(monkeypatch, expires_at=_expiry(minutes=1))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    profiling.mark(measurement, "validate")
    clock.advance(120 * 1_000_000)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["measurement_complete"] is False
    assert record["facts"]["profiling_stopped"] == "expired"


def test_a_measurement_inside_its_deadline_stays_complete(monkeypatch, caplog, clock):
    """The partial marking must not leak onto healthy samples."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    profiling.mark(measurement, "validate")
    clock.advance(5_000)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["measurement_complete"] is True
    assert "profiling_stopped" not in record["facts"]


def test_finish_and_discard_are_idempotent(monkeypatch, caplog):
    """A path that closes twice must not double-count."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.finish(measurement, outcome="dispatched")
    profiling.finish(measurement, outcome="failed")
    profiling.discard(measurement)

    assert len(_records(caplog)) == 1
    assert _records(caplog)[0]["startup_outcome"] == "dispatched"


def test_the_environment_is_read_once_per_process(monkeypatch, caplog):
    """The off path never re-parses the switch, so a later edit is inert."""
    assert profiling.reason() == "not_enabled"

    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "run-1",
                "operations": ["attempt.startup"],
                "expires_at": _expiry(),
                "max_operations": 1,
            }
        ),
    )
    assert profiling.reason() == "not_enabled"

    profiling.reset_cache()
    assert profiling.reason() == "active"


def test_expiry_stops_new_operations_without_touching_the_rest(monkeypatch, clock):
    """A lapsed run does not extend itself and does not fail the caller."""
    _enable(monkeypatch, expires_at=_expiry(minutes=1))

    assert profiling.start_operation(operation="attempt.startup") is not None
    clock.advance(120 * 1_000_000)
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None


def test_remote_stages_hang_off_the_stage_they_belong_to(monkeypatch, caplog, clock):
    """Another producer's durations are attached, attributed and bounded."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.mark(measurement, "supervisor_acquire")
    clock.advance(900_000)
    profiling.mark(measurement, "sandbox_running")
    clock.advance(1_000)
    attached = profiling.attach_remote_stages(
        measurement,
        "supervisor_acquire",
        [
            {"name": "container_start", "duration_us": 690_000, "outcome": "ok"},
            {"name": "service_hosts_sync", "duration_us": 1_200},
        ],
        source="broker",
    )
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    acquire = next(item for item in record["stages"] if item["stage"] == "supervisor_acquire")
    other = next(item for item in record["stages"] if item["stage"] == "sandbox_running")
    assert attached is True
    assert acquire["children"] == [
        {"stage": "container_start", "duration_us": 690_000, "outcome": "ok", "source": "broker"},
        {"stage": "service_hosts_sync", "duration_us": 1_200, "outcome": "ok", "source": "broker"},
    ]
    # Durations from another clock domain are never given a local offset.
    assert all("start_offset_us" not in child for child in acquire["children"])
    assert "children" not in other
    assert record["producers"] == ["runner", "broker"]


def test_remote_stages_are_bounded_and_untrusted(monkeypatch, caplog):
    """The list crosses a process boundary, so malformed entries are dropped."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "supervisor_acquire")
    profiling.mark(measurement, "sandbox_running")
    garbage = [
        {"name": "container_start", "duration_us": 5},
        {"name": "", "duration_us": 5},
        {"duration_us": 5},
        {"name": "no_duration"},
        {"name": "bad_duration", "duration_us": "soon"},
        "not a mapping",
        {"name": "negative", "duration_us": -9},
    ]
    garbage.extend(
        {"name": f"flood_{index}", "duration_us": 1}
        for index in range(profiling.MAX_REMOTE_STAGES * 2)
    )

    assert profiling.attach_remote_stages(
        measurement, "supervisor_acquire", garbage, source="broker"
    )
    profiling.finish(measurement, outcome="dispatched")

    children = _records(caplog)[0]["stages"][0]["children"]
    names = [child["stage"] for child in children]
    assert "container_start" in names
    assert "" not in names and "no_duration" not in names and "bad_duration" not in names
    # A negative duration is a clock artefact, never a reported value.
    assert all(child["duration_us"] >= 0 for child in children)
    assert len(children) <= profiling.MAX_REMOTE_STAGES


def test_a_clean_flood_is_truncated_at_the_ceiling(monkeypatch, caplog):
    """The ceiling bounds the record, not just the parse."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "supervisor_acquire")
    profiling.mark(measurement, "sandbox_running")
    flood = [
        {"name": f"step_{index}", "duration_us": 1}
        for index in range(profiling.MAX_REMOTE_STAGES * 3)
    ]

    assert profiling.attach_remote_stages(measurement, "supervisor_acquire", flood, source="broker")
    profiling.finish(measurement, outcome="dispatched")

    children = _records(caplog)[0]["stages"][0]["children"]
    assert len(children) == profiling.MAX_REMOTE_STAGES


def test_remote_stages_need_a_stage_to_attach_to(monkeypatch, caplog):
    """An empty or malformed report is not a partial timeline, it is nothing."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    assert profiling.attach_remote_stages(measurement, "x", [], source="broker") is False
    assert profiling.attach_remote_stages(measurement, "x", None, source="broker") is False
    assert profiling.attach_remote_stages(measurement, "x", "oops", source="broker") is False
    assert profiling.attach_remote_stages(None, "x", [{"name": "a"}], source="broker") is False
    assert measurement.contributors == set()


def test_a_substage_observer_records_against_the_current_stage(monkeypatch, caplog, clock):
    """An in-process caller times its own sub-stages without the profiler API."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "agent_network")
    observer = profiling.stage_observer(measurement)

    with profiling.substage(observer, "bootstrap_guest_stop"):
        clock.advance(400)
    with profiling.substage(observer, "bootstrap_guest_start"):
        clock.advance(600)
    profiling.finish(measurement, outcome="dispatched")

    network = next(
        item for item in _records(caplog)[0]["stages"] if item["stage"] == "agent_network"
    )
    assert [(child["stage"], child["duration_us"]) for child in network["children"]] == [
        ("bootstrap_guest_stop", 400),
        ("bootstrap_guest_start", 600),
    ]
    assert all(child["source"] == "runner" for child in network["children"])


def test_a_stage_recorder_collects_durations_without_a_measurement(monkeypatch):
    """The sandbox manager's recorder is the same protocol without a local timeline."""
    live = {
        "run_id": "run-1",
        "measurement_id": "msm_x",
        "operation": "attempt.startup",
        "expires_at": time.time() + 60,
    }
    assert profiling.start_stage_recording(live) is None, "an off process records nothing"

    _enable(monkeypatch)
    recorder = profiling.start_stage_recording(live)
    assert recorder is not None
    with profiling.substage(recorder, "container_create"):
        pass
    with pytest.raises(RuntimeError), profiling.substage(recorder, "container_start"):
        raise RuntimeError("docker refused")

    assert [item["name"] for item in recorder.stages] == ["container_create", "container_start"]
    assert recorder.names() == ["container_create", "container_start"]
    assert [item["outcome"] for item in recorder.stages] == ["ok", "error"]


def test_a_recorder_inherits_the_callers_selection(monkeypatch, clock):
    """The sandbox manager does not decide for itself whether an attempt was selected."""
    _enable(monkeypatch, run_id="run-1")
    live = {
        "run_id": "run-1",
        "measurement_id": "msm_x",
        "operation": "attempt.startup",
        "expires_at": time.time() + 60,
    }

    assert profiling.start_stage_recording(live) is not None
    # A different run, an operation this run does not select, and a selection
    # that has already lapsed are all refused.
    assert profiling.start_stage_recording({**live, "run_id": "other"}) is None
    assert profiling.start_stage_recording({**live, "operation": "terminal.open"}) is None
    assert profiling.start_stage_recording({**live, "expires_at": time.time() - 1}) is None
    assert profiling.start_stage_recording({**live, "expires_at": "soon"}) is None
    assert profiling.start_stage_recording({**live, "expires_at": None}) is None
    assert profiling.start_stage_recording("not a mapping") is None
    # No inherited selection means "not selected", never "record anyway".
    assert profiling.start_stage_recording(None) is None
    assert profiling.start_stage_recording({}) is None
    assert profiling.start_stage_recording(live) is not None


def test_a_recorder_stops_at_the_deadline_it_inherited(monkeypatch, clock):
    """A sandbox manager must not measure past the experiment the runner stopped."""
    _enable(monkeypatch)
    recorder = profiling.start_stage_recording(
        {
            "run_id": "run-1",
            "measurement_id": "msm_x",
            "operation": "attempt.startup",
            "expires_at": time.time() + 1,
        }
    )
    assert recorder is not None

    with profiling.substage(recorder, "early"):
        clock.advance(1_000)
    clock.advance(2 * 1_000_000)  # past the inherited deadline

    with profiling.substage(recorder, "late"):
        clock.advance(5_000)

    assert recorder.names() == ["early"]
    assert recorder.expired() is True


def test_a_substage_that_crosses_the_deadline_is_truncated(monkeypatch, caplog, clock):
    """Children end where the root ends, so the record is internally consistent.

    Regression: the root record was truncated at the deadline while the stage
    inside it kept its full two minutes, so a reader saw a child longer than
    its parent.
    """
    _enable(monkeypatch, expires_at=_expiry(minutes=1))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None
    profiling.mark(measurement, "agent_network")
    observer = profiling.stage_observer(measurement)

    with profiling.substage(observer, "bootstrap_guest_stop"):
        clock.advance(120 * 1_000_000)  # the deadline lands inside this stage
    # A sub-stage that only starts after the deadline is not recorded at all.
    with profiling.substage(observer, "bootstrap_guest_start"):
        clock.advance(1_000)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    network = next(item for item in record["stages"] if item["stage"] == "agent_network")
    assert [item["stage"] for item in network["children"]] == ["bootstrap_guest_stop"]
    child = network["children"][0]
    assert child["truncated"] is True
    assert child["duration_us"] <= record["duration_us"]
    assert network["duration_us"] <= record["duration_us"]
    assert record["measurement_complete"] is False


def test_a_span_that_crosses_the_deadline_is_truncated(monkeypatch, caplog, clock):
    """The same rule for a plain span."""
    _enable(monkeypatch, expires_at=_expiry(minutes=1))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    with profiling.span(measurement, "claim"):
        clock.advance(120 * 1_000_000)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["spans"][0]["truncated"] is True
    assert record["spans"][0]["duration_us"] <= record["duration_us"]


def test_a_missing_producer_makes_the_record_incomplete(monkeypatch, caplog):
    """A path that asked for a fragment and did not get one is not complete."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "supervisor_acquire")
    profiling.mark(measurement, "sandbox_running")

    assert profiling.expect_fragment(measurement, "broker", "supervisor_acquire") == "c1"
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["producers"] == ["runner"]
    assert record["missing_fragments"] == ["broker:c1:supervisor_acquire"]
    assert record["missing_producers"] == ["broker"]
    assert record["measurement_complete"] is False


def test_an_answered_producer_completes_the_record(monkeypatch, caplog):
    """The same path, with the sandbox manager answering, is complete."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "supervisor_acquire")
    profiling.mark(measurement, "sandbox_running")

    call = profiling.expect_fragment(measurement, "broker", "supervisor_acquire")
    assert profiling.record_fragment(
        measurement,
        producer="broker",
        call_id=call,
        stage="supervisor_acquire",
        response={
            "profiling_call_id": call,
            "profiling_recorded": True,
            "stages": [{"name": "container_start", "duration_us": 1}],
        },
    )
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["producers"] == ["runner", "broker"]
    assert "missing_producers" not in record
    assert record["measurement_complete"] is True


def test_an_expired_measurement_offers_no_selection_to_inherit(monkeypatch, clock):
    """A lapsed measurement must not hand the sandbox manager something to record under."""
    _enable(monkeypatch, expires_at=_expiry(minutes=1))
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    clock.advance(120 * 1_000_000)

    assert profiling.measurement_context(measurement) is None


def test_measurement_context_carries_the_selection_and_a_wall_clock_expiry(monkeypatch, clock):
    """Two processes share no monotonic clock, so the deadline travels absolute."""
    _enable(monkeypatch)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    context = profiling.measurement_context(measurement)

    assert context["run_id"] == "run-1"
    assert context["operation"] == "attempt.startup"
    assert context["measurement_id"] == measurement.measurement_id
    assert context["expires_at"] > time.time()
    assert profiling.measurement_context(None) is None
    profiling.finish(measurement, outcome="dispatched")
    assert profiling.measurement_context(measurement) is None


def test_one_answered_fragment_does_not_cover_a_missing_one(monkeypatch, caplog):
    """Completeness is per fragment, not per producer.

    Regression: an acquire that answered made the record complete even though
    the resident call's timing never arrived.
    """
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "supervisor_acquire")
    profiling.mark(measurement, "sandbox_running")
    profiling.mark(measurement, "resident_start")
    profiling.mark(measurement, "prompt_admission")

    acquire = profiling.expect_fragment(measurement, "broker", "supervisor_acquire")
    profiling.expect_fragment(measurement, "broker", "resident_start")
    profiling.record_fragment(
        measurement,
        producer="broker",
        call_id=acquire,
        stage="supervisor_acquire",
        response={
            "profiling_call_id": acquire,
            "profiling_recorded": True,
            "stages": [{"name": "container_start", "duration_us": 10}],
        },
    )
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["producers"] == ["runner", "broker"]
    assert record["missing_fragments"] == ["broker:c2:resident_start"]
    assert record["missing_producers"] == ["broker"]
    assert record["measurement_complete"] is False


def test_a_truncated_remote_fragment_is_not_a_complete_record(monkeypatch, caplog):
    """The producer's truncation has to survive the process boundary.

    Regression: the sandbox manager stopped at its deadline, the runner dropped the
    flag, and the record still claimed completeness.
    """
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "supervisor_acquire")
    profiling.mark(measurement, "sandbox_running")

    call = profiling.expect_fragment(measurement, "broker", "supervisor_acquire")
    assert profiling.record_fragment(
        measurement,
        producer="broker",
        call_id=call,
        stage="supervisor_acquire",
        response={
            "profiling_call_id": call,
            "profiling_recorded": True,
            "stages": [{"name": "container_start", "duration_us": 2_000_000, "truncated": True}],
        },
    )
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    child = record["stages"][0]["children"][0]
    assert child["truncated"] is True
    assert record["truncated"] is True
    assert record["measurement_complete"] is False
    # The fragment arrived, so nothing is attributed to a missing producer.
    assert "missing_fragments" not in record


def test_a_truncated_local_span_is_not_a_complete_record(monkeypatch, caplog, clock):
    """The local equivalent, for the same reason: a floor is not a duration."""
    _enable(monkeypatch, expires_at=_expiry(minutes=1))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    with profiling.span(measurement, "claim"):
        clock.advance(120 * 1_000_000)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["truncated"] is True
    assert record["measurement_complete"] is False


def test_an_over_long_run_id_disables_profiling_instead_of_failing_a_startup(monkeypatch, caplog):
    """A measurement setting must never be able to reject a sandbox request.

    Regression: the descriptor accepted a 129 character run id while the sandbox manager
    request model capped it at 128, so the same acquire returned 422 once the
    runner attached its selection.
    """
    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "r" * (profiling.MAX_RUN_ID_CHARS + 1),
                "operations": ["attempt.startup"],
                "expires_at": _expiry(),
                "max_operations": 5,
            }
        ),
    )
    profiling.reset_cache()

    assert profiling.active_run() is None
    assert profiling.start_operation(operation="attempt.startup") is None
    assert "continuing without profiling" in caplog.text


def test_a_run_id_at_the_limit_is_accepted(monkeypatch):
    """The bound is inclusive, so the rule is a limit and not an off-by-one."""
    _enable(monkeypatch, run_id="r" * profiling.MAX_RUN_ID_CHARS)

    assert profiling.active_run() is not None


def test_a_run_id_that_cannot_be_an_admin_path_is_rejected(monkeypatch, caplog):
    """A valid descriptor must have a reachable Admin summary URL."""
    _enable(monkeypatch, run_id="run/one")
    caplog.set_level(logging.WARNING, logger=profiling.__name__)

    assert profiling.start_operation(operation="attempt.startup") is None
    assert "run_id" in caplog.text


def test_the_parsed_run_preserves_manifest_identity_and_its_local_budget(monkeypatch):
    """Storage sees the same config version and budget the selector enforced."""
    _enable(monkeypatch, config_version=2, max_operations=77)

    manifest = profiling.manifest_for_run(profiling.active_run())

    assert manifest is not None
    assert manifest["config_version"] == 2
    assert manifest["max_operations"] == 77
    assert {
        "instance": profiling.PRODUCER_INSTANCE_ID,
        "max_operations": 77,
    } in manifest["admission_instances"]


def test_a_child_inherits_the_parents_claim_span_and_admission(monkeypatch):
    """The child extends one startup; it does not invent a fresh budget owner."""
    _enable(monkeypatch, max_operations=7)
    parent = profiling.start_operation(operation="attempt.startup")
    assert parent is not None
    with profiling.span(parent, "claim"):
        pass

    context = profiling.handoff_context(parent)
    assert context is not None
    context["admission_instances"] = [{"instance": "parent-process", "max_operations": 7}]
    child = profiling.attach_selection(context)

    assert child is not None
    assert [span.name for span in child.spans] == ["claim"]
    assert child.started_mono_ns == parent.started_mono_ns
    assert child.producer_instance_id == parent.producer_instance_id
    assert child.clock_domain_id == parent.clock_domain_id
    manifest = profiling.manifest_for_measurement(child)
    assert manifest is not None
    assert manifest["admission_instances"] == [
        {
            "instance": "parent-process",
            "max_operations": 7,
        }
    ]


def test_an_exporter_that_cannot_start_drops_measurements_without_failing(monkeypatch, caplog):
    """A process that cannot start a thread still serves traffic.

    Regression: `Thread.start()` raising propagated out of `finish()`, at a
    point where the prompt had already been sent.
    """
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    profiling.close_sink()

    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(profiling.threading.Thread, "start", refuse)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "validate")

    profiling.finish(measurement, outcome="dispatched")  # must not raise
    profiling.flush_sink()  # must not hang or raise

    assert profiling._EXPORTER is not None
    assert profiling._EXPORTER.disabled is True
    assert profiling._EXPORTER.dropped == 1
    assert "could not start its exporter" in caplog.text


def test_an_answer_with_no_stages_is_a_complete_fragment(monkeypatch, caplog):
    """A call that legitimately did no timed work still answered.

    Regression: an empty stage list was read as "never heard from the sandbox manager",
    which marked every normal CLI reuse incomplete.
    """
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "resident_start")

    call = profiling.expect_fragment(measurement, "broker", "resident_start")
    assert profiling.record_fragment(
        measurement,
        producer="broker",
        call_id=call,
        stage="resident_start",
        response={"profiling_call_id": call, "profiling_recorded": True, "stages": []},
    )
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert "missing_fragments" not in record
    assert record["measurement_complete"] is True
    resident = next(item for item in record["stages"] if item["stage"] == "resident_start")
    assert "children" not in resident


def test_a_response_for_another_call_is_not_an_answer(monkeypatch, caplog):
    """The echo is what binds an answer to a call, so it cannot be skipped."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "resident_start")

    call = profiling.expect_fragment(measurement, "broker", "resident_start")
    for response in (
        {"profiling_recorded": True, "stages": [{"name": "cli_spawn", "duration_us": 1}]},
        {"profiling_call_id": "c99", "profiling_recorded": True, "stages": []},
        {"profiling_call_id": call, "profiling_recorded": False, "stages": []},
        "not a mapping",
    ):
        assert (
            profiling.record_fragment(
                measurement,
                producer="broker",
                call_id=call,
                stage="resident_start",
                response=response,
            )
            is False
        )
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["missing_fragments"] == ["broker:c1:resident_start"]
    assert record["measurement_complete"] is False


def test_a_recorder_that_lapses_mid_call_reports_that_it_stopped(monkeypatch, clock):
    """A skipped stage leaves no trace, so the recorder has to say so itself.

    Regression: `profiling_recorded` only meant "a recorder existed". When the
    sandbox manager's deadline passed while it waited for the member lock, every later
    `substage` was skipped silently and the answer looked like a call that had
    nothing to time.
    """
    _enable(monkeypatch)
    recorder = profiling.start_stage_recording(
        {
            "run_id": "run-1",
            "measurement_id": "msm_x",
            "operation": "attempt.startup",
            "call_id": "c1",
            "expires_at": time.time() + 1,
        }
    )
    assert recorder is not None
    assert recorder.stopped_early() is False

    with profiling.substage(recorder, "container_start"):
        clock.advance(500)
    clock.advance(2 * 1_000_000)  # the deadline passes while the lock is held

    with profiling.substage(recorder, "cli_spawn"):
        clock.advance(500)

    assert [item["name"] for item in recorder.stages] == ["container_start"]
    assert recorder.skipped == 1
    assert recorder.stopped_early() is True


def test_a_truncated_but_unanswered_call_keeps_the_stages_it_measured(monkeypatch, caplog, clock):
    """The answer arrived, collection stopped early, and both facts are kept."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "resident_start")

    call = profiling.expect_fragment(measurement, "broker", "resident_start")
    assert profiling.record_fragment(
        measurement,
        producer="broker",
        call_id=call,
        stage="resident_start",
        response={
            "profiling_call_id": call,
            "profiling_recorded": True,
            # Collected, but the deadline cut it short: no stages came back at
            # all, which is not the same as a stage that had nothing to do.
            "profiling_truncated": True,
            "stages": [],
        },
    )
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["truncated"] is True
    assert record["truncated_fragments"] == ["broker:c1"]
    # Answered, so it is not a missing fragment; it is a partial one.
    assert "missing_fragments" not in record
    assert record["measurement_complete"] is False


def test_a_truncated_answer_still_contributes_the_stages_it_measured(monkeypatch, caplog):
    """Partial data is reported, not discarded, when the producer stopped early."""
    _enable(monkeypatch)
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    profiling.mark(measurement, "supervisor_acquire")
    profiling.mark(measurement, "sandbox_running")

    call = profiling.expect_fragment(measurement, "broker", "supervisor_acquire")
    profiling.record_fragment(
        measurement,
        producer="broker",
        call_id=call,
        stage="supervisor_acquire",
        response={
            "profiling_call_id": call,
            "profiling_recorded": True,
            "profiling_truncated": True,
            "stages": [{"name": "container_start", "duration_us": 900}],
        },
    )
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    children = record["stages"][0]["children"]
    assert [child["stage"] for child in children] == ["container_start"]
    assert record["truncated_fragments"] == ["broker:c1"]
    assert record["measurement_complete"] is False


# ---------------------------------------------------------------------------
# One collector, more than one producer
# ---------------------------------------------------------------------------


def test_a_v1_reader_can_still_find_a_startups_outcome(monkeypatch, caplog):
    """The startup alias survives, so records already written keep their name."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["schema_version"] == 3
    assert record["outcome"] == "dispatched"
    assert record["startup_outcome"] == "dispatched"
    assert record["producer"] == "runner"
    assert record["facts"]["outcome"] == "dispatched"


def test_a_file_read_is_never_reported_as_a_startup(monkeypatch, caplog):
    """A record names its own producer and its own endpoint, not the runner's."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch, operations=["files.open", "attempt.startup"])
    measurement = profiling.start_operation(operation="files.open", producer="control-plane")
    assert profiling.bind(measurement, job_id="ajob_1", sandbox_id="asbx_1")

    profiling.mark(measurement, "read")
    profiling.finish(measurement, outcome="completed")

    record = _records(caplog)[0]
    assert record["operation"] == "files.open"
    assert record["producer"] == "control-plane"
    assert record["producers"] == ["control-plane"]
    assert record["outcome"] == "completed"
    assert record["milestones"] == [
        {
            "name": "read",
            "offset_us": record["milestones"][0]["offset_us"],
            "source": "control-plane",
        }
    ]
    # `startup_outcome` is a claim about a startup, and this was not one.
    assert "startup_outcome" not in record
    assert "startup_outcome" not in record["facts"]


def test_a_root_producer_can_time_its_own_sub_stages(monkeypatch, caplog):
    """A control-plane stage is reported in the sandbox manager's own shape and source."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch, operations=["files.list"])
    measurement = profiling.start_operation(operation="files.list", producer="control-plane")
    assert profiling.bind(measurement, job_id="ajob_1")

    profiling.mark(measurement, "list")
    with profiling.attributed_stage(measurement, "list", "sandbox_manager_call"):
        pass
    profiling.finish(measurement, outcome="completed")

    record = _records(caplog)[0]
    children = record["stages"][0]["children"]
    assert [child["stage"] for child in children] == ["sandbox_manager_call"]
    assert children[0]["source"] == "control-plane"
    assert children[0]["outcome"] == "ok"


def test_a_local_sub_stage_is_attributed_to_its_own_producer(monkeypatch, caplog):
    """A shared observer must not file a sandbox manager's work under the runner."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch, operations=["files.list"])
    measurement = profiling.start_operation(operation="files.list", producer="control-plane")
    assert profiling.bind(measurement, job_id="ajob_1")

    observer = profiling.stage_observer(measurement, producer="broker")
    assert observer is not None
    with observer.stage("admission"):
        pass
    profiling.finish(measurement, outcome="completed")

    record = _records(caplog)[0]
    # No mark was ever placed, so the sub-stage hangs under the producer's own
    # name -- never under "startup", which would be a false claim.
    # The record names the producer that rooted the operation, the observer's
    # work is attributed to the producer that did it, and a measurement that
    # placed no mark still has one stage -- the operation's whole span -- for
    # that work to land on.
    assert record["producer"] == "control-plane"
    assert record["producers"] == ["control-plane", "broker"]
    assert record["timing_sources"] == ["control-plane", "broker"]
    stage = record["stages"][0]
    assert stage["stage"] == "files.list"
    assert stage["start_offset_us"] == 0
    assert [child["source"] for child in stage["children"]] == ["broker"]


def test_a_context_round_trips_through_its_wire_form(monkeypatch):
    """What one producer writes is exactly what the next one reads."""
    _enable(monkeypatch, operations=["files.open"])
    measurement = profiling.start_operation(operation="files.open", producer="control-plane")
    assert profiling.bind(measurement, job_id="ajob_1", host="host-a")
    call = profiling.expect_fragment(measurement, "broker", "read")

    encoded = json.dumps(profiling.measurement_context(measurement, call))
    decoded = profiling.parse_context(encoded)

    assert decoded is not None
    assert decoded["run_id"] == "run-1"
    assert decoded["measurement_id"] == measurement.measurement_id
    assert decoded["operation"] == "files.open"
    assert decoded["call_id"] == call
    assert decoded["expires_at"] == pytest.approx(time.time() + 3600, abs=120)


def test_an_unusable_context_is_refused_rather_than_guessed(monkeypatch):
    """Measurement metadata never becomes a reason to measure something else."""
    _enable(monkeypatch, operations=["files.open"])
    assert profiling.parse_context(None) is None
    assert profiling.parse_context("") is None
    assert profiling.parse_context("not json") is None
    assert profiling.parse_context(json.dumps([1, 2])) is None
    assert profiling.parse_context(json.dumps({"run_id": "run-1"})) is None
    assert profiling.parse_context("x" * (profiling.MAX_CONTEXT_CHARS + 1)) is None
    # Decodable, but not a run this process is experimenting with.
    assert (
        profiling.start_stage_recording(
            {"run_id": "other-run", "operation": "files.open", "expires_at": time.time() + 60}
        )
        is None
    )
    # Decodable, in the run, but for an operation this run does not select.
    assert (
        profiling.start_stage_recording(
            {"run_id": "run-1", "operation": "sandbox.wake", "expires_at": time.time() + 60}
        )
        is None
    )


def test_a_record_states_the_boundaries_and_coverage_it_reached(monkeypatch, caplog):
    """A reader must not have to infer how far a measurement reaches."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch, operations=["attempt.startup"])
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["schema_version"] == 3
    # The runner's boundary starts before the claim and ends at prompt
    # handoff: a host startup, not the user's wait.
    assert record["coverage_scope"] == "host_startup"
    assert record["boundary_version"] == profiling.BOUNDARY_VERSION
    assert record["end_to_end_complete"] is False


def test_a_root_that_states_no_coverage_claims_none(monkeypatch, caplog):
    """An unstated scope is reported empty, never assumed to be the widest."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch, operations=["sandbox.wake"])
    measurement = profiling.start_operation(operation="sandbox.wake", producer="control-plane")
    assert profiling.bind(measurement, job_id="ajob_1")

    profiling.finish(measurement, outcome="completed")

    record = _records(caplog)[0]
    assert record["coverage_scope"] == ""
    assert record["end_to_end_complete"] is False


def test_the_response_and_the_record_cannot_disagree_about_completeness(monkeypatch, caplog, clock):
    """A boundary frozen before the response is built is the one emitted.

    Reproduced the other way round: metadata rendered at 100 said complete,
    ``finish`` at 102 re-read the clock, saw the deadline pass, and emitted the
    same measurement as partial. The client had already been told the sample
    was whole, and the record denied it.
    """
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None
    profiling.mark(measurement, "validate")

    # The response is built one microsecond before the deadline, and encoding
    # it takes long enough to cross the deadline before export.
    remaining = measurement.deadline_offset_us()
    clock.advance(max(0, remaining - 1))
    profiling.finalize(measurement, "dispatched")
    assert profiling.collection_complete(measurement, "dispatched") is True

    clock.advance(10_000)
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    # What the response said and what the record says are the same answer.
    assert record["collection_complete"] is True
    assert record["measurement_complete"] is True


def test_a_boundary_frozen_past_the_deadline_stays_partial(monkeypatch, caplog, clock):
    """The freeze is not an escape from the deadline, only from re-deciding it."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    clock.advance(measurement.deadline_offset_us() + 1_000)
    profiling.finalize(measurement, "dispatched")
    profiling.finish(measurement, outcome="dispatched")

    record = _records(caplog)[0]
    assert record["collection_complete"] is False
    assert record["measurement_complete"] is False
    assert record["facts"]["profiling_stopped"] == "expired"


def test_a_frozen_outcome_is_not_rewritten_by_a_later_failure(monkeypatch, caplog):
    """A client already told the request succeeded is not contradicted later."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _enable(monkeypatch)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None

    profiling.finalize(measurement, "dispatched")
    # Export happens after the response; a bookkeeping failure must not turn a
    # dispatched startup into a failed one in the record.
    profiling.finish(measurement, outcome="failed")

    record = _records(caplog)[0]
    assert record["outcome"] == "dispatched"
    assert record["startup_outcome"] == "dispatched"


def test_a_measurement_carries_the_configured_expiry_not_its_own_clock(monkeypatch, clock):
    """The run's absolute expiry is identity; the monotonic deadline is not.

    A producer that re-derived the expiry from its own remaining time would be
    stating a different experiment than the one the descriptor configures, and
    two writers of one run would then disagree about what it is.
    """
    # The descriptor states one instant; everything else about the run is
    # derived from it.
    configured = datetime.now(timezone.utc) + timedelta(minutes=30)
    descriptor = {
        "run_id": "run-1",
        "operations": ["attempt.startup"],
        "expires_at": configured.isoformat(),
        "max_operations": 10,
    }
    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(profiling.ENV_PROFILING_RUN, json.dumps(descriptor))
    profiling.reset_cache()

    active = profiling.active_run()
    assert active is not None
    assert active.expires_at_utc == configured

    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None
    before = measurement.deadline_mono
    clock.advance(60_000_000)
    # Time passed, so the local deadline is closer -- and the configured
    # instant is unchanged, because it is not a measurement of anything.
    assert measurement.run_expires_at == configured
    assert measurement.deadline_mono == before


def test_the_same_instant_written_two_ways_is_one_expiry():
    """An offset spelling and a UTC spelling are the same configured moment."""
    # One future instant, spelled three ways. Deriving all three from a single
    # value is the point: literals pinned to a date would start failing on that
    # date, because ``parse_run`` requires an expiry in the future and the
    # check is against the real clock. A test must not expire.
    # Whole seconds, because one of the three spellings carries no fractional
    # part -- comparing a microsecond-truncated copy with the original would be
    # testing the spelling rather than the instant.
    moment = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    beijing = timezone(timedelta(hours=8))

    def descriptor(expires_at: str) -> dict[str, object]:
        return {
            "run_id": "run-1",
            "operations": ["attempt.startup"],
            "expires_at": expires_at,
            "max_operations": 10,
        }

    utc_run = profiling.parse_run(json.dumps(descriptor(moment.isoformat())))
    offset_run = profiling.parse_run(json.dumps(descriptor(moment.astimezone(beijing).isoformat())))
    zulu_run = profiling.parse_run(json.dumps(descriptor(moment.strftime("%Y-%m-%dT%H:%M:%SZ"))))

    # Three strings that differ, one instant that does not.
    assert len({moment.isoformat(), moment.astimezone(beijing).isoformat()}) == 2
    assert utc_run.expires_at_utc == offset_run.expires_at_utc == zulu_run.expires_at_utc
