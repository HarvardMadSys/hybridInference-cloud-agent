"""Loss-aware Kata guest process observation and tool attribution."""

from __future__ import annotations

import json

import pytest

from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_ROTATE_BYTES,
    GUEST_OBSERVER_SOURCE,
    GuestObserverController,
    GuestObserverCursor,
    GuestObserverProtocolError,
    GuestObserverSnapshot,
    GuestObserverStatus,
    attribute_processes,
    finish_observation,
    parse_snapshot,
)
from cloud_agent_host.tool_windows import TOOL_MARK_HELPER, ToolWindow


def _cursor(**overrides) -> GuestObserverCursor:
    values = {
        "uid": 11003,
        "offset": 0,
        "lost": 4,
        "journal_dropped": 2,
        "poll_seq": 10,
        "guest_mono_ns": 1_000_000_000,
        "mono_offset_ns": 10_000_000_000,
        "clock_error_ns": 0,
        "available": True,
    }
    values.update(overrides)
    return GuestObserverCursor(**values)


def _snapshot(**overrides) -> GuestObserverSnapshot:
    status = GuestObserverStatus(
        pid=91,
        poll_seq=11,
        event_seq=9,
        lost=4,
        journal_dropped=2,
    )
    return GuestObserverSnapshot(
        status=overrides.pop("status", status),
        journal_size=overrides.pop("journal_size", 100),
        guest_mono_ns=overrides.pop("guest_mono_ns", 2_000_000_000),
        **overrides,
    )


def _event(seq: int, kind: str, mono_ns: int, **overrides) -> bytes:
    value = {
        "version": 1,
        "seq": seq,
        "kind": kind,
        "mono_ns": mono_ns,
        "uid": 11003,
        "pid": 42,
        "tgid": 42,
        "ppid": 7,
        "comm": "tiny",
        "filename": "/bin/tiny",
        "exit_code": 0,
    }
    value.update(overrides)
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def test_snapshot_contract_requires_ready_versioned_monotonic_state():
    payload = json.dumps(
        {
            "status": {
                "version": 1,
                "ready": True,
                "pid": 9,
                "poll_seq": 12,
                "event_seq": 2,
                "lost": 0,
                "journal_dropped": 0,
                "journal_bytes": 81,
            },
            "journal_size": 81,
            "guest_mono_ns": 123456,
        }
    ).encode()

    snapshot = parse_snapshot(payload)

    assert snapshot.status.pid == 9
    assert snapshot.journal_size == 81
    assert snapshot.guest_mono_ns == 123456


def test_snapshot_refuses_a_status_that_could_make_no_events_look_complete():
    with pytest.raises(GuestObserverProtocolError, match="not ready"):
        parse_snapshot(
            json.dumps(
                {
                    "status": {
                        "version": 1,
                        "ready": False,
                        "pid": 9,
                        "poll_seq": 1,
                        "event_seq": 0,
                        "lost": 0,
                        "journal_dropped": 0,
                        "journal_bytes": 0,
                    },
                    "journal_size": 0,
                    "guest_mono_ns": 1,
                }
            ).encode()
        )


def test_idle_rotation_failure_never_rejects_the_next_turn(monkeypatch):
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=lambda *_args: None,
    )
    monkeypatch.setattr(
        controller,
        "snapshot",
        lambda **_kwargs: _snapshot(journal_size=GUEST_OBSERVER_ROTATE_BYTES),
    )

    def fail_start():
        raise RuntimeError("guest restart failed")

    monkeypatch.setattr(controller, "start", fail_start)

    assert controller.rotate_if_needed() is False


def test_a_sub_millisecond_process_survives_without_any_sampler_reading():
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000, filename=""),
            _event(2, "exec", 1_001_100_000),
            _event(3, "exit", 1_001_700_000),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.source == GUEST_OBSERVER_SOURCE
    assert observation.complete is True
    assert observation.lost_events == 0
    (process,) = observation.processes
    assert process.executable == "/bin/tiny"
    assert process.observed_fork and process.observed_exec and process.observed_exit
    assert process.duration_ms == 1
    assert process.exit_code == 0


