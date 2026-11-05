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


def sql_list(*values: GroupStateValue) -> str:
    """``values`` as a parenthesized SQL list, e.g. ``('cold', 'restoring')``."""
    return "(" + ", ".join(value.sql for value in values) + ")"
