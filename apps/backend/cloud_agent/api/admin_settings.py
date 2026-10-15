"""Administrator access to persisted deployment settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from cloud_agent import settings
from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    log_admin_action,
    require_store,
    verify_admin_access,
)
from cloud_agent.identity_namespace import IdentityNamespaceError
from cloud_agent.source_control import SourceControlError

if TYPE_CHECKING:
    from cloud_agent.ports import AuditRepository, SettingsRepository


class _SettingsStorage(Protocol):
    """The settings capability and the audit trail for accepted changes."""

    settings: SettingsRepository
    audit: AuditRepository


class UpdateSettingsRequest(BaseModel):
    """A partial settings update; omitted keys retain their existing values.

    ``expected_revision`` is the revision the change was reviewed against.
    When given, the update applies only if nothing was saved since, so a
    review cannot silently overwrite another administrator's change.
    """

    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]
    expected_revision: int | None = None


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin/agent/settings",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


@router.get("")
async def read_settings(
    store: _SettingsStorage | None = Depends(get_storage),
) -> dict[str, Any]:
    """Return editable setting definitions and values without exposing secrets."""
    revision, encoded = await require_store(store).settings.read_settings()
    try:
        return settings.settings_response(revision, encoded)
    except (SourceControlError, ValueError):
        raise HTTPException(
            status_code=503,
            detail="Settings cannot be loaded. Check persisted configuration and the server encryption key.",
        ) from None


@router.put("")
async def update_settings(
    payload: UpdateSettingsRequest,
    request: Request,
    store: _SettingsStorage | None = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Validate and atomically persist the submitted settings, recording safe changes."""
    storage = require_store(store)
    try:
        changes = settings.encode_changes(payload.settings)
        revision, encoded, previous = await storage.settings.update_settings(
            changes, expected_revision=payload.expected_revision
        )
    except settings.SettingsRevisionConflict:
        raise HTTPException(
            status_code=409,
            detail=(
                "Settings changed after this review. Refresh the settings, check the "
                "current values, and review the changes again."
            ),
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except IdentityNamespaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except SourceControlError:
        raise HTTPException(
            status_code=503,
            detail="Settings secrets cannot be stored. Check the server encryption key.",
        ) from None

    try:
        settings.apply_settings(revision, encoded)
    except SourceControlError:
        raise HTTPException(
            status_code=503,
            detail="Settings secrets cannot be loaded. Check the server encryption key.",
        ) from None
    current_view = settings.settings_response(revision, encoded)
    defaults = {
        definition["key"]: definition["default"] for definition in current_view["definitions"]
    }
    safe_changes = {}
    for stored_key in changes:
        key = settings.SETTINGS_BY_STORAGE_KEY[stored_key].key
        if key in current_view["secret_configured"]:
            safe_changes[key] = {
                "previous": "[redacted]" if previous.get(stored_key) else None,
                "value": "[redacted]" if current_view["secret_configured"].get(key) else None,
            }
        else:
            safe_changes[key] = {
                "previous": previous.get(stored_key, defaults.get(key)),
                "value": current_view["values"].get(key),
            }
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.settings.update",
        details={"revision": revision, "changes": safe_changes},
        actor_id=admin_id,
    )
    return current_view
