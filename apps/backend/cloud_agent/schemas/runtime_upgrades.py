"""Admin views and requests for updating an agent CLI from the Settings page."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from cloud_agent_shared.runtime_downloads import EXACT_VERSION, VERSION_MAX_CHARS


class RuntimeUpgradeProgress(BaseModel):
    """An update an admin started, as every online host reports it."""

    version: str
    state: Literal["downloading", "updated", "failed"] = Field(
        ...,
        description=(
            "downloading until every online host holds and has checked the "
            "version; updated once new chats use it; failed when any host "
            "could not download or check it (new chats keep their version)."
        ),
    )
    progress: int = Field(..., ge=0, le=100, description="Mean of the online hosts' progress.")
    error: str | None = Field(None, description="Which host failed, and why.")
    previous_version: str | None = Field(
        None,
        description="The version new chats used before; null means each server's default.",
    )
    requested_at: str | None = None
    activated_at: str | None = None
    kind: Literal["update", "selection"] = Field(
        "update",
        description=(
            "update for one started with the Update button; selection for a "
            "version an admin chose (Roll back included) that some online host "
            "could not download. A selection is reported only when it failed."
        ),
    )


class RuntimeUpgradeItem(BaseModel):
    """One agent: what new chats run, and what npm has."""

    runtime: str
    current_version: str | None = Field(
        None,
        description=(
            "The version new chats use: the admin's selection, else the image "
            "default every online host agrees on. Null when servers disagree."
        ),
    )
    default_version: str | None = Field(
        None, description="The sandbox image's own default, when every online host agrees."
    )
    latest_version: str | None = Field(None, description="npm's latest release.")
    latest_error: str | None = Field(None, description="Why the latest release is unknown.")
    update_available: bool = Field(
        False, description="Whether the latest release is newer than what new chats use."
    )
    upgrade: RuntimeUpgradeProgress | None = None


class RuntimeUpgradesResponse(BaseModel):
    """Every agent the Settings page can update."""

    runtimes: list[RuntimeUpgradeItem]


class RequestRuntimeUpgradeRequest(BaseModel):
    """Update one agent to one exact npm release."""

    runtime: str = Field(..., min_length=1, max_length=64)
    version: str = Field(
        ...,
        max_length=VERSION_MAX_CHARS,
        pattern=EXACT_VERSION.pattern,
        description="An exact npm version; the page sends the latest release.",
    )
