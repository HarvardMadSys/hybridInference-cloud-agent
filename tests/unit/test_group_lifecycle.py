"""The admission table: which group state refuses which operation, with what.

Each case sets some of a real group's lifecycle fields and names the exception
and message the operation must be refused with, or ``None`` when it is
admitted. Cases with several fields set pin the order of the checks, which
decides the message a caller sees.
"""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agent_host.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatLost,
)
from cloud_agent_host.group_lifecycle import ADMISSION, Blocker, GroupLifecycle, Operation
from cloud_agent_host.session_lifecycle import SessionBusy
from cloud_agent_host.shared_session_supervisor import _Group, _Member

CHECK = "shared sandbox lifecycle check is in progress"
CAPTURE = "shared workspace snapshot capture is in progress"
STOP = "shared workspace stop is in progress"
START = "shared workspace start is in progress"
BLOCKED = "shared workspace final archive is blocked by native flush failure"
RETRY = "shared workspace final archive retry is pending"
ORPHAN = "shared workspace orphan finalization is pending"
COMPLETION = "shared workspace start archive completion is pending"
LOST = "shared sandbox container identity was lost"
BEFORE_DELETION = "sandbox lifecycle archive must finish before deletion"
GROUP_STOP = "shared sandbox group stop is already in progress"
GROUP_START = "shared sandbox group start is in progress"

#: Set on a group, a value meaning "a lifecycle check holds it".
HELD_CHECK = object()


def _group(running: bool = True, **fields: Any) -> _Group:
    spec = ChatSandboxSpec(
        thread_id="athr_source",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image",
        shared_live=True,
        resident_slot_capacity=4,
    )
    inspection = ChatSandboxInspection(
        container_id="container-shared",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING if running else ChatSandboxStatus.STOPPED,
        running=running,
        image="sha256:image",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
    )
    group = _Group(spec=spec, inspection=inspection)
    group.members["athr_source"] = _Member(thread_id="athr_source")
    for name, value in fields.items():
        setattr(group, name, value)
    return group


def _leased() -> dict[str, _Member]:
    return {"athr_source": _Member(thread_id="athr_source", lease_token="lease")}


