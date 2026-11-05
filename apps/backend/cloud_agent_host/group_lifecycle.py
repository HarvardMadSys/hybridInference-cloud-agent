"""Whether a shared sandbox group admits an operation now, decided from one table.

A group (``shared_session_state._Group``) keeps its lifecycle in separate
fields: the operations in progress (``starting``, ``stopping``,
``snapshotting``), records of what happened (``start_failed``,
``container_lost``), barriers that wait for unfinished work (the ``*_pending``
and ``*_blocked`` fields), and the holders using the group (its operation and
terminal tokens, its members' leases and turns, a lifecycle check).

:class:`GroupLifecycle` is a view over those same fields and keeps no state of
its own. :data:`ADMISSION` lists, for each :class:`Operation`, the checks that
refuse it in the order they are made. The first check that holds decides the
exception and its message, so the order is part of each row: two callers that
check the same state in a different order are two rows.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

from cloud_agent_host.chat_sandbox_models import RetainedChatLost
from cloud_agent_host.session_lifecycle import SessionBusy

if TYPE_CHECKING:
    from cloud_agent_host.sandbox import SandboxError
    from cloud_agent_host.shared_session_state import _Group


class Operation(Enum):
    """What a caller asks a group to admit."""

    #: Write the workspace, open a member's process or turn, or fork.
    MUTATION = "mutation"
    #: Archive or delete while the lifecycle archive fences must hold.
    DELETION = "deletion"
    #: The first archive request on a group: deletion fences, mutation fences, idle.
    ARCHIVE = "archive"
    #: A deletion no archive request holds: deletion fences, mutation fences, idle.
    IDLE_DELETION = "idle_deletion"
    #: Start or reuse the container once no start is in flight.
    START = "start"
    #: Why a caller waiting for an in-flight start still waits.
    START_WAIT = "start_wait"
    #: Why an activation still waits for a transition to finish.
    ACTIVATION_WAIT = "activation_wait"
    #: Run the group for a request, once nothing transitions it.
    RUN = "run"
    #: Count an admitted request in the run the group is in.
    JOIN_RUN = "join_run"
    #: Stop the group.
    STOP = "stop"
    #: Stop the group because it is idle.
    IDLE_STOP = "idle_stop"
    #: Take the group's stop for a final archive retry or finalization.
    TAKE_STOP = "take_stop"
    #: Take the group's stop to finish an orphaned group's final archive.
    ORPHAN_FINALIZATION = "orphan_finalization"
    #: Reserve the group for a lifecycle check, or release an archive barrier.
    LIFECYCLE_CHECK = "lifecycle_check"
    #: Install a control-plane manifest.
    MANIFEST = "manifest"


class Blocker(Enum):
    """One piece of group state an admission check reads."""

    #: A lifecycle check holds the group, and the caller is not let through it.
    LIFECYCLE_CHECK = "lifecycle_check"
    #: As :attr:`LIFECYCLE_CHECK`, unless the group is stopping.
    LIFECYCLE_CHECK_OUTSIDE_STOP = "lifecycle_check_outside_stop"
    SNAPSHOTTING = "snapshotting"
    STARTING = "starting"
    STOPPING = "stopping"
    CONTAINER_LOST = "container_lost"
    INITIAL_ARCHIVE_PRESERVATION = "initial_archive_preservation_pending"
    STOP_ARCHIVE_PENDING = "stop_archive_pending"
    STOP_ARCHIVE_BLOCKED = "stop_archive_blocked"
    ORPHAN_FINALIZATION = "orphan_finalization_pending"
    START_ARCHIVE_COMPLETION = "start_archive_completion_pending"
    #: An archive request holds the group.
    ARCHIVE_REQUEST = "archive_request"
    #: The group's last inspection says its container is not running.
    NOT_RUNNING = "not_running"
    #: An operation or holder keeps the group from being quiescent.
    NOT_QUIESCENT = "not_quiescent"


@dataclass(frozen=True)
class Refusal:
    """One admission check: any of ``blockers`` refuses with ``error(message)``."""

    blockers: tuple[Blocker, ...]
    message: str
    error: type[SandboxError] = SessionBusy


def _busy(*blockers: Blocker, message: str) -> Refusal:
    return Refusal(blockers, message)


_CHECK_IN_PROGRESS: Final = "shared sandbox lifecycle check is in progress"
_START_IN_PROGRESS: Final = "shared workspace start is in progress"
_STOP_IN_PROGRESS: Final = "shared workspace stop is in progress"
_ARCHIVE_RETRY_PENDING: Final = "shared workspace final archive retry is pending"
_ARCHIVE_BLOCKED: Final = "shared workspace final archive is blocked by native flush failure"
_ORPHAN_PENDING: Final = "shared workspace orphan finalization is pending"

_MUTATION: Final = (
    _busy(Blocker.LIFECYCLE_CHECK_OUTSIDE_STOP, message=_CHECK_IN_PROGRESS),
    _busy(Blocker.SNAPSHOTTING, message="shared workspace snapshot capture is in progress"),
    _busy(Blocker.STOPPING, message=_STOP_IN_PROGRESS),
    _busy(Blocker.STARTING, message=_START_IN_PROGRESS),
    _busy(Blocker.STOP_ARCHIVE_BLOCKED, message=_ARCHIVE_BLOCKED),
    _busy(Blocker.STOP_ARCHIVE_PENDING, message=_ARCHIVE_RETRY_PENDING),
    _busy(Blocker.ORPHAN_FINALIZATION, message=_ORPHAN_PENDING),
    _busy(
        Blocker.START_ARCHIVE_COMPLETION,
        message="shared workspace start archive completion is pending",
    ),
)
_DELETION: Final = (
    Refusal(
        (Blocker.CONTAINER_LOST,),
        "shared sandbox container identity was lost",
        RetainedChatLost,
    ),
    _busy(
        Blocker.SNAPSHOTTING,
        Blocker.STARTING,
        Blocker.INITIAL_ARCHIVE_PRESERVATION,
        Blocker.STOP_ARCHIVE_PENDING,
        Blocker.STOP_ARCHIVE_BLOCKED,
        Blocker.ORPHAN_FINALIZATION,
        Blocker.START_ARCHIVE_COMPLETION,
        message="sandbox lifecycle archive must finish before deletion",
    ),
)
_TAKE_STOP: Final = (
    _busy(Blocker.STOPPING, message="shared sandbox group stop is already in progress"),
)
_STOP: Final = (
    *_TAKE_STOP,
    _busy(Blocker.STARTING, message="shared sandbox group start is in progress"),
)

#: For each operation, the checks that refuse it, in the order they are made.
ADMISSION: Final[dict[Operation, tuple[Refusal, ...]]] = {
    Operation.MUTATION: _MUTATION,
    Operation.DELETION: _DELETION,
    Operation.ARCHIVE: (
        *_DELETION,
        *_MUTATION,
        _busy(Blocker.NOT_QUIESCENT, message="sandbox archive requires an idle group"),
    ),
    Operation.IDLE_DELETION: (
        *_DELETION,
        *_MUTATION,
        _busy(Blocker.NOT_QUIESCENT, message="sandbox deletion requires an idle group"),
    ),
    Operation.START: (
        _busy(Blocker.LIFECYCLE_CHECK, message=_CHECK_IN_PROGRESS),
        _busy(Blocker.STOPPING, message=_STOP_IN_PROGRESS),
        _busy(Blocker.STOP_ARCHIVE_PENDING, message=_ARCHIVE_RETRY_PENDING),
        _busy(Blocker.STOP_ARCHIVE_BLOCKED, message=_ARCHIVE_BLOCKED),
        _busy(Blocker.ORPHAN_FINALIZATION, message=_ORPHAN_PENDING),
    ),
    Operation.START_WAIT: (_busy(Blocker.STARTING, message=_START_IN_PROGRESS),),
    Operation.ACTIVATION_WAIT: (
        _busy(Blocker.SNAPSHOTTING, message="shared workspace snapshot is in progress"),
        _busy(Blocker.STARTING, message=_START_IN_PROGRESS),
        _busy(Blocker.STOPPING, message=_STOP_IN_PROGRESS),
    ),
    Operation.RUN: (
        _busy(Blocker.STOP_ARCHIVE_PENDING, message=_ARCHIVE_RETRY_PENDING),
        _busy(Blocker.ORPHAN_FINALIZATION, message=_ORPHAN_PENDING),
    ),
    Operation.JOIN_RUN: (
        _busy(
            Blocker.STOPPING,
            Blocker.NOT_RUNNING,
            message="shared workspace stopped before the request was admitted",
        ),
    ),
    Operation.STOP: _STOP,
    Operation.IDLE_STOP: (
        *_STOP,
        _busy(Blocker.NOT_QUIESCENT, message="shared sandbox group still has active members"),
    ),
    Operation.TAKE_STOP: _TAKE_STOP,
    Operation.ORPHAN_FINALIZATION: (
        _busy(
            Blocker.STOPPING,
            message="shared sandbox orphan finalization is already in progress",
        ),
    ),
    Operation.LIFECYCLE_CHECK: (_busy(Blocker.LIFECYCLE_CHECK, message=_CHECK_IN_PROGRESS),),
    Operation.MANIFEST: (
        _busy(Blocker.LIFECYCLE_CHECK, message=_CHECK_IN_PROGRESS),
        _busy(
            Blocker.ARCHIVE_REQUEST,
            message="shared sandbox archive and deletion is in progress",
        ),
    ),
}

_Holds = Callable[["GroupLifecycle", bool], bool]

_HOLDS: Final[dict[Blocker, _Holds]] = {
    Blocker.LIFECYCLE_CHECK: lambda life, allow: (
        life.group.lifecycle_check is not None and not allow
    ),
    Blocker.LIFECYCLE_CHECK_OUTSIDE_STOP: lambda life, allow: (
        life.group.lifecycle_check is not None and not allow and not life.group.stopping
    ),
    Blocker.SNAPSHOTTING: lambda life, _: life.group.snapshotting,
    Blocker.STARTING: lambda life, _: life.group.starting,
    Blocker.STOPPING: lambda life, _: life.group.stopping,
    Blocker.CONTAINER_LOST: lambda life, _: life.group.container_lost,
    Blocker.INITIAL_ARCHIVE_PRESERVATION: lambda life, _: (
        life.group.initial_archive_preservation_pending
    ),
    Blocker.STOP_ARCHIVE_PENDING: lambda life, _: life.group.stop_archive_pending,
    Blocker.STOP_ARCHIVE_BLOCKED: lambda life, _: life.group.stop_archive_blocked,
    Blocker.ORPHAN_FINALIZATION: lambda life, _: life.group.orphan_finalization_pending,
    Blocker.START_ARCHIVE_COMPLETION: lambda life, _: life.group.start_archive_completion_pending,
    Blocker.ARCHIVE_REQUEST: lambda life, _: life.group.archive_request_id is not None,
    Blocker.NOT_RUNNING: lambda life, _: not life.group.inspection.running,
    Blocker.NOT_QUIESCENT: lambda life, allow: not life.is_quiescent(allow_lifecycle_check=allow),
}


@dataclass(frozen=True)
class GroupLifecycle:
    """The admission decisions over one group's lifecycle fields, which it reads in place.

    ``allow_lifecycle_check`` lets a caller that holds the group's lifecycle
    check through the checks that the check would otherwise refuse.
    """

    group: _Group

    def is_quiescent(self, *, allow_lifecycle_check: bool = False) -> bool:
        """Whether no operation in progress and no holder is using the group."""
        group = self.group
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

    def holds(self, blocker: Blocker, *, allow_lifecycle_check: bool = False) -> bool:
        """Whether the group's state is ``blocker`` now."""
        return _HOLDS[blocker](self, allow_lifecycle_check)

    def _refusing(self, operation: Operation, allow_lifecycle_check: bool) -> Refusal | None:
        for refusal in ADMISSION[operation]:
            if any(
                self.holds(blocker, allow_lifecycle_check=allow_lifecycle_check)
                for blocker in refusal.blockers
            ):
                return refusal
        return None

    def refusal(
        self, operation: Operation, *, allow_lifecycle_check: bool = False
    ) -> SandboxError | None:
        """The exception that refuses ``operation`` now, or ``None`` if it is admitted."""
        refusal = self._refusing(operation, allow_lifecycle_check)
        return None if refusal is None else refusal.error(refusal.message)

    def busy_reason(self, operation: Operation) -> str | None:
        """The message of the first check that refuses ``operation``, for a caller that waits."""
        refusal = self._refusing(operation, False)
        return None if refusal is None else refusal.message

    def admit(self, operation: Operation, *, allow_lifecycle_check: bool = False) -> None:
        """Raise the exception the table names if ``operation`` is refused now."""
        refused = self.refusal(operation, allow_lifecycle_check=allow_lifecycle_check)
        if refused is not None:
            raise refused
