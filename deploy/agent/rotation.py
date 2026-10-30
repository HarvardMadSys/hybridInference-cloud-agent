"""Key rotation steps on this machine (filled in with the rotation workflows)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deploy.agent.operations import Executor


def register(executor: Executor) -> None:
    """Add the rotation handlers to ``executor``."""
