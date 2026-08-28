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
    TOOL_MARK_FILE,
    ToolMarkReader,
    ToolWindow,
    ToolWindowTracker,
    attribute,
    hook_settings,
    tool_time_ms,
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


# --- hook marks: the exact windows -------------------------------------------


def test_marks_narrow_a_window_and_label_it_exact():
    """The upgrade: the sandbox's own boundaries replace the outside bracket."""
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "t1", "name": "Bash"}, mono=1.0)
    tracker.observe("tool_result", {"tool_use_id": "t1"}, mono=9.0)

    upgraded = tracker.apply_marks([(2.0, "pre", "t1"), (8.0, "post", "t1")])

    (window,) = tracker.windows()
    assert upgraded == 1
    assert window.source == "exact"
    assert window.duration_ms == 6_000


def test_a_mark_outside_the_window_it_would_narrow_is_refused():
    """A stepped clock must not move a call somewhere it never ran."""
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "t1", "name": "Bash"}, mono=1.0)
    tracker.observe("tool_result", {"tool_use_id": "t1"}, mono=9.0)

    upgraded = tracker.apply_marks([(0.5, "pre", "t1"), (8.0, "post", "t1")])

    (window,) = tracker.windows()
    assert upgraded == 0
    assert window.source == "message"
    assert window.opened_mono == 1.0


def test_half_a_pair_of_marks_leaves_the_call_alone():
    """Both boundaries or neither; a one-sided window is not a measurement."""
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "t1", "name": "Bash"}, mono=1.0)
    tracker.observe("tool_result", {"tool_use_id": "t1"}, mono=9.0)

    tracker.apply_marks([(2.0, "pre", "t1")])

    assert tracker.windows()[0].source == "message"


def test_exact_windows_stop_parallel_calls_sharing_when_they_did_not_overlap():
    """Why the hooks are worth their 0.6 ms: a third of production's calls
    overlap at message level and can only be reported as shared upper bounds.
    Narrowed to what actually ran, the same two calls are exclusive."""
    tracker = ToolWindowTracker()
    # One assistant message announcing both calls: identical message windows.
    tracker.observe("tool_use", {"id": "a", "name": "Bash"}, mono=0.0)
    tracker.observe("tool_use", {"id": "b", "name": "Bash"}, mono=0.0)
    tracker.observe("tool_result", {"tool_use_id": "a"}, mono=12.0)
    tracker.observe("tool_result", {"tool_use_id": "b"}, mono=12.0)
    samples = _second_by_second(13)

    before = attribute(samples, tracker.windows())
    assert all(row["overlap_n"] == 2 for row in before)
    assert all(row["cpu_usec_exclusive"] is None for row in before)

    tracker.apply_marks(
        [(0.5, "pre", "a"), (5.0, "post", "a"), (6.0, "pre", "b"), (11.5, "post", "b")]
    )
    after = attribute(samples, tracker.windows())

    assert all(row["overlap_n"] == 1 for row in after)
    assert all(row["cpu_usec_exclusive"] is not None for row in after)
    assert all(row["timing_source"] == "exact" for row in after)


def test_an_unmarked_turn_reports_every_row_as_message_timed():
    """No hooks, no marks, no claim of precision."""
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "t1", "name": "Bash"}, mono=1.0)
    tracker.observe("tool_result", {"tool_use_id": "t1"}, mono=9.0)
    (row,) = attribute([], tracker.windows())
    assert row["timing_source"] == "message"


# --- the mark file -----------------------------------------------------------


def test_the_reader_only_takes_marks_written_after_it_was_built(tmp_path):
    """A resident session appends every turn's marks to the same file."""
    path = tmp_path / TOOL_MARK_FILE
    path.write_text("100.0 pre old\n100.5 post old\n")

    reader = ToolMarkReader(str(tmp_path))
    with path.open("a") as handle:
        handle.write("101.0 pre new\n")

    assert [mark[2] for mark in reader.read()] == ["new"]


def test_a_malformed_mark_is_skipped_not_raised(tmp_path):
    path = tmp_path / TOOL_MARK_FILE
    reader = ToolMarkReader(str(tmp_path))
    path.write_text(
        "not a mark\n"
        "abc pre t1\n"
        "1.0 sideways t1\n"
        "1.0 pre has space\n"
        "1.0 pre t1;rm -rf /\n"
        "1.0 pre good\n"
    )
    assert [mark[2] for mark in reader.read()] == ["good"]


def test_no_state_mount_means_no_marks_and_no_error():
    assert ToolMarkReader(None).read() == []


def test_a_missing_mark_file_is_simply_no_marks(tmp_path):
    assert ToolMarkReader(str(tmp_path)).read() == []


# --- the hook the sandbox runs -----------------------------------------------


def test_the_hook_is_passive_by_construction():
    """It writes and exits 0; it never inspects a result or blocks a call."""
    settings = hook_settings("/agent-state/tool-marks.log")
    commands = [
        entry["hooks"][0]["command"]
        for entry in (*settings["hooks"]["PreToolUse"], *settings["hooks"]["PostToolUse"])
    ]
    assert len(commands) == 2
    for command in commands:
        assert command.endswith("exit 0")
        assert ">> /agent-state/tool-marks.log" in command
        assert "rm " not in command
    assert " pre " in commands[0] and " post " in commands[1]


def test_the_hook_takes_its_timestamp_before_it_reads_anything():
    """Otherwise the mark is the boundary plus however long stdin took."""
    command = hook_settings("/m")["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command.index("date +%s.%N") < command.index("sed -n")


def test_every_hook_carries_a_timeout():
    """A hook without one could hold a tool call for as long as it liked."""
    settings = hook_settings("/m")
    for event in ("PreToolUse", "PostToolUse"):
        assert settings["hooks"][event][0]["hooks"][0]["timeout"] == 5


# --- where the turn's time went ----------------------------------------------


def test_tool_time_is_the_union_so_it_cannot_exceed_the_turn():
    """Summing parallel calls produces more tool time than there was time."""
    windows = [
        ToolWindow("a", "Bash", opened_mono=0.0, closed_mono=10.0),
        ToolWindow("b", "Bash", opened_mono=2.0, closed_mono=6.0),
    ]
    assert tool_time_ms(windows) == 10_000


def test_serial_calls_add_up():
    windows = [
        ToolWindow("a", "Bash", opened_mono=0.0, closed_mono=2.0),
        ToolWindow("b", "Bash", opened_mono=5.0, closed_mono=6.0),
    ]
    assert tool_time_ms(windows) == 3_000


def test_a_call_that_never_returned_contributes_no_time():
    """Its end is unknown, and guessing it would inflate the split."""
    assert tool_time_ms([ToolWindow("a", "Bash", opened_mono=0.0)]) == 0


def test_a_turn_with_no_calls_spent_no_time_in_them():
    assert tool_time_ms([]) == 0
