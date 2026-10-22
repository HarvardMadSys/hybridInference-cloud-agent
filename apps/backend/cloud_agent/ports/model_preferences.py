"""Owner-scoped persistence for the model a new task should start on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cloud_agent.model_preference import ModelPreferenceView


class ModelPreferenceRepository(Protocol):
    """Read and pin one owner's model choice, alongside their newest turn's model."""

    async def get_model_preference(self, user_id: str) -> ModelPreferenceView:
        """Return the pin and the model of the owner's newest turn, resolved.

        An owner who has never chosen gets an empty view, never another owner's
        value: both halves of this read are scoped to ``user_id``.
        """
        ...

    async def set_default_model(self, user_id: str, model: str | None) -> ModelPreferenceView:
        """Pin the default model, or clear the pin with ``None``.

        Clearing returns to following the last used model. The deployment
        default still applies below it, as it does for anyone with no pin.
        """
        ...
