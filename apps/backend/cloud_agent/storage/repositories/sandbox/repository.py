"""The retained-sandbox repository, composed of one mixin per aggregate.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage.repositories.sandbox.admin_deletion import AdminDeletionMixin
from cloud_agent.storage.repositories.sandbox.checkpoints import CheckpointMixin
from cloud_agent.storage.repositories.sandbox.members import MemberLifecycleMixin
from cloud_agent.storage.repositories.sandbox.observations import ObservationMixin
from cloud_agent.storage.repositories.sandbox.stops import GroupStopMixin
from cloud_agent.storage.repositories.sandbox.workspace import WorkspaceMixin

if TYPE_CHECKING:
    import asyncpg


class PostgresSandboxRepository(
    AdminDeletionMixin,
    GroupStopMixin,
    WorkspaceMixin,
    MemberLifecycleMixin,
    CheckpointMixin,
    ObservationMixin,
):
    """One chat, one restartable sandbox: reserve, admit, settle, abandon."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool
