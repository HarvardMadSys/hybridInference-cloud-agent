"""Owner-facing staging for files a task carries into its sandbox.

Uploads are staged before the job exists and named by id in the create request.
Sending the bytes with the job instead would make ``POST /v1/agent/jobs``
multipart and change the shape its ``Idempotency-Key`` fingerprints; staging
also means the transfer happens while the user is still typing rather than
after they press Run.

**Why JSON and base64 rather than multipart.** Every other surface in this
service speaks JSON, and there is no binary request or response anywhere in
``cloud_agent/api``. Multipart would add both a new request style and a new
runtime dependency (``python-multipart``) for one endpoint. At a 5 MB ceiling
the encoding costs a third more bytes on the wire and nothing in complexity,
which is the cheaper trade for a service that is about to be read by people who
did not write it.
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.attachments.models import (
    MAX_ATTACHMENT_FILE_BYTES,
    MAX_ATTACHMENT_PATH_LENGTH,
    MAX_CONTENT_TYPE_LENGTH,
    AttachmentError,
    AttachmentView,
    attachment_max_file_bytes,
    attachment_size_label,
    safe_attachment_path,
)

if TYPE_CHECKING:
    from cloud_agent.ports.attachments import AttachmentRepository


router = APIRouter(prefix="/v1/agent/uploads", tags=["agent-uploads"])

#: Base64 expands by 4/3, plus padding. Bounded in the schema so an oversized
#: upload is refused before its base64 is decoded. That does not bound what is
#: read: FastAPI reads and parses the whole JSON body before validation or the
#: owner check runs, so the request's own size is limited in front of this
#: service — the shipped nginx configs allow this route 8 MiB.
_MAX_ENCODED = (MAX_ATTACHMENT_FILE_BYTES // 3 + 1) * 4 + 16


class _UploadsStorage(Protocol):
    """Only the owner's staged uploads are needed here."""

    attachments: AttachmentRepository


class CreateUpload(BaseModel):
    """One file, with the path it will take under ``/uploads``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=MAX_ATTACHMENT_PATH_LENGTH)
    content_base64: str = Field(min_length=1, max_length=_MAX_ENCODED)
    content_type: str = Field(
        default="application/octet-stream", min_length=1, max_length=MAX_CONTENT_TYPE_LENGTH
    )


def upload_http_error(exc: AttachmentError) -> HTTPException:
    """Map a domain refusal to a useful owner-facing error."""
    return HTTPException(
        status_code=400,
        detail={"error": {"type": "invalid_upload", "message": str(exc)}},
    )


@router.post("", response_model=AttachmentView, status_code=201)
async def create_upload(
    body: CreateUpload,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _UploadsStorage | None = Depends(get_storage),
) -> AttachmentView:
    """Stage one file for a task the owner has not submitted yet."""
    try:
        path = safe_attachment_path(body.path)
        content = base64.b64decode(body.content_base64, validate=True)
    except AttachmentError as exc:
        raise upload_http_error(exc) from exc
    except (binascii.Error, ValueError) as exc:
        raise upload_http_error(AttachmentError("Upload content is not valid base64.")) from exc
    try:
        max_file_bytes = attachment_max_file_bytes()
        if len(content) > max_file_bytes:
            raise AttachmentError(
                f"Each file must be {attachment_size_label(max_file_bytes)} or smaller."
            )
        row = await require_store(store).attachments.create_attachment(
            user_id=user["user_id"],
            path=path,
            content=content,
            content_type=body.content_type,
        )
    except AttachmentError as exc:
        raise upload_http_error(exc) from exc
    return AttachmentView.model_validate(row)


@router.delete("/{attachment_id}", status_code=204)
async def delete_upload(
    attachment_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _UploadsStorage | None = Depends(get_storage),
) -> Response:
    """Drop a staged upload the owner removed before running.

    Only a staged one: once a turn has accepted a file, the turn owns it, and
    deleting the conversation is what removes the bytes.
    """
    removed = await require_store(store).attachments.delete_attachment(
        user_id=user["user_id"], attachment_id=attachment_id
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "upload_not_found",
                    "message": "Upload not found, or it already belongs to a task.",
                }
            },
        )
    return Response(status_code=204)
