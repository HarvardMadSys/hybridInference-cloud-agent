"""Per-call attribution: what a one-second sampler may say about a 35 ms call.

The answer is almost always "nothing", and these pin that the nothing arrives
as a row saying so rather than as an absence or a zero. Production's median
tool call is far shorter than the sampling interval, so the row that reads
``sample_count = 0`` with null counters is not the edge case here — it is the
common case, and the one a reader must be able to tell apart from a call that
genuinely used no CPU.
"""

from __future__ import annotations

from cloud_agent_host.resources import Sample
from cloud_agent_host.tool_windows import (
    MAX_TOOL_CALLS,
    ToolWindow,
    ToolWindowTracker,
    attribute,
)


def _second_by_second(count: int, *, cpu_step: int = 100_000, mem: int = 4_000_000):
    """One reading a second, the way the deployed sampler takes them."""
    return [
        Sample(mono=float(i), cpu_usec=i * cpu_step, mem_bytes=mem + i * 1_000)
        for i in range(count)
    ]


def _by_id(rows, tool_use_id):
    return next(row for row in rows if row["tool_use_id"] == tool_use_id)


# --- the two rules -----------------------------------------------------------


def test_a_call_long_enough_to_hold_readings_is_measured_exclusively():
    """Nine readings inside one call: the delta between the first and last."""
    windows = [ToolWindow("t1", "Bash", opened_mono=0.4, closed_mono=9.9)]
    (row,) = attribute(_second_by_second(13), windows)
    assert row["sample_count"] == 9
    assert row["overlap_n"] == 1
    assert row["cpu_usec_exclusive"] == 800_000
    assert row["cpu_usec_shared"] is None


def test_a_call_shorter_than_the_interval_reports_a_row_with_no_numbers():
    """The 97 % case. The row exists; every counter on it is null."""
    windows = [ToolWindow("t1", "Read", opened_mono=10.15, closed_mono=10.185)]
    (row,) = attribute(_second_by_second(13), windows)
    assert row["tool_name"] == "Read"
    assert row["duration_ms"] == 35
    assert row["sample_count"] == 0
    assert row["cpu_usec_exclusive"] is None
    assert row["cpu_usec_shared"] is None
    assert row["mem_peak_exclusive_bytes"] is None
    assert row["mem_peak_shared_bytes"] is None


def test_a_call_holding_one_reading_is_still_unmeasured():
    """One reading of a cumulative counter is a level, not a consumption."""
    windows = [ToolWindow("t1", "Bash", opened_mono=2.5, closed_mono=3.5)]
    (row,) = attribute(_second_by_second(13), windows)
    assert row["sample_count"] == 1
    assert row["cpu_usec_exclusive"] is None


def test_parallel_calls_share_their_readings_and_none_of_them_is_exclusive():
    """The counters cannot separate concurrent calls, so neither claims them."""
    windows = [
        ToolWindow("a", "Bash", opened_mono=2.0, closed_mono=6.0),
        ToolWindow("b", "Bash", opened_mono=3.0, closed_mono=7.0),
    ]
    rows = attribute(_second_by_second(13), windows)
    for row in rows:
        assert row["overlap_n"] == 2
        assert row["cpu_usec_exclusive"] is None
        assert row["mem_peak_exclusive_bytes"] is None
        assert row["cpu_usec_shared"] is not None


def test_a_serial_neighbour_does_not_make_a_call_shared():
    """Calls that merely follow one another each own their own readings."""
    windows = [
        ToolWindow("a", "Bash", opened_mono=0.5, closed_mono=4.5),
        ToolWindow("b", "Bash", opened_mono=5.5, closed_mono=9.5),
    ]
    rows = attribute(_second_by_second(13), windows)
    assert [row["overlap_n"] for row in rows] == [1, 1]
    assert all(row["cpu_usec_exclusive"] is not None for row in rows)


# --- honesty about what the number is ----------------------------------------


def test_the_measured_span_says_the_number_is_a_floor():
    """Readings start after the call and end before it; the row admits it."""
    windows = [ToolWindow("t1", "Bash", opened_mono=0.4, closed_mono=9.9)]
    (row,) = attribute(_second_by_second(13), windows)
    assert row["duration_ms"] == 9_500
    assert row["measured_span_ms"] == 8_000
    assert row["measured_span_ms"] < row["duration_ms"]


