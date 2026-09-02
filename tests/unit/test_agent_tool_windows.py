"""Per-call attribution: what a one-second sampler may say about a 35 ms call.

The answer is almost always "nothing", and these pin that the nothing arrives
as a row saying so rather than as an absence or a zero. Production's median
tool call is far shorter than the sampling interval, so the row that reads
``sample_count = 0`` with null counters is not the edge case here — it is the
common case, and the one a reader must be able to tell apart from a call that
genuinely used no CPU.
"""

from __future__ import annotations

import pytest

from cloud_agent.schemas import (
    WorkerProcessObservation,
    WorkerResourcesRequest,
    WorkerToolCallResources,
)
from cloud_agent_host.resources import Sample
from cloud_agent_host.tool_windows import (
    MAX_TOOL_CALLS,
    TOOL_MARK_FILE,
    TOOL_MARK_HELPER,
    TOOL_MARK_SANDBOX_PATH,
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


def test_an_unfinished_parallel_call_prevents_a_false_exclusive_number():
    """An open-ended call may have produced any reading after it opened."""
    windows = [
        ToolWindow("finished", "Bash", opened_mono=2.0, closed_mono=6.0),
        ToolWindow("open", "Bash", opened_mono=3.0),
    ]

    finished, open_call = attribute(_second_by_second(13), windows)

    assert finished["overlap_n"] == 2
    assert finished["cpu_usec_exclusive"] is None
    assert finished["cpu_usec_shared"] is not None
    assert open_call["cpu_usec_exclusive"] is None


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


def test_an_incomplete_window_list_never_claims_a_delta_is_exclusive():
    """A dropped call may overlap any retained row, so attribution is unsafe."""
    windows = [ToolWindow("kept", "Bash", opened_mono=0.5, closed_mono=4.5)]

    (row,) = attribute(_second_by_second(6), windows, windows_complete=False)

    assert row["sample_count"] == 0
    assert row["cpu_usec_exclusive"] is None
    assert row["mem_peak_exclusive_bytes"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"overlap_n": 2, "cpu_usec_exclusive": 10},
        {"overlap_n": 1, "cpu_usec_shared": 10},
        {
            "overlap_n": 2,
            "cpu_usec_shared": 10,
            "mem_peak_exclusive_bytes": 20,
        },
        {"sample_count": 1, "measured_span_ms": 1},
        # Exit-accounted numbers need a process to have been accounted.
        {"process_count": 0, "process_cpu_usec": 10},
        {"process_count": 0, "process_mem_peak_bytes": 10},
        {"process_count": 0, "process_resources_partial": True},
    ],
)
def test_the_worker_schema_refuses_false_per_call_precision(overrides):
    values = {
        "call_seq": 1,
        "tool_use_id": "toolu_1",
        "tool_name": "Bash",
        "sample_count": 2,
        **overrides,
    }
    with pytest.raises(ValueError):
        WorkerToolCallResources(**values)


def test_a_process_that_has_not_exited_cannot_carry_exit_counters():
    values = {
        "process_seq": 1,
        "uid": 11001,
        "pid": 412,
        "tgid": 412,
        "comm": "tiny",
        "started_offset_ms": 12,
        "observed_fork": True,
        "observed_exit": False,
        "cpu_usec": 15_000,
    }
    with pytest.raises(ValueError):
        WorkerProcessObservation(**values)
    assert WorkerProcessObservation(**{**values, "observed_exit": True}).cpu_usec == 15_000


@pytest.mark.parametrize(
    "payload",
    [
        {
            "tool_calls_truncated": True,
            "tool_time_ms": 10,
        },
        {
            "tool_calls_truncated": True,
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_1",
                    "tool_name": "Bash",
                    "sample_count": 2,
                    "measured_span_ms": 1,
                    "cpu_usec_exclusive": 10,
                }
            ],
        },
        {
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_1",
                    "tool_name": "Bash",
                    "duration_ms": None,
                }
            ],
            "tool_time_ms": 10,
        },
        {
            "process_observer": "kata-guest-ebpf-v1",
            "process_observation_complete": True,
            "process_events_lost": 1,
        },
        {
            "tool_calls": [
                {
                    "call_seq": 1,
                    "tool_use_id": "toolu_1",
                    "tool_name": "Bash",
                    "process_count": 1,
                }
            ],
        },
    ],
)
def test_the_worker_rollup_refuses_completeness_without_evidence(payload):
    with pytest.raises(ValueError):
        WorkerResourcesRequest(sampler_version="2", **payload)


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


