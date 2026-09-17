"""Authenticated personal skill management and review-before-import routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cloud_agent.api.deps import get_agent_app_credentials, get_storage, require_store
from cloud_agent.api.routes import require_agent_owner
from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.github_app import GitHubAppError
from cloud_agent.skills.imports import (
    SkillImportNotFound,
    fetch_github_skill,
    github_skill_location,
    parse_skill_document,
)
from cloud_agent.skills.models import (
    MAX_SKILL_BYTES,
    SkillConflict,
    SkillContent,
    SkillError,
    SkillSummary,
    SkillView,
)

if TYPE_CHECKING:
    from cloud_agent.ports.skills import SkillRepository
    from cloud_agent.ports.source_control import SourceControlRepository


router = APIRouter(prefix="/v1/agent/skills", tags=["agent-skills"])


class _SkillsStorage(Protocol):
    """Only the owner's library and source-control entitlements are needed."""

    skills: SkillRepository
    source_control: SourceControlRepository


class SkillList(BaseModel):
    """Lightweight library entries for Integrations and conversation pickers."""

    skills: list[SkillSummary]


class CreateSkill(SkillContent):
    """Create instructions directly or after reviewing an uploaded Markdown file."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: Literal["created", "uploaded"] = "created"
    default_enabled: bool = False


class UpdateSkill(BaseModel):
    """A revision-fenced partial edit; null never silently clears content."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    name: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, min_length=1, max_length=1024)
    instructions: str | None = Field(None, min_length=1, max_length=MAX_SKILL_BYTES)
    default_enabled: bool | None = None


class SkillImportPreviewRequest(BaseModel):
    """Preview a GitHub link or an uploaded SKILL.md, without writing a skill."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(None, max_length=2048)
    content: str | None = Field(None, max_length=MAX_SKILL_BYTES)


class SkillImportPreview(SkillContent):
    """The exact content presented to the owner before confirmation."""

    source: Literal["uploaded", "github"]
    source_url: str | None = None
    source_revision: str | None = None
    content_digest: str | None = None


class ImportGitHubSkill(BaseModel):
    """Confirm the bytes reviewed; a moving branch must not replace them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=80)
    default_enabled: bool = False


def skill_http_error(exc: SkillError) -> HTTPException:
    """Map a domain refusal to a useful owner-facing error."""
    return HTTPException(
        status_code=409 if isinstance(exc, SkillConflict) else 400,
        detail={
            "error": {
                "type": "skill_conflict" if isinstance(exc, SkillConflict) else "invalid_skill",
                "message": str(exc),
            }
        },
    )


def skill_not_found() -> HTTPException:
    """Use the owner API error envelope for missing library entries."""
    return HTTPException(
        status_code=404,
        detail={
            "error": {
                "type": "skill_not_found",
                "message": "Skill not found. Refresh your library.",
            }
        },
    )


async def _github_preview(
    url: str, *, user_id: str, store: _SkillsStorage, credentials: Any
) -> dict[str, str]:
    """Prefer entitled, repository-scoped reads; public imports need no connection."""
    location = github_skill_location(url)
    if credentials is not None:
        try:
            await require_entitled_repo(
                location.repo, user_id, store=store.source_control, app_credentials=credentials
            )
        except (RepoNotAllowed, GitHubAppError):
            # An unrelated public skill remains importable without granting this
            # owner access to any connected account's private repositories.
            pass
        else:
            return await fetch_github_skill(url, credentials=credentials)
    try:
        return await fetch_github_skill(url)
    except SkillImportNotFound as exc:
        raise SkillError(
            "SKILL.md was not found or your connected GitHub account cannot access it. "
            "Check the URL, or upload the file."
        ) from exc


@router.get("", response_model=SkillList)
async def list_skills(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SkillsStorage | None = Depends(get_storage),
) -> SkillList:
    """List only the authenticated owner's library."""
    rows = await require_store(store).skills.list_skills(user_id=user["user_id"])
    return SkillList(skills=[SkillSummary.model_validate(row) for row in rows])


@router.post("", response_model=SkillView, status_code=201)
async def create_skill(
    body: CreateSkill,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SkillsStorage | None = Depends(get_storage),
) -> SkillView:
    """Save custom or uploaded instructions after explicit confirmation."""
    try:
        row = await require_store(store).skills.create_skill(
            user_id=user["user_id"], values=body.model_dump()
        )
    except SkillError as exc:
        raise skill_http_error(exc) from exc
    return SkillView.model_validate(row)


@router.post("/import/preview", response_model=SkillImportPreview)
async def preview_import(
    body: SkillImportPreviewRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SkillsStorage | None = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> SkillImportPreview:
    """Validate and display instructions without installing anything."""
    if (body.url is None) == (body.content is None):
        raise skill_http_error(SkillError("Provide one GitHub URL or one SKILL.md file."))
    try:
        if body.content is not None:
            return SkillImportPreview(
                **parse_skill_document(body.content).model_dump(), source="uploaded"
            )
        assert body.url is not None
        result = await _github_preview(
            body.url, user_id=user["user_id"], store=require_store(store), credentials=credentials
        )
        return SkillImportPreview.model_validate(result)
    except SkillError as exc:
        raise skill_http_error(exc) from exc


@router.post("/import", response_model=SkillView, status_code=201)
async def import_github_skill(
    body: ImportGitHubSkill,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SkillsStorage | None = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> SkillView:
    """Refetch and compare the approved content before storing its source."""
    storage = require_store(store)
    try:
        result = await _github_preview(
            body.url, user_id=user["user_id"], store=storage, credentials=credentials
        )
        if result.pop("content_digest") != body.content_digest:
            raise SkillConflict("The GitHub skill changed. Preview it again before importing.")
        content = SkillContent.model_validate({**result, "name": body.name})
        row = await storage.skills.create_skill(
            user_id=user["user_id"],
            values={**result, **content.model_dump(), "default_enabled": body.default_enabled},
        )
    except ValidationError as exc:
        raise skill_http_error(SkillError("Provide a valid skill name.")) from exc
    except SkillError as exc:
        raise skill_http_error(exc) from exc
    return SkillView.model_validate(row)


@router.get("/{skill_id}", response_model=SkillView)
async def get_skill(
    skill_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SkillsStorage | None = Depends(get_storage),
) -> SkillView:
    """Read the current content, never another owner's skill."""
    row = await require_store(store).skills.get_skill(user_id=user["user_id"], skill_id=skill_id)
    if row is None:
        raise skill_not_found()
    return SkillView.model_validate(row)


@router.patch("/{skill_id}", response_model=SkillView)
async def update_skill(
    skill_id: int,
    body: UpdateSkill,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SkillsStorage | None = Depends(get_storage),
) -> SkillView:
    """Edit local content or change a default without mutating existing turns."""
    storage = require_store(store)
    current = await storage.skills.get_skill(user_id=user["user_id"], skill_id=skill_id)
    if current is None:
        raise skill_not_found()
    values = body.model_dump(exclude_unset=True, exclude={"revision"})
    if not values or any(value is None for value in values.values()):
        raise skill_http_error(SkillError("Provide nonempty changes to this skill."))
    if current["source"] == "github" and any(key != "default_enabled" for key in values):
        raise skill_http_error(SkillError("Imported GitHub skills are read-only."))
    try:
        content = SkillContent.model_validate({**current, **values})
        values.update({key: value for key, value in content.model_dump().items() if key in values})
        row = await storage.skills.update_skill(
            user_id=user["user_id"], skill_id=skill_id, revision=body.revision, values=values
        )
    except ValidationError as exc:
        raise skill_http_error(
            SkillError("Skill fields must contain valid, nonempty text.")
        ) from exc
    except SkillError as exc:
        raise skill_http_error(exc) from exc
    if row is None:
        raise skill_not_found()
    return SkillView.model_validate(row)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: int,
    revision: int = Query(ge=1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SkillsStorage | None = Depends(get_storage),
) -> Response:
    """Remove a library entry; conversation snapshots remain available."""
    try:
        removed = await require_store(store).skills.delete_skill(
            user_id=user["user_id"], skill_id=skill_id, revision=revision
        )
    except SkillError as exc:
        raise skill_http_error(exc) from exc
    if not removed:
        raise skill_not_found()
    return Response(status_code=204)
