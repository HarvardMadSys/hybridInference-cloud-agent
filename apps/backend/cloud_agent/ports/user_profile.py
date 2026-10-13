"""Persistence for an owner's editable display profile."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cloud_agent.user_profile import ProfileView


class UserProfileRepository(Protocol):
    """Read and save names and pictures under the authenticated owner."""

    async def get_user_profile(self, user_id: str) -> ProfileView:
        """Return saved fields, or an empty profile for a new owner."""
        ...

    async def save_user_profile(
        self,
        user_id: str,
        *,
        first_name: str,
        last_name: str,
        expected_revision: int,
        replace_image: bool,
        image_data_url: str | None,
    ) -> ProfileView:
        """Save only at the revision the owner read, raising ProfileConflict otherwise."""
        ...
