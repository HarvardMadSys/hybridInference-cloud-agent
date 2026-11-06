"""Every lifecycle state a group can be in, enumerated.

The state space of ``GroupLifecycleState`` is finite once its holders are
reduced to their kinds (A2 spec section 7.2): 92,160 states. For each one, and
each ``Operation``, the admission decided from the new state must be the one
the flags it replaced decided. ``_OLD_HOLDS`` below is a frozen copy of the
blocker predicates as they read those flags before the state existed; it is
the reference, and is never to be changed to follow the code.
"""

from __future__ import annotations

import itertools
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final

import pytest

from cloud_agent_host.container.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.supervisor.group_lifecycle import ADMISSION, Blocker, Operation
from cloud_agent_host.supervisor.group_lifecycle_state import (
    START,
    ExclusiveHolder,
    FinalArchive,
    GroupLifecycleState,
    Hold,
    OrphanFinalization,
    StopHolder,
)
from cloud_agent_host.supervisor.shared_session_state import _Group, _Member

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Stands for the lifecycle check a group may hold.
_CHECK: Final[Any] = object()

# The blocker predicates over the flags, frozen as they were before
# GroupLifecycleState replaced the flags (commit a4b4ef436).


def _old_is_quiescent(group: Any, *, allow_lifecycle_check: bool = False) -> bool:
    return (
        (allow_lifecycle_check or group.lifecycle_check is None)
        and not group.snapshotting
        and not group.starting
        and not group.stopping
        and not group.operation_tokens
        and not group.terminal_tokens
        and all(
            member.lease_token is None and member.current_turn is None
            for member in group.members.values()
        )
    )


_OLD_HOLDS: Final[dict[Blocker, Callable[[Any, bool], bool]]] = {
    Blocker.LIFECYCLE_CHECK: lambda group, allow: group.lifecycle_check is not None and not allow,
    Blocker.LIFECYCLE_CHECK_OUTSIDE_STOP: lambda group, allow: (
        group.lifecycle_check is not None and not allow and not group.stopping
    ),
    Blocker.SNAPSHOTTING: lambda group, _: group.snapshotting,
    Blocker.STARTING: lambda group, _: group.starting,
    Blocker.STOPPING: lambda group, _: group.stopping,
    Blocker.CONTAINER_LOST: lambda group, _: group.container_lost,
    Blocker.INITIAL_ARCHIVE_PRESERVATION: lambda group, _: (
        group.initial_archive_preservation_pending
    ),
    Blocker.STOP_ARCHIVE_PENDING: lambda group, _: group.stop_archive_pending,
    Blocker.STOP_ARCHIVE_BLOCKED: lambda group, _: group.stop_archive_blocked,
    Blocker.ORPHAN_FINALIZATION: lambda group, _: group.orphan_finalization_pending,
    Blocker.START_ARCHIVE_COMPLETION: lambda group, _: group.start_archive_completion_pending,
    Blocker.ARCHIVE_REQUEST: lambda group, _: group.archive_request_id is not None,
    Blocker.NOT_RUNNING: lambda group, _: not group.inspection.running,
    Blocker.NOT_QUIESCENT: lambda group, allow: (
        not _old_is_quiescent(group, allow_lifecycle_check=allow)
    ),
    # Read by the initial archive's hand-written drain wait before it was a row.
    Blocker.OPEN_MUTATIONS: lambda group, _: bool(group.operation_tokens or group.terminal_tokens),
}


def _old_initial_archive_wait(group: Any) -> str | None:
    """The initial archive's first wait, as ``capture_initial_archive`` wrote it."""
    return (
        "shared workspace start is in progress"
        if group.starting
        else "shared sandbox lifecycle check is in progress"
        if group.lifecycle_check is not None
        else None
    )


def _old_initial_archive_drain(group: Any) -> str | None:
    """The wait under its raised barrier, as ``capture_initial_archive`` wrote it."""
    return (
        "shared workspace start is in progress"
        if group.starting
        else "shared sandbox lifecycle check is in progress"
        if group.lifecycle_check is not None
        else "shared workspace still has active mutations"
        if group.operation_tokens or group.terminal_tokens
        else None
    )


def _old_refusal(group: Any, operation: Operation, allow: bool) -> tuple[type, str] | None:
    for refusal in ADMISSION[operation]:
        if any(_OLD_HOLDS[blocker](group, allow) for blocker in refusal.blockers):
            return refusal.error, refusal.message
    return None


