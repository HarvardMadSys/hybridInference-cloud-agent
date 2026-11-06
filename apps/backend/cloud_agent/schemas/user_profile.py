"""An owner's saved display profile, as the store returns it."""

from __future__ import annotations

from pydantic import BaseModel


class ProfileView(BaseModel):
    """An owner's saved display fields and their current revision."""

    first_name: str = ""
    last_name: str = ""
    image_data_url: str | None = None
    revision: int = 0