def test_member_uid_is_the_attribution_fence_for_concurrent_siblings():
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000, uid=11004, pid=51, tgid=51),
            _event(2, "exit", 1_002_000_000, uid=11004, pid=51, tgid=51),
            _event(3, "fork", 1_003_000_000),
            _event(4, "exit", 1_004_000_000),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert [process.pid for process in observation.processes] == [42]


def test_authenticated_guest_marks_share_the_uid_and_monotonic_fence():
    payload = b"".join(
        [
            _event(1, "tool_pre", 1_001_000_000, pid=50, tool_use_id="toolu_1"),
            _event(
                2,
                "tool_post",
                1_003_000_000,
                uid=11004,
                pid=51,
                tool_use_id="sibling",
            ),
            _event(3, "tool_post", 1_004_000_000, pid=50, tool_use_id="toolu_1"),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.tool_marks == (
        (11.001, "pre", "toolu_1"),
        (11.004, "post", "toolu_1"),
    )


@pytest.mark.parametrize(
    ("status", "truncated", "expected_lost"),
    [
        (GuestObserverStatus(91, 11, 9, 7, 2), False, 3),
        (GuestObserverStatus(91, 11, 9, 4, 5), False, 3),
        (GuestObserverStatus(91, 11, 9, 4, 2), True, 0),
    ],
)
def test_loss_or_a_bounded_read_never_claims_a_complete_census(
    status: GuestObserverStatus,
    truncated: bool,
    expected_lost: int,
):
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(status=status),
        payload=b"",
        truncated=truncated,
    )

    assert observation.complete is False
    assert observation.lost_events == expected_lost


def test_process_start_is_attributed_even_when_it_fits_between_sampler_ticks():
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=_event(1, "fork", 1_001_000_000) + _event(2, "exit", 1_002_000_000),
        truncated=False,
    )
    windows = [
        ToolWindow(
            tool_use_id="toolu_1",
            tool="Bash",
            opened_mono=11.000,
            closed_mono=11.010,
        )
    ]

    rows, counts = attribute_processes(observation, windows)

    assert counts == {1: 1}
    assert rows[0]["tool_call_seqs"] == [1]
    assert rows[0]["started_offset_ms"] == 1


def test_tool_marker_instrumentation_is_not_counted_as_user_workload():
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000, pid=41, tgid=41, filename=""),
            _event(
                2,
                "exec",
                1_001_100_000,
                pid=41,
                tgid=41,
                comm="freeinference-t",
                filename=TOOL_MARK_HELPER,
            ),
            _event(3, "exit", 1_001_500_000, pid=41, tgid=41),
            _event(4, "fork", 1_002_000_000, pid=42, tgid=42, filename=""),
            _event(5, "exec", 1_002_100_000, pid=42, tgid=42),
            _event(6, "exit", 1_002_600_000, pid=42, tgid=42),
        ]
    )
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )
    windows = [ToolWindow("one", "Bash", opened_mono=11.000, closed_mono=11.010)]

    rows, counts = attribute_processes(observation, windows)

    assert [row["pid"] for row in rows] == [41, 42]
    assert rows[0]["executable"] == TOOL_MARK_HELPER
    assert rows[0]["tool_call_seqs"] == []
    assert counts == {1: 1}


def test_overlapping_tool_windows_keep_ambiguity_instead_of_picking_one():
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=_event(1, "fork", 1_005_000_000),
        truncated=False,
    )
    windows = [
        ToolWindow("one", "Bash", opened_mono=11.000, closed_mono=11.010),
        ToolWindow("two", "Bash", opened_mono=11.003, closed_mono=11.020),
    ]

    rows, counts = attribute_processes(observation, windows)

    assert rows[0]["tool_call_seqs"] == [1, 2]
    assert counts == {1: 1, 2: 1}


def test_an_exit_only_baseline_process_is_not_manufactured_into_turn_usage():
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=_event(1, "exit", 1_005_000_000),
        truncated=False,
    )

    assert observation.complete is True
    assert observation.processes == ()