_CASES: list[tuple[Operation, dict[str, Any], bool, type[Exception] | None, str | None]] = [
    # MUTATION: the check, then each operation in progress, then each barrier.
    (Operation.MUTATION, {}, False, None, None),
    (Operation.MUTATION, {"lifecycle_check": HELD_CHECK}, False, SessionBusy, CHECK),
    (Operation.MUTATION, {"lifecycle_check": HELD_CHECK}, True, None, None),
    (
        Operation.MUTATION,
        {"lifecycle_check": HELD_CHECK, "stopping": True},
        False,
        SessionBusy,
        STOP,
    ),
    (
        Operation.MUTATION,
        {"snapshotting": True, "stopping": True, "starting": True},
        False,
        SessionBusy,
        CAPTURE,
    ),
    (Operation.MUTATION, {"stopping": True, "starting": True}, False, SessionBusy, STOP),
    (Operation.MUTATION, {"starting": True}, False, SessionBusy, START),
    (Operation.MUTATION, {"starting": True, "start_failed": True}, False, SessionBusy, START),
    (
        Operation.MUTATION,
        {"stop_archive_blocked": True, "stop_archive_pending": True},
        False,
        SessionBusy,
        BLOCKED,
    ),
    (Operation.MUTATION, {"stop_archive_pending": True}, False, SessionBusy, RETRY),
    (
        Operation.MUTATION,
        {"orphan_finalization_pending": True, "start_archive_completion_pending": True},
        False,
        SessionBusy,
        ORPHAN,
    ),
    (
        Operation.MUTATION,
        {"start_archive_completion_pending": True},
        False,
        SessionBusy,
        COMPLETION,
    ),
    (Operation.MUTATION, {"container_lost": True}, False, None, None),
    (Operation.MUTATION, {"start_failed": True}, False, None, None),
    (Operation.MUTATION, {"initial_archive_preservation_pending": True}, False, None, None),
    (Operation.MUTATION, {"operation_tokens": {"fork:op": "athr_source"}}, False, None, None),
    # DELETION: a lost container first, then any lifecycle archive fence.
    (Operation.DELETION, {"container_lost": True, "starting": True}, False, RetainedChatLost, LOST),
    (Operation.DELETION, {"snapshotting": True}, False, SessionBusy, BEFORE_DELETION),
    (Operation.DELETION, {"starting": True}, False, SessionBusy, BEFORE_DELETION),
    (
        Operation.DELETION,
        {"initial_archive_preservation_pending": True},
        False,
        SessionBusy,
        BEFORE_DELETION,
    ),
    (Operation.DELETION, {"stop_archive_pending": True}, False, SessionBusy, BEFORE_DELETION),
    (Operation.DELETION, {"stop_archive_blocked": True}, False, SessionBusy, BEFORE_DELETION),
    (
        Operation.DELETION,
        {"orphan_finalization_pending": True},
        False,
        SessionBusy,
        BEFORE_DELETION,
    ),
    (
        Operation.DELETION,
        {"start_archive_completion_pending": True},
        False,
        SessionBusy,
        BEFORE_DELETION,
    ),
    (Operation.DELETION, {"stopping": True}, False, None, None),
    (Operation.DELETION, {"lifecycle_check": HELD_CHECK}, False, None, None),
    (Operation.DELETION, {"orphan_marker_unreadable": True}, False, None, None),
    (Operation.DELETION, {"operation_tokens": {"t": "athr_source"}}, False, None, None),
    # ARCHIVE and IDLE_DELETION: deletion fences, then mutation's, then idle.
    (Operation.ARCHIVE, {"starting": True}, False, SessionBusy, BEFORE_DELETION),
    (Operation.ARCHIVE, {"stopping": True}, False, SessionBusy, STOP),
    (Operation.ARCHIVE, {"lifecycle_check": HELD_CHECK}, False, SessionBusy, CHECK),
    (
        Operation.ARCHIVE,
        {"operation_tokens": {"t": "athr_source"}},
        False,
        SessionBusy,
        "sandbox archive requires an idle group",
    ),
    (Operation.ARCHIVE, {}, False, None, None),
    (
        Operation.IDLE_DELETION,
        {"container_lost": True, "stopping": True},
        False,
        RetainedChatLost,
        LOST,
    ),
    (
        Operation.IDLE_DELETION,
        {"terminal_tokens": {"t": "athr_source"}},
        False,
        SessionBusy,
        "sandbox deletion requires an idle group",
    ),
    (
        Operation.IDLE_DELETION,
        {"members": _leased()},
        False,
        SessionBusy,
        "sandbox deletion requires an idle group",
    ),
    (Operation.IDLE_DELETION, {"lifecycle_check": HELD_CHECK}, True, None, None),
    (Operation.IDLE_DELETION, {"lifecycle_check": HELD_CHECK}, False, SessionBusy, CHECK),
    # START: its own order, the retry before the block.
    (
        Operation.START,
        {"lifecycle_check": HELD_CHECK, "stopping": True},
        False,
        SessionBusy,
        CHECK,
    ),
    (Operation.START, {"stopping": True, "stop_archive_pending": True}, False, SessionBusy, STOP),
    (
        Operation.START,
        {"stop_archive_pending": True, "stop_archive_blocked": True},
        False,
        SessionBusy,
        RETRY,
    ),
    (Operation.START, {"stop_archive_blocked": True}, False, SessionBusy, BLOCKED),
    (Operation.START, {"orphan_finalization_pending": True}, False, SessionBusy, ORPHAN),
    (Operation.START, {"starting": True, "start_failed": True}, False, None, None),
    (Operation.START, {"snapshotting": True}, False, None, None),
    (Operation.START, {"start_archive_completion_pending": True}, False, None, None),
    # The waits name the transition they still wait for.
    (Operation.START_WAIT, {"starting": True}, False, SessionBusy, START),
    (Operation.START_WAIT, {"stopping": True, "snapshotting": True}, False, None, None),
    (
        Operation.ACTIVATION_WAIT,
        {"snapshotting": True, "starting": True, "stopping": True},
        False,
        SessionBusy,
        "shared workspace snapshot is in progress",
    ),
    (Operation.ACTIVATION_WAIT, {"starting": True, "stopping": True}, False, SessionBusy, START),
    (Operation.ACTIVATION_WAIT, {"stopping": True}, False, SessionBusy, STOP),
    (Operation.ACTIVATION_WAIT, {"stop_archive_pending": True}, False, None, None),
    # RUN and JOIN_RUN.
    (
        Operation.RUN,
        {"stop_archive_pending": True, "orphan_finalization_pending": True},
        False,
        SessionBusy,
        RETRY,
    ),
    (Operation.RUN, {"orphan_finalization_pending": True}, False, SessionBusy, ORPHAN),
    (Operation.RUN, {"stop_archive_blocked": True, "stopping": True}, False, None, None),
    (
        Operation.JOIN_RUN,
        {"stopping": True},
        False,
        SessionBusy,
        "shared workspace stopped before the request was admitted",
    ),
    (
        Operation.JOIN_RUN,
        {"running": False},
        False,
        SessionBusy,
        "shared workspace stopped before the request was admitted",
    ),
    (Operation.JOIN_RUN, {"starting": True}, False, None, None),
    # Stops.
    (Operation.STOP, {"stopping": True, "starting": True}, False, SessionBusy, GROUP_STOP),
    (Operation.STOP, {"starting": True}, False, SessionBusy, GROUP_START),
    (Operation.STOP, {"operation_tokens": {"t": "athr_source"}}, False, None, None),
    (
        Operation.IDLE_STOP,
        {"starting": True, "members": _leased()},
        False,
        SessionBusy,
        GROUP_START,
    ),
    (
        Operation.IDLE_STOP,
        {"operation_tokens": {"t": "athr_source"}},
        False,
        SessionBusy,
        "shared sandbox group still has active members",
    ),
    (
        Operation.IDLE_STOP,
        {"lifecycle_check": HELD_CHECK},
        False,
        SessionBusy,
        "shared sandbox group still has active members",
    ),
    (Operation.IDLE_STOP, {}, False, None, None),
    (Operation.TAKE_STOP, {"stopping": True}, False, SessionBusy, GROUP_STOP),
    (Operation.TAKE_STOP, {"starting": True}, False, None, None),
    (
        Operation.ORPHAN_FINALIZATION,
        {"stopping": True},
        False,
        SessionBusy,
        "shared sandbox orphan finalization is already in progress",
    ),
    (Operation.ORPHAN_FINALIZATION, {"starting": True}, False, None, None),
    # The lifecycle check and the manifest.
    (Operation.LIFECYCLE_CHECK, {"lifecycle_check": HELD_CHECK}, False, SessionBusy, CHECK),
    (Operation.LIFECYCLE_CHECK, {"stopping": True, "starting": True}, False, None, None),
    (
        Operation.MANIFEST,
        {"lifecycle_check": HELD_CHECK, "archive_request_id": "req"},
        False,
        SessionBusy,
        CHECK,
    ),
    (
        Operation.MANIFEST,
        {"archive_request_id": "req"},
        False,
        SessionBusy,
        "shared sandbox archive and deletion is in progress",
    ),
    (Operation.MANIFEST, {"stopping": True}, False, None, None),
]