def test_parallel_calls_are_reported_in_the_order_they_were_issued():
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "a", "name": "Bash"}, mono=1.0)
    tracker.observe("tool_use", {"id": "b", "name": "Read"}, mono=1.1)
    tracker.observe("tool_result", {"tool_use_id": "b"}, mono=1.2)
    tracker.observe("tool_result", {"tool_use_id": "a"}, mono=4.0)
    assert [w.tool for w in tracker.windows()] == ["Bash", "Read"]


def test_parallel_open_calls_are_bounded_while_the_turn_is_still_running():
    tracker = ToolWindowTracker(limit=3)
    for index in range(10_000):
        tracker.observe(
            "tool_use",
            {"id": f"t{index}", "name": "Read"},
            mono=float(index),
        )

    assert len(tracker.windows()) == 3
    assert tracker.truncated


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


def test_a_tool_id_too_large_for_the_wire_is_not_retained():
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "x" * 201, "name": "Read"}, mono=1.0)

    assert tracker.windows() == []
    assert tracker.truncated


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


def test_duplicate_marks_do_not_claim_an_ambiguous_exact_window():
    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "t1", "name": "Bash"}, mono=1.0)
    tracker.observe("tool_result", {"tool_use_id": "t1"}, mono=9.0)

    upgraded = tracker.apply_marks(
        [
            (2.0, "pre", "t1"),
            (3.0, "pre", "t1"),
            (8.0, "post", "t1"),
        ]
    )

    (window,) = tracker.windows()
    assert upgraded == 0
    assert window.source == "message"


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


def test_the_remote_reader_fences_retained_marks_and_bounds_the_slice():
    """A Docker-owned state layer has the same per-turn append-only fence."""
    content = bytearray(b"100.0 pre old\n")
    reads: list[tuple[int, int]] = []

    def remote_read(offset: int, count: int) -> bytes:
        reads.append((offset, count))
        return bytes(content[offset : offset + count])

    reader = ToolMarkReader(
        None,
        remote_size=lambda: len(content),
        remote_read=remote_read,
    )
    content.extend(b"101.0 pre new\n101.5 post new\n")

    assert [mark[2] for mark in reader.read()] == ["new", "new"]
    assert reads == [(len(b"100.0 pre old\n"), 1024 * 1024)]


def test_a_failed_remote_fence_never_replays_an_older_turn():
    reads: list[tuple[int, int]] = []

    def remote_size() -> int:
        raise OSError("unreadable")

    def remote_read(offset: int, count: int) -> bytes:
        reads.append((offset, count))
        return b"100.0 pre stale\n100.1 post stale\n"

    reader = ToolMarkReader(
        None,
        remote_size=remote_size,
        remote_read=remote_read,
    )

    assert reader.read() == []
    assert reads == []


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
    """It execs one fixed helper and cannot fail or block a tool call."""
    settings = hook_settings(TOOL_MARK_SANDBOX_PATH)
    commands = [
        entry["hooks"][0]["command"]
        for entry in (*settings["hooks"]["PreToolUse"], *settings["hooks"]["PostToolUse"])
    ]
    assert len(commands) == 2
    for command in commands:
        assert command.endswith("exit 0")
        assert f"exec {TOOL_MARK_HELPER}" in command
        assert "/agent-state/tool-marks.log" in command
        assert "$(" not in command
        assert "sed" not in command
        assert "date" not in command
        assert "rm " not in command
    assert " pre " in commands[0] and " post " in commands[1]


def test_the_hook_rejects_any_non_platform_mark_path():
    with pytest.raises(ValueError, match="fixed sandbox state"):
        hook_settings("/agent-state/path with spaces.log")


def test_every_hook_carries_a_timeout():
    """A hook without one could hold a tool call for as long as it liked."""
    settings = hook_settings(TOOL_MARK_SANDBOX_PATH)
    for event in ("PreToolUse", "PostToolUse"):
        assert settings["hooks"][event][0]["hooks"][0]["timeout"] == 1


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


def test_a_call_that_never_returned_makes_the_split_unavailable():
    """Zero would count the whole unfinished call as non-tool time."""
    assert tool_time_ms([ToolWindow("a", "Bash", opened_mono=0.0)]) is None


def test_a_turn_with_no_calls_spent_no_time_in_them():
    assert tool_time_ms([]) == 0
