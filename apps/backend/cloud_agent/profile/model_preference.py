"""Which model connection and model a new task should start on.

A new task starts where the owner's newest turn ran: the same model on the
same model connection, while that connection is still enabled and still lists
the model. Otherwise nothing is pre-selected and the composer waits for the
owner to choose; there is deliberately no "first model in the list" fallback,
because the model decides cost, speed and capability.

The remembered choice is **derived, never written**: it is read from the
owner's newest turn, so the single source of truth stays the job row that was
really created.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloud_agent.schemas.model_preferences import ModelPreferenceView


def resolve_connection_default(
    preference: ModelPreferenceView,
    connections: list[dict[str, object]],
) -> tuple[str, str] | None:
    """``(model, connection_id)`` when a new task should start where the last one ran.

    Only while that connection is still enabled and still lists the model.
    """
    connection_id = preference.last_model_connection_id
    if not connection_id or not preference.last_model:
        return None
    for connection in connections:
        listed = connection.get("models")
        if (
            connection.get("id") == connection_id
            and connection.get("enabled")
            and isinstance(listed, list)
            and preference.last_model in listed
        ):
            return preference.last_model, connection_id
    return None
