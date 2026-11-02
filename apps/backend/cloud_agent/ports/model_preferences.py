"""Owner-scoped read of the model a new task should start on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cloud_agent.model_preference import ModelPreferenceView


class ModelPreferenceRepository(Protocol):
    """Read where one owner's newest turn ran."""

    async def get_model_preference(self, user_id: str) -> ModelPreferenceView:
        """Return the model and connection of the owner's newest turn.

        An owner with no turn gets an empty view, never another owner's value:
        the read is scoped to ``user_id``.
        """
        ...
