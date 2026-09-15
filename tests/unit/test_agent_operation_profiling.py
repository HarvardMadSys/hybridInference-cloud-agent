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

from cloud_agent_host import operation_profiling as profiling


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
    """Return every emitted record captured from the log."""
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


def test_the_sink_write_leaves_the_attempt_thread(monkeypatch, caplog, tmp_path):
    """Export runs on its own thread, so disk latency stays out of the sample.

    Regression: the record was written synchronously while the sandbox was
    still starting, which put a slow filesystem inside the measurement it was
    supposed to explain.
    """
    sink = tmp_path / "profile.jsonl"
    _enable(monkeypatch)
    monkeypatch.setenv(profiling.ENV_PROFILING_SINK, str(sink))
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    writers: list[threading.Thread] = []
    real_write = profiling._SinkWriter._write

    def recording_write(self, line):
        writers.append(threading.current_thread())
        real_write(self, line)

    monkeypatch.setattr(profiling._SinkWriter, "_write", recording_write)
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")

    profiling.mark(measurement, "validate")
    profiling.finish(measurement, outcome="dispatched")
    profiling.flush_sink()

    assert writers, "the record was never exported"
    assert writers[0] is not threading.current_thread()
    assert writers[0].name == "operation-profile-sink"
    assert json.loads(sink.read_text().splitlines()[0])["measurement_id"]


def test_profiling_off_never_starts_the_export_thread(monkeypatch, caplog):
    """The default deployment pays for none of this, thread or file."""
    assert profiling.start_operation(operation="attempt.startup") is None
    profiling.flush_sink()
    profiling.close_sink()

    assert profiling._SINK is None


def test_a_full_queue_drops_records_instead_of_blocking(monkeypatch, tmp_path, caplog):
    """A sink that cannot keep up loses records and says so, and never waits."""
    caplog.set_level(logging.WARNING, logger=profiling.__name__)
    release = threading.Event()
    entered = threading.Event()

    def stalled_write(self, line):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(profiling._SinkWriter, "_write", stalled_write)
    writer = profiling._SinkWriter(str(tmp_path / "profile.jsonl"), maxsize=4)
    try:
        writer.offer('{"warm":true}')
        # Wait until the writer thread holds that line and is parked in the
        # write, so the queue is provably empty and the count is deterministic.
        assert entered.wait(5)
        started = time.monotonic()
        for index in range(14):
            writer.offer(f'{{"n":{index}}}')
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