@pytest.mark.parametrize(
    ("operation", "fields", "allow_lifecycle_check", "error", "message"), _CASES
)
def test_admission_table_refuses_with_the_rows_exception_and_message(
    operation: Operation,
    fields: dict[str, Any],
    allow_lifecycle_check: bool,
    error: type[Exception] | None,
    message: str | None,
) -> None:
    fields = dict(fields)
    group = _group(running=fields.pop("running", True), **fields)

    refused = group.lifecycle.refusal(operation, allow_lifecycle_check=allow_lifecycle_check)

    if error is None:
        assert refused is None
        group.lifecycle.admit(operation, allow_lifecycle_check=allow_lifecycle_check)
        return
    assert type(refused) is error
    assert str(refused) == message
    with pytest.raises(error) as raised:
        group.lifecycle.admit(operation, allow_lifecycle_check=allow_lifecycle_check)
    assert type(raised.value) is error
    assert str(raised.value) == message


def test_every_operation_has_a_row_and_every_case_set_names_each_operation() -> None:
    assert set(ADMISSION) == set(Operation)
    assert {case[0] for case in _CASES} == set(Operation)


@pytest.mark.parametrize(
    ("operation", "fields", "reason"),
    [
        (Operation.START_WAIT, {"starting": True}, START),
        (Operation.START_WAIT, {}, None),
        (
            Operation.ACTIVATION_WAIT,
            {"snapshotting": True},
            "shared workspace snapshot is in progress",
        ),
        (Operation.ACTIVATION_WAIT, {"stopping": True}, STOP),
        (Operation.ACTIVATION_WAIT, {}, None),
    ],
)
def test_busy_reason_names_the_first_refusing_message(
    operation: Operation, fields: dict[str, Any], reason: str | None
) -> None:
    assert _group(**fields).lifecycle.busy_reason(operation) == reason


@pytest.mark.parametrize(
    ("fields", "allow_lifecycle_check", "quiescent"),
    [
        ({}, False, True),
        ({"lifecycle_check": HELD_CHECK}, False, False),
        ({"lifecycle_check": HELD_CHECK}, True, True),
        ({"snapshotting": True}, False, False),
        ({"starting": True}, False, False),
        ({"stopping": True}, False, False),
        ({"operation_tokens": {"t": "athr_source"}}, False, False),
        ({"terminal_tokens": {"t": "athr_source"}}, False, False),
        ({"members": _leased()}, False, False),
        # Records and barriers are not holders.
        ({"container_lost": True, "start_failed": True}, False, True),
        ({"stop_archive_pending": True, "orphan_finalization_pending": True}, False, True),
    ],
)
def test_quiescence_reads_operations_in_progress_and_holders(
    fields: dict[str, Any], allow_lifecycle_check: bool, quiescent: bool
) -> None:
    lifecycle = _group(**fields).lifecycle
    assert lifecycle.is_quiescent(allow_lifecycle_check=allow_lifecycle_check) is quiescent
    assert (
        lifecycle.holds(Blocker.NOT_QUIESCENT, allow_lifecycle_check=allow_lifecycle_check)
        is not quiescent
    )


def test_a_member_turn_holds_the_group() -> None:
    group = _group()
    group.members["athr_source"].current_turn = object()  # type: ignore[assignment]
    assert not group.lifecycle.is_quiescent()


def test_the_view_reads_the_groups_fields_in_place() -> None:
    group = _group()
    lifecycle = GroupLifecycle(group)
    lifecycle.admit(Operation.MUTATION)
    group.starting = True
    with pytest.raises(SessionBusy, match=START):
        lifecycle.admit(Operation.MUTATION)
