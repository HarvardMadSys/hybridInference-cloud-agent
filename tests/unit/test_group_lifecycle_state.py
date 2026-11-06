"""The transition table of ``GroupLifecycleState`` (A2 spec section 7.1).

Each row starts from a state written by its old flag names, makes one call
under the state's lock, and names the combination it leaves or the exception
it raises. Every transition has a row whose precondition holds, one whose
precondition does not (an ``AssertionError``: unit tests run with strict
checks, ``tests/unit/conftest.py``), and rows under the records and barriers
that change what it does.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import pytest
from tests.support.group_lifecycle import new_state

from cloud_agent_host.session_lifecycle import StaleStartFence
from cloud_agent_host.supervisor.group_lifecycle_state import (
    FLAG_NAMES,
    START,
    ExclusiveHolder,
    FinalArchive,
    GroupLifecycleState,
    Hold,
    LifecycleInvariantError,
    OrphanFinalization,
    RunLabel,
    StartOutcome,
    StopHolder,
)

if TYPE_CHECKING:
    from collections.abc import Callable

#: Stands for a lifecycle check holding the group.
CHECK: Final[Any] = object()
#: Another caller's lifecycle check.
OTHER_CHECK: Final[Any] = object()
MEMBERS: Final = ()


_ROWS: list[tuple[str, dict[str, Any], Callable[[GroupLifecycleState], object], object]] = [
    # The start.
    ("begin_start", {}, lambda life: life.begin_start_locked(), ("start",)),
    (
        "begin_start/started",
        {"starting": True},
        lambda life: life.begin_start_locked(),
        AssertionError,
    ),
    (
        "begin_start/stopping",
        {"stopping": True},
        lambda life: life.begin_start_locked(),
        AssertionError,
    ),
    (
        "begin_start/retry_after_failure",
        {"start_failed": True},
        lambda life: life.begin_start_locked(),
        ("start", "start_failed"),
    ),
    (
        "begin_start/lost",
        {"container_lost": True},
        lambda life: life.begin_start_locked(),
        ("start", "container_lost"),
    ),
    (
        "end_start/completed",
        {"starting": True, "start_failed": True},
        lambda life: life.end_start_locked(life.start, StartOutcome.COMPLETED),
        (),
    ),
    (
        "end_start/failed",
        {"starting": True},
        lambda life: life.end_start_locked(life.start, StartOutcome.FAILED),
        ("start_failed",),
    ),
    (
        "end_start/refused",
        {"starting": True, "start_failed": True},
        lambda life: life.end_start_locked(life.start, StartOutcome.REFUSED),
        ("start_failed",),
    ),
    (
        "end_start/another_start",
        {"starting": True},
        lambda life: life.end_start_locked(Hold(START), StartOutcome.COMPLETED),
        AssertionError,
    ),
    (
        "owe_start_completion",
        {"starting": True},
        lambda life: life.owe_start_completion_locked(),
        ("start", "start_completion"),
    ),
    (
        "owe_start_completion/no_start",
        {},
        lambda life: life.owe_start_completion_locked(),
        AssertionError,
    ),
    (
        "settle_start_completion",
        {"start_archive_completion_pending": True},
        lambda life: life.settle_start_completion_locked(),
        (),
    ),
    # The stop.
    (
        "begin_stop/stop_under_its_check",
        {"lifecycle_check": CHECK},
        lambda life: life.begin_stop_locked(StopHolder.STOP, check=CHECK),
        ("stop:stop", "check"),
    ),
    (
        "begin_stop/finalize_under_its_check",
        {"lifecycle_check": CHECK, "orphan_finalization_pending": True},
        lambda life: life.begin_stop_locked(StopHolder.FINALIZE, check=CHECK),
        ("stop:finalize", "check", "orphan"),
    ),
    (
        "begin_stop/stop_without_a_check",
        {},
        lambda life: life.begin_stop_locked(StopHolder.STOP),
        AssertionError,
    ),
    (
        "begin_stop/stop_under_another_check",
        {"lifecycle_check": OTHER_CHECK},
        lambda life: life.begin_stop_locked(StopHolder.STOP, check=CHECK),
        AssertionError,
    ),
    (
        "begin_stop/shutdown_retry",
        {"stop_archive_pending": True},
        lambda life: life.begin_stop_locked(StopHolder.SHUTDOWN_RETRY),
        ("stop:shutdown", "final_archive"),
    ),
    (
        "begin_stop/shutdown_retry_during_a_start",
        {"starting": True, "stop_archive_pending": True},
        lambda life: life.begin_stop_locked(StopHolder.SHUTDOWN_RETRY),
        AssertionError,
    ),
    (
        "begin_stop/shutdown_retry_under_a_check",
        {"lifecycle_check": CHECK, "stop_archive_pending": True},
        lambda life: life.begin_stop_locked(StopHolder.SHUTDOWN_RETRY),
        AssertionError,
    ),
    (
        "hold_archive/starting",
        {"starting": True},
        lambda life: life.hold_archive_locked("req"),
        AssertionError,
    ),
    (
        "begin_stop/stopping",
        {"stopping": True},
        lambda life: life.begin_stop_locked(StopHolder.SHUTDOWN_RETRY),
        AssertionError,
    ),
    ("end_stop", {"stopping": True}, lambda life: life.end_stop_locked(life.stop), ()),
    (
        "end_stop/another_stop",
        {"stopping": True},
        lambda life: life.end_stop_locked(Hold(StopHolder.SHUTDOWN_RETRY)),
        AssertionError,
    ),
    ("end_stop/none_taken", {}, lambda life: life.end_stop_locked(None), ()),
    (
        "end_stop/none_taken_while_stopping",
        {"stopping": True},
        lambda life: life.end_stop_locked(None),
        AssertionError,
    ),
    (
        "hold_archive",
        {"lifecycle_check": CHECK},
        lambda life: life.hold_archive_locked("req"),
        ("stop:archive_hold", "check"),
    ),
    (
        "hold_archive/held_for_this_request",
        {"archive_request_id": "req"},
        lambda life: life.hold_archive_locked("req"),
        ("stop:archive_hold",),
    ),
    (
        "hold_archive/stopping",
        {"stopping": True},
        lambda life: life.hold_archive_locked("req"),
        AssertionError,
    ),
    (
        "release_archive",
        {"archive_request_id": "req", "stop_archive_pending": True},
        lambda life: life.release_archive_locked("req"),
        ("final_archive",),
    ),
    (
        "release_archive/another_request",
        {"archive_request_id": "other"},
        lambda life: life.release_archive_locked("req"),
        AssertionError,
    ),
    # The exclusive workspace.
    (
        "begin_exclusive",
        {"starting": True},
        lambda life: life.begin_exclusive_locked(ExclusiveHolder.ENVIRONMENT),
        ("start", "exclusive:environment"),
    ),
    (
        "begin_exclusive/held",
        {"snapshotting": True},
        lambda life: life.begin_exclusive_locked(ExclusiveHolder.ENVIRONMENT),
        AssertionError,
    ),
    (
        "end_exclusive",
        {"snapshotting": True},
        lambda life: life.end_exclusive_locked(life.exclusive),
        (),
    ),
    (
        "end_exclusive/another_holder",
        {"snapshotting": True},
        lambda life: life.end_exclusive_locked(Hold(ExclusiveHolder.INITIAL_ARCHIVE)),
        AssertionError,
    ),
    (
        "expect_exclusive",
        {"snapshotting": True},
        lambda life: life.expect_exclusive_locked(life.exclusive),
        ("exclusive:initial_archive",),
    ),
    (
        "expect_exclusive/released",
        {},
        lambda life: life.expect_exclusive_locked(Hold(ExclusiveHolder.INITIAL_ARCHIVE)),
        AssertionError,
    ),
    (
        "preserve_initial",
        {"snapshotting": True},
        lambda life: life.preserve_initial_locked(),
        ("exclusive:initial_archive", "initial_preservation"),
    ),
    (
        "release_initial",
        {"snapshotting": True, "initial_archive_preservation_pending": True},
        lambda life: life.release_initial_locked(life.exclusive),
        (),
    ),
    (
        "release_initial/released_by_a_stopped_inspection",
        {},
        lambda life: life.release_initial_locked(Hold(ExclusiveHolder.INITIAL_ARCHIVE)),
        (),
    ),
    (
        "release_initial/another_holder",
        {"snapshotting": True},
        lambda life: life.release_initial_locked(Hold(ExclusiveHolder.INITIAL_ARCHIVE)),
        AssertionError,
    ),
    (
        "accepted_stopped/preserved",
        {"snapshotting": True, "initial_archive_preservation_pending": True},
        lambda life: life.accepted_stopped_locked(),
        (),
    ),
    (
        "accepted_stopped/nothing_preserved",
        {"snapshotting": True},
        lambda life: life.accepted_stopped_locked(),
        ("exclusive:initial_archive",),
    ),
    # The lifecycle check, the generation and the loss.
    (
        "begin_check",
        {"stopping": True},
        lambda life: life.begin_check_locked(CHECK),
        ("stop:shutdown", "check"),
    ),
    (
        "begin_check/held",
        {"lifecycle_check": OTHER_CHECK},
        lambda life: life.begin_check_locked(CHECK),
        AssertionError,
    ),
    ("end_check", {"lifecycle_check": CHECK}, lambda life: life.end_check_locked(CHECK), ()),
    (
        "end_check/another_check",
        {"lifecycle_check": OTHER_CHECK},
        lambda life: life.end_check_locked(CHECK),
        ("check",),
    ),
    (
        "end_check/its_stop_in_progress",
        {"lifecycle_check": CHECK},
        lambda life: (
            life.begin_stop_locked(StopHolder.STOP, check=CHECK),
            life.end_check_locked(CHECK),
        ),
        AssertionError,
    ),
    (
        "end_check/under_an_archive_hold",
        {"lifecycle_check": CHECK, "archive_request_id": "req"},
        lambda life: life.end_check_locked(CHECK),
        ("stop:archive_hold",),
    ),
    (
        "mark_lost",
        {"starting": True},
        lambda life: life.mark_lost_locked(),
        ("start", "container_lost"),
    ),
    # Barriers.
    (
        "owe_final",
        {"stopping": True},
        lambda life: life.owe_final_locked(MEMBERS),
        ("stop:shutdown", "final_archive"),
    ),
    (
        "owe_final/blocked",
        {"lifecycle_check": CHECK},
        lambda life: life.owe_final_locked(MEMBERS, blocked=True),
        ("check", "final_archive", "final_archive_blocked"),
    ),
    (
        "owe_final/keeps_a_block",
        {"starting": True, "stop_archive_pending": True, "stop_archive_blocked": True},
        lambda life: life.owe_final_locked(MEMBERS),
        ("start", "final_archive", "final_archive_blocked"),
    ),
    (
        "owe_final/no_upload_owed",
        {"archives_finals": False, "starting": True, "stop_archive_pending": True},
        lambda life: life.owe_final_locked(None),
        ("start",),
    ),
    (
        "owe_final/outside_any_operation",
        {"snapshotting": True},
        lambda life: life.owe_final_locked(MEMBERS),
        AssertionError,
    ),
    (
        "owe_final/no_members_with_final_archives",
        {"stopping": True},
        lambda life: life.owe_final_locked(None, blocked=True),
        AssertionError,
    ),
    (
        "refresh_final_members",
        {"stopping": True, "stop_archive_pending": True},
        lambda life: life.refresh_final_members_locked(MEMBERS),
        ("stop:shutdown", "final_archive"),
    ),
    (
        "refresh_final_members/none_owed",
        {"stopping": True},
        lambda life: life.refresh_final_members_locked(MEMBERS),
        AssertionError,
    ),
    (
        "owe_orphan",
        {"lifecycle_check": CHECK},
        lambda life: life.owe_orphan_locked(marker_unreadable=True),
        ("check", "orphan", "orphan_marker_unreadable"),
    ),
    (
        "owe_orphan/keeps_its_marker",
        {"lifecycle_check": CHECK, "orphan_marker_unreadable": True},
        lambda life: life.owe_orphan_locked(),
        ("check", "orphan", "orphan_marker_unreadable"),
    ),
    (
        "owe_orphan/outside_a_check_or_stop",
        {"starting": True},
        lambda life: life.owe_orphan_locked(),
        AssertionError,
    ),
    (
        "marker_read",
        {"lifecycle_check": CHECK, "orphan_marker_unreadable": True},
        lambda life: life.marker_read_locked(),
        ("check", "orphan"),
    ),
    ("marker_read/no_orphan", {}, lambda life: life.marker_read_locked(), ()),
    (
        "settle_orphan",
        {"lifecycle_check": CHECK, "orphan_marker_unreadable": True},
        lambda life: life.settle_orphan_locked(),
        ("check",),
    ),
    (
        "settle_archive",
        {
            "stopping": True,
            "stop_archive_pending": True,
            "stop_archive_blocked": True,
            "orphan_marker_unreadable": True,
            "start_archive_completion_pending": True,
            "initial_archive_preservation_pending": True,
            "snapshotting": True,
        },
        lambda life: life.settle_archive_locked(),
        ("stop:shutdown", "exclusive:initial_archive", "initial_preservation"),
    ),
    (
        "settle_archive/outside_any_operation",
        {"stop_archive_pending": True},
        lambda life: life.settle_archive_locked(),
        AssertionError,
    ),
]


@pytest.mark.parametrize(
    ("flags", "call", "expected"),
    [pytest.param(flags, call, expected, id=name) for name, flags, call, expected in _ROWS],
)
def test_a_transition_leaves_the_rows_combination_or_raises(
    flags: dict[str, Any],
    call: Callable[[GroupLifecycleState], object],
    expected: object,
) -> None:
    life = new_state(**flags)
    with life.condition:
        if isinstance(expected, type):
            with pytest.raises(expected):
                call(life)
            return
        call(life)
    assert life.combination() == expected


def test_every_transition_has_rows() -> None:
    called = {name.split("/")[0] for name, *_ in _ROWS}
    transitions = {
        name.removesuffix("_locked")
        for name in vars(GroupLifecycleState)
        if name.endswith("_locked")
        and name not in {"begin_run_locked", "join_run_locked", "advance_generation_locked"}
    }
    assert transitions <= called


def test_a_transition_without_the_lock_is_refused() -> None:
    life = new_state()
    with pytest.raises(AssertionError, match="without the state lock"):
        life.begin_start_locked()
    assert life.start is None


def test_an_archive_hold_is_taken_once_per_request_and_released_with_the_generation() -> None:
    life = new_state(lifecycle_check=CHECK)
    with life.condition:
        assert life.hold_archive_locked("req") is True
        assert life.hold_archive_locked("req") is False
        assert life.archive_request_id == "req"
        life.end_check_locked(CHECK)
        generation = life.generation
        life.release_archive_locked("req")
    assert life.archive_request_id is None
    assert life.generation == generation + 1


def test_the_check_and_the_loss_advance_the_generation() -> None:
    life = new_state()
    with life.condition:
        life.begin_check_locked(CHECK)
        assert life.generation == 0
        assert life.end_check_locked(CHECK) is True
        assert life.end_check_locked(CHECK) is False
        assert life.generation == 1
        life.mark_lost_locked()
        life.mark_lost_locked()
        assert life.advance_generation_locked() == 3


def test_a_stopped_inspection_releases_a_foreign_exclusive_workspace_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    life = new_state(initial_archive_preservation_pending=True)
    with life.condition:
        life.begin_exclusive_locked(ExclusiveHolder.ENVIRONMENT)
        with caplog.at_level(logging.WARNING):
            life.accepted_stopped_locked()
    assert life.exclusive is None
    assert not life.initial_preservation
    events = [getattr(record, "event", None) for record in caplog.records]
    assert events == ["group_lifecycle_foreign_exclusive_release"]


def test_a_final_archive_without_final_archives_records_only_a_block() -> None:
    life = new_state(archives_finals=False, stopping=True)
    with life.condition:
        life.owe_final_locked(None, blocked=True)
    assert life.final_archive == FinalArchive(None, blocked=True)
    assert not life.final_archive_pending


def test_an_orphan_marker_read_keeps_the_finalization() -> None:
    life = new_state(lifecycle_check=CHECK, orphan_marker_unreadable=True)
    with life.condition:
        life.marker_read_locked()
    assert life.orphan == OrphanFinalization(marker_unreadable=False)


# The run label.


@pytest.mark.parametrize(
    ("before", "start_seq", "after"),
    [
        (RunLabel(), 3, RunLabel(start_seq=3)),
        (RunLabel(), None, RunLabel(unfenced=True)),
        (RunLabel(start_seq=4, sealed_seq=2), 5, RunLabel(start_seq=5, sealed_seq=4)),
        (RunLabel(start_seq=4, unfenced=True), None, RunLabel(unfenced=True, sealed_seq=4)),
        (RunLabel(start_seq=1, sealed_seq=6), 7, RunLabel(start_seq=7, sealed_seq=6)),
    ],
)
def test_a_run_seals_the_last_and_takes_its_requests_fence(
    before: RunLabel, start_seq: int | None, after: RunLabel
) -> None:
    life = new_state(starting=True)
    life.run = before
    with life.condition:
        life.begin_run_locked(start_seq)
    assert life.run == after


@pytest.mark.parametrize("start_seq", [4, 3])
def test_a_run_refuses_a_fence_at_or_below_its_seal_and_keeps_the_seal(start_seq: int) -> None:
    life = new_state(starting=True)
    life.run = RunLabel(start_seq=4, sealed_seq=2)
    with life.condition, pytest.raises(StaleStartFence):
        life.begin_run_locked(start_seq)
    assert life.run == RunLabel(start_seq=4, sealed_seq=4)


def test_a_run_begins_only_inside_a_start() -> None:
    life = new_state()
    with life.condition, pytest.raises(AssertionError):
        life.begin_run_locked(1)


@pytest.mark.parametrize(
    ("before", "start_seq", "after"),
    [
        (RunLabel(), 5, RunLabel(start_seq=5)),
        (RunLabel(start_seq=5), 3, RunLabel(start_seq=5)),
        (RunLabel(start_seq=5), 8, RunLabel(start_seq=8)),
        (RunLabel(start_seq=5), None, RunLabel(start_seq=5, unfenced=True)),
        # U3, open in the spec: a join is not compared with the seal.
        (RunLabel(sealed_seq=9), 2, RunLabel(start_seq=2, sealed_seq=9)),
    ],
)
def test_a_joined_request_raises_the_runs_label(
    before: RunLabel, start_seq: int | None, after: RunLabel
) -> None:
    life = new_state()
    life.run = before
    with life.condition:
        life.join_run_locked(start_seq)
    assert life.run == after


def test_a_transition_that_leaves_a_proven_invariant_broken_fails() -> None:
    life = new_state(lifecycle_check=CHECK)
    with life.condition:
        life.begin_stop_locked(StopHolder.STOP, check=CHECK)
        life.check = None  # as no transition would: the stop outlives its check
        with pytest.raises(LifecycleInvariantError, match="stop_under_check"):
            life.advance_generation_locked()


def test_the_open_invariants_are_named_but_not_enforced() -> None:
    life = new_state(initial_archive_preservation_pending=True)
    life.run = RunLabel(start_seq=2, sealed_seq=5)
    with life.condition:
        life.advance_generation_locked()
    assert life.check_invariants() == []
    assert life.unproven() == ["I5", "U3"]
    assert life.checked_combination()[-2:] == ("unproven:I5", "unproven:U3")


def test_a_start_and_a_stop_in_progress_at_once_break_u1() -> None:
    life = new_state(starting=True, stopping=True)
    assert life.check_invariants() == ["U1"]


def test_the_state_keeps_the_flag_names_the_wire_and_the_logs_use() -> None:
    life = new_state(stopping=True, orphan_marker_unreadable=True)
    flags = life.flags()
    assert tuple(flags) == FLAG_NAMES
    assert [name for name, held in flags.items() if held] == [
        "stopping",
        "orphan_finalization_pending",
        "orphan_marker_unreadable",
    ]