def _old_flags(group: _Group) -> SimpleNamespace:
    """The flags ``group``'s state stands for, as the old fields held them."""
    life = group.life
    return SimpleNamespace(
        lifecycle_check=life.check,
        snapshotting=life.exclusive is not None,
        starting=life.start is not None,
        stopping=life.stop is not None,
        container_lost=life.container_lost,
        initial_archive_preservation_pending=life.initial_preservation,
        stop_archive_pending=life.final_archive is not None
        and life.final_archive.members is not None,
        stop_archive_blocked=life.final_archive is not None and life.final_archive.blocked,
        orphan_finalization_pending=life.orphan is not None,
        start_archive_completion_pending=life.start_completion_pending,
        archive_request_id=life.archive_request_id,
        inspection=group.inspection,
        operation_tokens=group.operation_tokens,
        terminal_tokens=group.terminal_tokens,
        members=group.members,
    )


# The enumeration (A2 spec section 7.2).

_STOPS: Final = (None, *StopHolder)
_EXCLUSIVES: Final = (None, *ExclusiveHolder)
_FINALS: Final = (
    None,
    FinalArchive(()),
    FinalArchive((), blocked=True),
    FinalArchive(None, blocked=True),
)
_ORPHANS: Final = (None, OrphanFinalization(), OrphanFinalization(marker_unreadable=True))


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_source",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image",
        shared_live=True,
        resident_slot_capacity=4,
    )


def _inspection(spec: ChatSandboxSpec, *, running: bool) -> ChatSandboxInspection:
    return ChatSandboxInspection(
        container_id="container-shared",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING if running else ChatSandboxStatus.STOPPED,
        running=running,
        image="sha256:image",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
    )


def _stop_hold(kind: StopHolder | None) -> Hold | None:
    if kind is None:
        return None
    return Hold(kind, "request" if kind is StopHolder.ARCHIVE_HOLD else None)


def every_state() -> Iterator[_Group]:
    """Each of the 92,160 states, written into one reused group in turn."""
    spec = _spec()
    inspections = {running: _inspection(spec, running=running) for running in (True, False)}
    condition = threading.Condition(threading.RLock())
    lives = {
        finals: GroupLifecycleState(condition, archives_finals=finals) for finals in (True, False)
    }
    group = _Group(spec=spec, inspection=inspections[True], life=lives[True])
    member = _Member(thread_id="athr_source")
    group.members[member.thread_id] = member
    for (
        start,
        stop,
        exclusive,
        check,
        failed,
        lost,
        preservation,
        completion,
        final,
        orphan,
        occupied,
        running,
        finals,
    ) in itertools.product(
        (False, True),
        _STOPS,
        _EXCLUSIVES,
        (False, True),
        (False, True),
        (False, True),
        (False, True),
        (False, True),
        _FINALS,
        _ORPHANS,
        (False, True),
        (False, True),
        (False, True),
    ):
        life = lives[finals]
        life.start = Hold(START) if start else None
        life.stop = _stop_hold(stop)
        life.exclusive = None if exclusive is None else Hold(exclusive)
        life.check = _CHECK if check else None
        life.last_start_failed = failed
        life.container_lost = lost
        life.initial_preservation = preservation
        life.start_completion_pending = completion
        life.final_archive = final
        life.orphan = orphan
        group.life = life
        group.inspection = inspections[running]
        member.lease_token = "lease" if occupied else None
        yield group


def test_the_enumeration_covers_the_92160_states() -> None:
    assert sum(1 for _ in every_state()) == 92_160


def test_every_state_admits_every_operation_as_the_flags_it_replaced_did() -> None:
    mismatches: list[tuple[Operation, bool, tuple[str, ...]]] = []
    for group in every_state():
        old = _old_flags(group)
        lifecycle = group.lifecycle
        for operation in Operation:
            for allow in (False, True):
                refused = lifecycle.refusal(operation, allow_lifecycle_check=allow)
                new = None if refused is None else (type(refused), str(refused))
                if new != _old_refusal(old, operation, allow):
                    mismatches.append((operation, allow, group.life.combination()))
    assert mismatches == []


@pytest.mark.parametrize("allow", [False, True])
def test_the_old_table_never_read_an_unreadable_marker_on_its_own(allow: bool) -> None:
    """``orphan_marker_unreadable`` alone, a state I3 rules out, refused no deletion."""
    group = next(every_state())
    old = _old_flags(group)
    old.orphan_marker_unreadable = True
    assert _old_refusal(old, Operation.DELETION, allow) is None


def test_the_initial_archive_waits_name_what_the_hand_written_waits_named() -> None:
    for group in every_state():
        for tokens in ({}, {"t": "athr_source"}):
            group.operation_tokens = dict(tokens)
            for terminals in ({}, {"t": "athr_source"}):
                group.terminal_tokens = dict(terminals)
                old = _old_flags(group)
                lifecycle = group.lifecycle
                assert lifecycle.busy_reason(
                    Operation.INITIAL_ARCHIVE_WAIT
                ) == _old_initial_archive_wait(old)
                assert lifecycle.busy_reason(
                    Operation.INITIAL_ARCHIVE_DRAIN
                ) == _old_initial_archive_drain(old)