def test_an_unmeasured_call_has_no_span_either():
    """A span of zero would read as "measured, and it took no time"."""
    windows = [ToolWindow("t1", "Read", opened_mono=10.15, closed_mono=10.185)]
    (row,) = attribute(_second_by_second(13), windows)
    assert row["measured_span_ms"] is None


def test_a_call_that_never_returned_is_a_row_not_a_deletion():
    """A turn that ends mid-call is a timeout, and the row is the evidence."""
    windows = [ToolWindow("t1", "Bash", opened_mono=1.0, closed_mono=None)]
    (row,) = attribute(_second_by_second(13), windows)
    assert row["duration_ms"] is None
    assert row["sample_count"] == 0
    assert row["cpu_usec_exclusive"] is None


def test_heartbeat_samples_do_not_count_as_readings():
    """A sample carrying no counter at all measures nothing, inside or out."""
    samples = [Sample(mono=float(i)) for i in range(6)]
    windows = [ToolWindow("t1", "Bash", opened_mono=0.5, closed_mono=5.5)]
    (row,) = attribute(samples, windows)
    assert row["sample_count"] == 0
    assert row["cpu_usec_exclusive"] is None


def test_no_samples_at_all_still_produces_every_row():
    """An unmeasured turn still says which calls it made."""
    windows = [
        ToolWindow("a", "Bash", opened_mono=0.5, closed_mono=4.5),
        ToolWindow("b", "Read", opened_mono=5.0, closed_mono=5.1),
    ]
    rows = attribute([], windows)
    assert [row["tool_name"] for row in rows] == ["Bash", "Read"]
    assert all(row["sample_count"] == 0 for row in rows)


# --- the tracker -------------------------------------------------------------


def test_the_tracker_pairs_a_result_to_its_call():
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "t1", "name": "Bash"}, mono=1.0)
    tracker.observe("message", {"text": "thinking"}, mono=1.5)
    tracker.observe("tool_result", {"tool_use_id": "t1"}, mono=3.0)
    (window,) = tracker.windows()
    assert window.tool == "Bash"
    assert window.duration_ms == 2_000


def test_a_result_with_no_matching_call_is_ignored():
    """Pairing on the id is what keeps one call's time off another's row."""
    tracker = ToolWindowTracker()
    tracker.observe("tool_result", {"tool_use_id": "nobody"}, mono=3.0)
    assert tracker.windows() == []


def test_a_call_without_an_id_is_not_tracked_at_all():
    """It could only ever be reported unfinished, which would be a lie."""
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"name": "Bash"}, mono=1.0)
    tracker.observe("tool_result", {"tool_use_id": ""}, mono=2.0)
    assert tracker.windows() == []


def test_an_unclosed_call_survives_into_the_report():
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "t1", "name": "Bash"}, mono=1.0)
    (window,) = tracker.windows()
    assert window.closed_mono is None
    assert not tracker.truncated


def test_finished_calls_are_reported_in_the_order_they_finished():
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "a", "name": "Bash"}, mono=1.0)
    tracker.observe("tool_use", {"id": "b", "name": "Read"}, mono=1.1)
    tracker.observe("tool_result", {"tool_use_id": "b"}, mono=1.2)
    tracker.observe("tool_result", {"tool_use_id": "a"}, mono=4.0)
    assert [w.tool for w in tracker.windows()] == ["Read", "Bash"]


def test_a_turn_past_the_cap_says_so_rather_than_keeping_a_quiet_prefix():
    tracker = ToolWindowTracker(limit=3)
    for index in range(5):
        tracker.observe("tool_use", {"id": f"t{index}", "name": "Read"}, mono=float(index))
        tracker.observe("tool_result", {"tool_use_id": f"t{index}"}, mono=index + 0.01)
    assert len(tracker.windows()) == 3
    assert tracker.truncated


def test_the_cap_is_the_one_the_schema_bounds():
    """The runner and the request model must agree, or a full turn 422s."""
    assert MAX_TOOL_CALLS == 500


def test_a_hostile_tool_name_cannot_widen_the_row():
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "t1", "name": "x" * 5_000}, mono=1.0)
    (window,) = tracker.windows()
    assert len(window.tool) == 120
