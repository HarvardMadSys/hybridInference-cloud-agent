"""Whether a shared sandbox group admits an operation now, decided from one table.

A group (``shared_session_state._Group``) keeps its lifecycle in one
:class:`~cloud_agent_host.supervisor.group_lifecycle_state.GroupLifecycleState`:
the operations in progress (a start, a stop, the exclusive workspace, a
lifecycle check), records of what happened (the last start failed, the
container was lost), and barriers that wait for unfinished work (a final
archive or an orphan finalization owed, a preserved initial archive, a start
completion). The holders using the group -- its operation and terminal tokens,
its members' leases and turns -- stay on the group.

:class:`GroupLifecycle` is a view over that state and the group's holders and
keeps no state of its own. :data:`ADMISSION` lists, for each :class:`Operation`, the checks that
refuse it in the order they are made. The first check that holds decides the
exception and its message, so the order is part of each row: two callers that
check the same state in a different order are two rows.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

from cloud_agent_host.container.chat_sandbox_models import RetainedChatLost
from cloud_agent_host.session_lifecycle import SessionBusy
from cloud_agent_host.supervisor.group_lifecycle_state import COMBINATIONS

if TYPE_CHECKING:
    from cloud_agent_host.container.sandbox import SandboxError
    from cloud_agent_host.supervisor.group_lifecycle_state import Hold
    from cloud_agent_host.supervisor.shared_session_state import _Group


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
    #: Why the pre-prompt archive still waits before it raises its barrier.
    INITIAL_ARCHIVE_WAIT = "initial_archive_wait"
    #: Why the pre-prompt archive, its barrier raised, still waits for the
    #: requests admitted before it.
    INITIAL_ARCHIVE_DRAIN = "initial_archive_drain"
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
    #: A workspace operation or terminal is open on the group.
    OPEN_MUTATIONS = "open_mutations"


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
    Operation.INITIAL_ARCHIVE_WAIT: (
        _busy(Blocker.STARTING, message=_START_IN_PROGRESS),
        _busy(Blocker.LIFECYCLE_CHECK, message=_CHECK_IN_PROGRESS),
    ),
    Operation.INITIAL_ARCHIVE_DRAIN: (
        _busy(Blocker.STARTING, message=_START_IN_PROGRESS),
        _busy(Blocker.LIFECYCLE_CHECK, message=_CHECK_IN_PROGRESS),
        _busy(Blocker.OPEN_MUTATIONS, message="shared workspace still has active mutations"),
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
    Blocker.LIFECYCLE_CHECK: lambda life, allow: life.group.life.check is not None and not allow,
    Blocker.LIFECYCLE_CHECK_OUTSIDE_STOP: lambda life, allow: (
        life.group.life.check is not None and not allow and life.group.life.stop is None
    ),
    Blocker.SNAPSHOTTING: lambda life, _: life.group.life.exclusive is not None,
    Blocker.STARTING: lambda life, _: life.group.life.start is not None,
    Blocker.STOPPING: lambda life, _: life.group.life.stop is not None,
    Blocker.CONTAINER_LOST: lambda life, _: life.group.life.container_lost,
    Blocker.INITIAL_ARCHIVE_PRESERVATION: lambda life, _: life.group.life.initial_preservation,
    Blocker.STOP_ARCHIVE_PENDING: lambda life, _: life.group.life.final_archive_pending,
    Blocker.STOP_ARCHIVE_BLOCKED: lambda life, _: life.group.life.final_archive_blocked,
    Blocker.ORPHAN_FINALIZATION: lambda life, _: life.group.life.orphan is not None,
    Blocker.START_ARCHIVE_COMPLETION: lambda life, _: life.group.life.start_completion_pending,
    Blocker.ARCHIVE_REQUEST: lambda life, _: life.group.life.archive_request_id is not None,
    Blocker.NOT_RUNNING: lambda life, _: not life.group.inspection.running,
    Blocker.NOT_QUIESCENT: lambda life, allow: not life.is_quiescent(allow_lifecycle_check=allow),
    Blocker.OPEN_MUTATIONS: lambda life, _: bool(
        life.group.operation_tokens or life.group.terminal_tokens
    ),
}


@dataclass(frozen=True)
class GroupLifecycle:
    """The admission decisions over one group's lifecycle state, which it reads in place.

    ``allow_lifecycle_check`` lets a caller that holds the group's lifecycle
    check through the checks that the check would otherwise refuse.
    """

    group: _Group

    def is_quiescent(self, *, allow_lifecycle_check: bool = False) -> bool:
        """Whether no operation in progress and no holder is using the group."""
        group = self.group
        life = group.life
        return (
            (allow_lifecycle_check or life.check is None)
            and life.exclusive is None
            and life.start is None
            and life.stop is None
            and not group.operation_tokens
            and not group.terminal_tokens
            and all(
                member.lease_token is None and member.current_turn is None
                for member in group.members.values()
            )
        )

    def combination(self) -> tuple[str, ...]:
        """The group's operations in progress with their holders, its records and its barriers."""
        return self.group.life.combination()

    def record_combination(self, *, site: str) -> None:
        """Count the group's current :meth:`combination` in this process's counts."""
        COMBINATIONS.observe(self.combination(), site=site)

    def holds(self, blocker: Blocker, *, allow_lifecycle_check: bool = False) -> bool:
        """Whether the group's state is ``blocker`` now."""
        return _HOLDS[blocker](self, allow_lifecycle_check)

    def _refusing(
        self,
        operation: Operation,
        allow_lifecycle_check: bool,
        ignored: Blocker | None = None,
    ) -> Refusal | None:
        for refusal in ADMISSION[operation]:
            if any(
                blocker is not ignored
                and self.holds(blocker, allow_lifecycle_check=allow_lifecycle_check)
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

    def admit_as(self, hold: Hold, operation: Operation) -> None:
        """As :meth:`admit`, for the caller holding the exclusive workspace as ``hold``.

        That hold is the one exclusive workspace the caller expects, so it
        does not count as :attr:`Blocker.SNAPSHOTTING`; everything else is
        checked as usual.
        """
        self.group.life.expect_exclusive_locked(hold)
        refusal = self._refusing(operation, False, ignored=Blocker.SNAPSHOTTING)
        if refusal is not None:
            raise refusal.error(refusal.message)
