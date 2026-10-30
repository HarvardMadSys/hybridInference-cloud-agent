"""Database identity, connection switch and migration on this machine (filled in later)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deploy.agent.operations import Executor, Outcome


def register(executor: Executor) -> None:
    """Add the database handlers to ``executor``."""


def local_command(executor: Executor, args: Any) -> Outcome:
    """A local database recovery command."""
    from deploy.agent.operations import Outcome

    return Outcome("rejected", "database commands are not available in this agent version")
