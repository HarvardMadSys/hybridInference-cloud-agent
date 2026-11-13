"""The two lifecycle axes of a sandbox group (``agent_sandboxes``).

``state`` is where the group's container is in its life; ``deletion_state``
is how far an administrator's deletion of it has gone. They move
independently: a group being deleted still has a container state, and a
``failed`` deletion leaves the group serving its chats.

The store writes these values and the API reads them. Storage SQL names them
through :attr:`GroupStateValue.sql` and :func:`sql_list` rather than spelling
the literals out, so every value a statement compares with is one defined here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class GroupStateValue(StrEnum):
    """A value of one of the two axes, with its SQL spelling."""

    @property
    def sql(self) -> str:
        """The value as an SQL string literal, e.g. ``'running'``."""
        return f"'{self.value}'"


class GroupState(GroupStateValue):
    """``agent_sandboxes.state``: where the group's container is."""

    PROVISIONING = "provisioning"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STOPPED_DIRTY = "stopped_dirty"
    LOST = "lost"
    #: Handed over: the group lives in its checkpoint until a host restores it.
    COLD = "cold"
    RESTORING = "restoring"


class DeletionState(GroupStateValue):
    """``agent_sandboxes.deletion_state``: how far the group's deletion has gone."""

    NONE = "none"
    PENDING = "pending"
    ARCHIVED = "archived"
    DELETED = "deleted"
    FAILED = "failed"


#: The deletion states of a group whose deletion is in progress or done. A
#: deletion that failed before its archive (``failed``) kept the container,
#: and the group serves its chats as one never asked to be deleted: it is
#: counted, moved out, handed over and lost with its machine like any other.
DELETED_GROUP_STATES = (DeletionState.PENDING, DeletionState.ARCHIVED, DeletionState.DELETED)


#: The ``state`` a new group starts in: the column's default, which the
#: reservation that inserts the group leaves to the database.
INITIAL_GROUP_STATE = GroupState.PROVISIONING

#: For each ``state``, the other states the store's writes can move a group to.
#: It is what the statements do today, not a policy they are checked against:
#: ``tests/unit/test_sandbox_group_transitions.py`` names every statement that
#: writes the column and the changes each can make, and requires this table to
#: be exactly their union. Several statements do not test the state they
#: replace (a turn's settlement, a confirmed start, Fork's materialization, a
#: discard), so ``running``, ``lost`` and ``stopping`` are reachable from every
#: state. Nothing writes ``stopped_dirty``; a row may still carry it.
GROUP_STATE_TRANSITIONS: Mapping[GroupState, frozenset[GroupState]] = {
    GroupState.PROVISIONING: frozenset(
        {
            GroupState.STARTING,
            GroupState.RUNNING,
            GroupState.STOPPING,
            GroupState.STOPPED,
            GroupState.LOST,
        }
    ),
    GroupState.STARTING: frozenset(
        {GroupState.PROVISIONING, GroupState.RUNNING, GroupState.STOPPING, GroupState.LOST}
    ),
    GroupState.RUNNING: frozenset(
        {GroupState.PROVISIONING, GroupState.STOPPING, GroupState.STOPPED, GroupState.LOST}
    ),
    GroupState.STOPPING: frozenset(
        {GroupState.PROVISIONING, GroupState.RUNNING, GroupState.STOPPED, GroupState.LOST}
    ),
    GroupState.STOPPED: frozenset(
        {
            GroupState.PROVISIONING,
            GroupState.STARTING,
            GroupState.RUNNING,
            GroupState.STOPPING,
            GroupState.LOST,
            GroupState.COLD,
        }
    ),
    GroupState.STOPPED_DIRTY: frozenset(
        {
            GroupState.PROVISIONING,
            GroupState.RUNNING,
            GroupState.STOPPING,
            GroupState.STOPPED,
            GroupState.LOST,
        }
    ),
    GroupState.LOST: frozenset(
        {GroupState.PROVISIONING, GroupState.RUNNING, GroupState.STOPPING, GroupState.STOPPED}
    ),
    GroupState.COLD: frozenset(
        {
            GroupState.PROVISIONING,
            GroupState.RUNNING,
            GroupState.STOPPING,
            GroupState.LOST,
            GroupState.RESTORING,
        }
    ),
    GroupState.RESTORING: frozenset(
        {GroupState.RUNNING, GroupState.STOPPING, GroupState.LOST, GroupState.COLD}
    ),
}

#: The ``deletion_state`` a new group starts in, the column's default.
INITIAL_DELETION_STATE = DeletionState.NONE

#: For each ``deletion_state``, the other states the store's writes can move a
#: group to, derived and tested like :data:`GROUP_STATE_TRANSITIONS`. A resumed
#: request returns to ``archived`` when it holds an archive and to ``pending``
#: otherwise; an ``archived`` request always holds one, so it never goes back.
DELETION_STATE_TRANSITIONS: Mapping[DeletionState, frozenset[DeletionState]] = {
    DeletionState.NONE: frozenset({DeletionState.PENDING}),
    DeletionState.PENDING: frozenset(
        {DeletionState.ARCHIVED, DeletionState.DELETED, DeletionState.FAILED}
    ),
    DeletionState.ARCHIVED: frozenset({DeletionState.DELETED, DeletionState.FAILED}),
    DeletionState.DELETED: frozenset(),
    DeletionState.FAILED: frozenset({DeletionState.PENDING, DeletionState.ARCHIVED}),
}


def group_state_change_allowed(old: GroupState, new: GroupState) -> bool:
    """Whether a write that moves a group from ``old`` to ``new`` is one the store makes."""
    return old == new or new in GROUP_STATE_TRANSITIONS[old]


def deletion_state_change_allowed(old: DeletionState, new: DeletionState) -> bool:
    """Whether a write that moves a deletion from ``old`` to ``new`` is one the store makes."""
    return old == new or new in DELETION_STATE_TRANSITIONS[old]


def sql_list(*values: GroupStateValue) -> str:
    """``values`` as a parenthesized SQL list, e.g. ``('cold', 'restoring')``."""
    return "(" + ", ".join(value.sql for value in values) + ")"
