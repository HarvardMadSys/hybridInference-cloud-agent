"""Skill content, public projections, and shared size bounds."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic model field.
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SKILL_BYTES = 64 * 1024
MAX_SELECTION_BYTES = 128 * 1024
MAX_SELECTED_SKILLS = 16
MAX_LIBRARY_SKILLS = 100


class SkillError(ValueError):
    """An invalid skill, selection, or import the owner can correct."""


class SkillConflict(SkillError):
    """A duplicate name or a write made against an older revision."""


class SkillContent(BaseModel):
    """The reusable instructions and when an agent should apply them."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=1, max_length=MAX_SKILL_BYTES)

    @field_validator("name", "description", "instructions")
    @classmethod
    def text_only(cls, value: str) -> str:
        """Reject NUL and bound UTF-8 bytes, not just Python characters."""
        if "\x00" in value:
            raise ValueError("Skill text cannot contain NUL characters.")
        if len(value.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ValueError("A skill must be no larger than 64 KB.")
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
    created_at: datetime
    updated_at: datetime


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
