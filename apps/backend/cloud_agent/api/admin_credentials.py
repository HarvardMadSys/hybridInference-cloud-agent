"""Named administrator API credentials: create (shown once), list, revoke.

A named credential can call the admin API but cannot create or revoke
credentials, so a leaked one cannot extend itself; only ``ADMIN_TOKEN`` or a
Gateway administrator session manages them. ``ADMIN_TOKEN`` itself is not
managed here: it is rotated through the control-plane machines' deployment
agents (``security.admin_token``).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api.deps import (
    ADMIN_CREDENTIAL_PREFIX,
    get_client_ip,
    get_storage,
    log_admin_action,
    require_store,
    verify_admin_access,
)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin/agent/credentials",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


def require_full_authority(admin_id: str) -> None:
    """``ADMIN_TOKEN`` or a Gateway administrator; never a named credential."""
    if admin_id == "admin-token" or admin_id.startswith("admin:"):
        return
    raise HTTPException(
        status_code=403,
        detail="Only ADMIN_TOKEN or a signed-in administrator can do this.",
    )


class CreateCredentialRequest(BaseModel):
    """A new named credential."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
    deployment: bool = False
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "credential_id": row["credential_id"],
        "name": row["name"],
        "scopes": list(row["scopes"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "last_used_at": row["last_used_at"],
    }


@router.get("")
async def list_credentials(store: Any = Depends(get_storage)) -> dict[str, Any]:
    """Every named credential, without any secret."""
    rows = await require_store(store).admin_credentials.list_credentials()
    return {"credentials": [_view(row) for row in rows]}


@router.post("")
async def create_credential(
    body: CreateCredentialRequest,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Create a credential; its secret is in this answer and nowhere else."""
    require_full_authority(admin_id)
    storage = require_store(store)
    credential_id = uuid.uuid4().hex
    secret = f"{ADMIN_CREDENTIAL_PREFIX}{credential_id}_{secrets.token_urlsafe(32)}"
    scopes = ["admin", *(["deployment"] if body.deployment else [])]
    row = await storage.admin_credentials.create_credential(
        credential_id=credential_id,
        name=body.name,
        verifier=hashlib.sha256(secret.encode()).hexdigest(),
        scopes=scopes,
        created_by=admin_id,
        expires_at=None
        if body.expires_in_days is None
        else datetime.now(UTC) + timedelta(days=body.expires_in_days),
    )
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.admin_credential.create",
        details={"credential_id": credential_id, "name": body.name, "scopes": scopes},
        actor_id=admin_id,
    )
    return {**_view(row), "credential": secret}


@router.post("/{credential_id}/revoke")
async def revoke_credential(
    credential_id: str,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Revoke a credential; every process refuses it from the next request."""
    require_full_authority(admin_id)
    storage = require_store(store)
    if not await storage.admin_credentials.revoke_credential(credential_id, revoked_by=admin_id):
        raise HTTPException(status_code=404, detail="No such active credential.")
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.admin_credential.revoke",
        details={"credential_id": credential_id},
        actor_id=admin_id,
    )
    return {"revoked": True}
