"""Loss-aware Kata guest process observation and tool attribution."""

from __future__ import annotations

import json
import subprocess

import pytest

from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_ROTATE_BYTES,
    GUEST_OBSERVER_SOURCE,
    MAX_OBSERVED_TOOL_MARKS,
    GuestObserverController,
    GuestObserverCursor,
    GuestObserverProtocolError,
    GuestObserverRuntimeError,
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
                "journal_dev": 7,
                "journal_ino": 19,
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
                        "journal_dev": 7,
                        "journal_ino": 19,
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


def test_a_replaced_journal_cannot_look_like_the_same_observer_after_pid_reuse(
    monkeypatch,
):
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=lambda *_args: None,
    )
    cursor = _cursor(observer_pid=91, journal_dev=7, journal_ino=19)
    replaced = _snapshot(
        status=GuestObserverStatus(
            pid=91,
            poll_seq=11,
            event_seq=20,
            lost=4,
            journal_dropped=2,
            journal_bytes=100,
            journal_dev=7,
            journal_ino=20,
        )
    )
    monkeypatch.setattr(controller, "snapshot", lambda **_kwargs: replaced)

    observation = controller.finish(cursor)

    assert observation.complete is False
    assert "journal changed" in (observation.detail or "")


def test_regressed_loss_counters_cannot_look_like_a_complete_observation(monkeypatch):
    controller = GuestObserverController(
        capture=lambda *_args: pytest.fail("a reset fence must be rejected before journal read"),
        privileged=lambda *_args: None,
    )
    regressed = _snapshot(
        status=GuestObserverStatus(
            pid=91,
            poll_seq=11,
            event_seq=9,
            lost=3,
            journal_dropped=2,
            journal_bytes=100,
            journal_dev=7,
            journal_ino=19,
        )
    )
    monkeypatch.setattr(controller, "snapshot", lambda **_kwargs: regressed)

    observation = controller.finish(_cursor(observer_pid=91, journal_dev=7, journal_ino=19))

    assert observation.complete is False
    assert "loss counters reset" in (observation.detail or "")


def test_a_journal_replaced_after_the_final_snapshot_is_refused(tmp_path, monkeypatch):
    """The fd opened for the read must still be the cursor's exact journal."""
    replacement = tmp_path / "events.jsonl"
    replacement.write_bytes(_event(1, "fork", 1_001_000_000))
    expected_size = replacement.stat().st_size
    expected_dev, expected_ino = 7, 19
    snapshot = _snapshot(
        status=GuestObserverStatus(
            pid=91,
            poll_seq=11,
            event_seq=9,
            lost=4,
            journal_dropped=2,
            journal_bytes=expected_size,
            journal_dev=expected_dev,
            journal_ino=expected_ino,
        ),
        journal_size=expected_size,
    )

    def capture(argv, timeout_s):
        command = list(argv)
        command[3] = str(replacement)
        return subprocess.run(
            command,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )

    controller = GuestObserverController(
        capture=capture,
        privileged=lambda *_args: None,
    )
    monkeypatch.setattr(controller, "snapshot", lambda **_kwargs: snapshot)
    cursor = _cursor(
        observer_pid=91,
        journal_dev=expected_dev,
        journal_ino=expected_ino,
    )

    observation = controller.finish(cursor)

    assert observation.complete is False
    assert "journal changed before read" in (observation.detail or "")


def test_begin_crosses_a_complete_poll_before_publishing_its_fence(monkeypatch):
    opening = _snapshot(
        status=GuestObserverStatus(91, 20, 8, 4, 2, 100, 7, 19),
        journal_size=100,
        guest_mono_ns=1_900_000_000,
    )
    flushed = _snapshot(
        status=GuestObserverStatus(91, 22, 9, 5, 2, 180, 7, 19),
        journal_size=180,
        guest_mono_ns=2_000_000_000,
    )
    waits: list[int | None] = []
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=lambda *_args: None,
    )
    monkeypatch.setattr(controller, "ensure", lambda: None)

    def snapshot(*, after_poll_seq=None, **_kwargs):
        waits.append(after_poll_seq)
        return opening if after_poll_seq is None else flushed

    monkeypatch.setattr(controller, "snapshot", snapshot)

    cursor = controller.begin(user_uid=11003)

    assert waits == [None, 21]
    assert cursor.offset == 180
    assert cursor.lost == 5
    assert cursor.event_seq == 9
    assert cursor.guest_mono_ns == 2_000_000_000


def test_active_shared_observations_never_restart_on_a_new_snapshot_failure(monkeypatch):
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=lambda *_args: pytest.fail("an active sibling forbids observer restart"),
    )
    monkeypatch.setattr(
        controller,
        "ensure",
        lambda: pytest.fail("restart recovery must stay disabled while a cursor is active"),
    )

    def unavailable_snapshot(**_kwargs):
        raise GuestObserverRuntimeError("transient docker exec failure")

    monkeypatch.setattr(controller, "snapshot", unavailable_snapshot)

    cursor = controller.begin(user_uid=11004, restart_if_missing=False)

    assert cursor.available is False
    assert "transient docker exec failure" in (cursor.detail or "")


def test_late_journal_delivery_cannot_charge_a_pre_turn_process():
    payload = b"".join(
        [
            _event(1, "fork", 999_000_000, pid=41, tgid=41),
            _event(2, "exit", 999_500_000, pid=41, tgid=41),
            _event(3, "fork", 1_001_000_000),
            _event(4, "exit", 1_002_000_000),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.complete is True
    assert [process.pid for process in observation.processes] == [42]


def test_an_event_at_or_before_the_cursor_sequence_makes_the_slice_partial():
    observation = finish_observation(
        cursor=_cursor(event_seq=5),
        snapshot=_snapshot(),
        payload=_event(5, "fork", 1_001_000_000),
        truncated=False,
    )

    assert observation.complete is False
    assert observation.truncated is True
    assert observation.processes == ()


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


def test_tool_mark_overflow_cannot_downgrade_a_complete_process_census():
    marks = b"".join(
        _event(
            seq,
            "tool_pre",
            1_000_000_000 + seq,
            pid=50,
            tool_use_id=f"toolu_{seq}",
        )
        for seq in range(1, MAX_OBSERVED_TOOL_MARKS + 2)
    )
    process_seq = MAX_OBSERVED_TOOL_MARKS + 2
    payload = marks + _event(process_seq, "fork", 1_001_000_000)

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.complete is True
    assert len(observation.tool_marks) == MAX_OBSERVED_TOOL_MARKS
    assert [process.pid for process in observation.processes] == [42]


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
