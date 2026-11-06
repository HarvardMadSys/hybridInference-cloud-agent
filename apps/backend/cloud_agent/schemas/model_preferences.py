"""Where the owner's newest turn ran, which a new task starts from."""

from __future__ import annotations

from pydantic import BaseModel


class ModelPreferenceView(BaseModel):
    """The model and connection the owner's newest turn ran on."""

    last_model: str | None = None
    #: The owner connection ``last_model`` ran on. Null for a turn from before
    #: model connections, which no new task starts on.
    last_model_connection_id: str | None = None
