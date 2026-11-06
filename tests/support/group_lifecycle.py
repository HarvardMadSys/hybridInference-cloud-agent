"""Set a test group's lifecycle by the flag names it had before ``GroupLifecycleState``.

Tests describe a group's state as the wire and the logs name it --
``stopping``, ``stop_archive_pending``, ``orphan_marker_unreadable`` -- and
these helpers write the equivalent state directly, the way a test arranges
any other field. A ``stopping`` group is stopped by a shutdown retry, the one
holder that needs no lifecycle check, and an ``archive_request_id`` holds the
stop for that request. ``orphan_marker_unreadable`` implies the orphan
finalization it belongs to (I3 in the A2 spec).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from cloud_agent_host.supervisor.group_lifecycle_state import (
    START,
    ExclusiveHolder,
    FinalArchive,
    GroupLifecycleState,
    Hold,
    OrphanFinalization,
    RunLabel,
    StopHolder,
)

if TYPE_CHECKING:
    from cloud_agent_host.supervisor.shared_session_state import _Group

#: The flag names :func:`set_flags` accepts.
FLAGS = frozenset(
    {
        "starting",
        "stopping",
        "snapshotting",
        "lifecycle_check",
        "start_failed",
        "container_lost",
        "initial_archive_preservation_pending",
        "archive_request_id",
        "stop_archive_pending",
        "stop_archive_blocked",
        "stop_archive_members",
        "orphan_finalization_pending",
        "orphan_marker_unreadable",
        "start_archive_completion_pending",
        "lifecycle_generation",
        "start_seq",
        "run_unfenced",
        "sealed_seq",
    }
)


def new_state(*, archives_finals: bool = True, **flags: Any) -> GroupLifecycleState:
    """A lifecycle under a fresh lock, set by :func:`apply_flags`."""
    life = GroupLifecycleState(
        threading.Condition(threading.RLock()), archives_finals=archives_finals
    )
    apply_flags(life, **flags)
    return life


def set_flags(group: _Group, **flags: Any) -> None:
    """Set ``group``'s lifecycle by the old flag names."""
    apply_flags(group.life, **flags)


def apply_flags(life: GroupLifecycleState, **flags: Any) -> None:
    """Write the state the old flags describe into ``life``; unnamed parts are kept."""
    unknown = set(flags) - FLAGS
    if unknown:
        raise TypeError(f"not lifecycle flags: {sorted(unknown)}")
    if "starting" in flags:
        life.start = Hold(START) if flags["starting"] else None
    if "archive_request_id" in flags:
        request = flags["archive_request_id"]
        life.stop = None if request is None else Hold(StopHolder.ARCHIVE_HOLD, request)
    if "stopping" in flags and (life.stop is None) == bool(flags["stopping"]):
        life.stop = Hold(StopHolder.SHUTDOWN_RETRY) if flags["stopping"] else None
    if "snapshotting" in flags:
        life.exclusive = Hold(ExclusiveHolder.INITIAL_ARCHIVE) if flags["snapshotting"] else None
    if "lifecycle_check" in flags:
        life.check = flags["lifecycle_check"]
    if "start_failed" in flags:
        life.last_start_failed = bool(flags["start_failed"])
    if "container_lost" in flags:
        life.container_lost = bool(flags["container_lost"])
    if "initial_archive_preservation_pending" in flags:
        life.initial_preservation = bool(flags["initial_archive_preservation_pending"])
    if {"stop_archive_pending", "stop_archive_blocked", "stop_archive_members"} & set(flags):
        owed = life.final_archive
        pending = flags.get("stop_archive_pending", life.final_archive_pending)
        blocked = flags.get("stop_archive_blocked", life.final_archive_blocked)
        members = flags.get("stop_archive_members", owed.members if owed is not None else None)
        if pending and members is None:
            members = ()
        if not pending:
            members = None
        life.final_archive = (
            None if members is None and not blocked else FinalArchive(members, blocked)
        )
    if {"orphan_finalization_pending", "orphan_marker_unreadable"} & set(flags):
        unreadable = flags.get("orphan_marker_unreadable", life.orphan_marker_unreadable)
        pending = flags.get("orphan_finalization_pending", life.orphan is not None) or unreadable
        life.orphan = OrphanFinalization(bool(unreadable)) if pending else None
    if "start_archive_completion_pending" in flags:
        life.start_completion_pending = bool(flags["start_archive_completion_pending"])
    if "lifecycle_generation" in flags:
        life.generation = flags["lifecycle_generation"]
    run = life.run
    life.run = RunLabel(
        start_seq=flags.get("start_seq", run.start_seq),
        unfenced=flags.get("run_unfenced", run.unfenced),
        sealed_seq=flags.get("sealed_seq", run.sealed_seq),
    )
