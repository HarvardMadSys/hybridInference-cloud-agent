"""Skill content, public projections, and shared size bounds."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic model field.
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_agent.settings import integer_setting

MAX_SKILL_BYTES = 64 * 1024
MAX_SELECTION_BYTES = 128 * 1024
MAX_SELECTED_SKILLS = 16
MAX_LIBRARY_SKILLS = 100
MAX_SKILL_NAME_LENGTH = 80
MAX_SKILL_DESCRIPTION_LENGTH = 1024


class SkillLimits(BaseModel):
    """Advertise the server's selection and editor bounds to clients."""

    max_selected: int = Field(default_factory=lambda: skill_max_selected())
    max_selection_bytes: int = Field(default_factory=lambda: skill_max_selection_bytes())
    max_skill_bytes: int = Field(default_factory=lambda: skill_max_bytes())
    max_name_length: int = MAX_SKILL_NAME_LENGTH
    max_description_length: int = MAX_SKILL_DESCRIPTION_LENGTH


def skill_max_bytes() -> int:
    """Read the active editor/import byte limit within the fixed schema cap."""
    return integer_setting("skills.max_skill_bytes", MAX_SKILL_BYTES)


def skill_max_selection_bytes() -> int:
    """Read the combined instruction quota for one turn and account defaults."""
    return integer_setting("skills.max_selection_bytes", MAX_SELECTION_BYTES)


def skill_max_selected() -> int:
    """Read how many skills one turn may select."""
    return integer_setting("skills.max_selected", MAX_SELECTED_SKILLS)


def skill_max_library() -> int:
    """Read the quota for live entries in an owner's library."""
    return integer_setting("skills.max_library", MAX_LIBRARY_SKILLS)


def skill_size_label(size: int) -> str:
    """Describe a configured instruction quota, including sub-kilobyte values."""
    return f"{size / 1024:g} KB" if size >= 1024 else f"{size} bytes"


class SkillError(ValueError):
    """An invalid skill, selection, or import the owner can correct."""


class SkillConflict(SkillError):
    """A duplicate name or a write made against an older revision."""


class SkillContent(BaseModel):
    """The reusable instructions and when an agent should apply them."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=MAX_SKILL_NAME_LENGTH)
    description: str = Field(min_length=1, max_length=MAX_SKILL_DESCRIPTION_LENGTH)
    instructions: str = Field(min_length=1, max_length=MAX_SKILL_BYTES)

    @field_validator("name", "description", "instructions")
    @classmethod
    def text_only(cls, value: str) -> str:
        """Reject NUL and bound UTF-8 bytes, not just Python characters."""
        if "\x00" in value:
            raise ValueError("Skill text cannot contain NUL characters.")
        max_bytes = skill_max_bytes()
        if len(value.encode("utf-8")) > max_bytes:
            raise ValueError(f"A skill must be no larger than {skill_size_label(max_bytes)}.")
        return value


class SkillSummary(BaseModel):
    """One library entry without its potentially large instruction body."""

    id: int
    name: str
    description: str
    source: Literal["created", "uploaded", "github"]
    source_url: str | None = None
    source_revision: str | None = None
    default_enabled: bool = False
    revision: int
    content_revision: int = 1
    instruction_bytes: int = 0
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def instruction_size(cls, value: Any) -> Any:
        """Populate size on full-row writes as well as SQL-projected summaries."""
        if isinstance(value, dict) and "instructions" in value:
            return {**value, "instruction_bytes": len(value["instructions"].encode("utf-8"))}
        return value


class SkillView(SkillSummary):
    """The full skill its owner can inspect."""

    instructions: str


class SelectedSkill(BaseModel):
    """The identity and revision selected for a particular immutable turn."""

    id: int
    name: str
    description: str
    revision: int


class SkillSnapshot(SelectedSkill):
    """The exact selected content, retained even after library deletion."""

    instructions: str
