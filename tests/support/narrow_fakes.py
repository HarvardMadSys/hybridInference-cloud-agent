"""Fakes for capabilities the storage assembly types with their repository class.

Such a capability has no port that mirrors its repository: each caller declares
the narrow Protocol it uses, and the repository satisfies it structurally. Its
name is therefore not one :class:`tests.support.storage.AllCapabilities`
resolves. A fake for it is ``create_autospec`` of the caller's Protocol, so a
call that drifts from that Protocol's signature fails here as it would under
mypy, and the answers come from the in-memory state the test arranged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

from cloud_agent.api.config import ModelPreferenceReader
from cloud_agent.schemas.model_preferences import ModelPreferenceView

if TYPE_CHECKING:
    from collections.abc import Mapping


def newest_turn_preferences(jobs: Mapping[str, Mapping[str, Any]]) -> Any:
    """A :class:`ModelPreferenceReader` answering from a fake's jobs.

    The newest owned turn's model and connection, as the repository derives
    them. ``jobs`` is read on every call, so turns created later count.
    A test that wants another answer sets ``get_model_preference.side_effect``.
    """

    async def get_model_preference(user_id: str) -> ModelPreferenceView:
        owned = [job for job in jobs.values() if job.get("user_id") == user_id]
        if not owned:
            return ModelPreferenceView()
        newest = max(owned, key=lambda job: job["id"])
        return ModelPreferenceView(
            last_model=newest["model"], last_model_connection_id=newest.get("model_connection_id")
        )

    reader = create_autospec(ModelPreferenceReader, instance=True)
    reader.get_model_preference.side_effect = get_model_preference
    return reader
