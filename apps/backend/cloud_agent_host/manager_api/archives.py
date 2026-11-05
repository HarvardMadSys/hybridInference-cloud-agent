"""A group's archives: the pre-prompt baseline, and the admin deletion's final copy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from cloud_agent_host.archive.sandbox_deletion_backup import ArchiveIdentity
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.manager_api.context import shared_spec, supervisor_error
from cloud_agent_host.manager_api.models import (  # noqa: TC001 — FastAPI resolves the request models.
    SandboxArchiveCommitRequest,
    SandboxArchiveDiscardRequest,
    SandboxArchivePrepareRequest,
    SandboxArchiveUploadRequest,
    SharedInitialArchiveRequest,
    SharedInitialArchiveStatusRequest,
)
from cloud_agent_host.manager_api.routing import route
from cloud_agent_host.shared_session_supervisor import archive_failure_http_detail
from cloud_agent_host.workspace_git import COMMIT_SHA as _COMMIT_SHA

if TYPE_CHECKING:
    from starlette.datastructures import State

    from cloud_agent_host.manager_api.context import ManagerContext


def _sandbox_archive_identity(
    thread_id: str, body: SandboxArchivePrepareRequest
) -> ArchiveIdentity:
    return ArchiveIdentity(
        thread_id=thread_id,
        request_id=body.request_id,
        generation=body.generation,
        expected_container_id=body.expected_container_id,
        archive_key=body.archive_key,
    )


class ArchiveRoutes:
    """The initial archive a fresh activation stores, and the admin deletion's archive."""

    def __init__(self, context: ManagerContext, state: State) -> None:
        self._context = context
        self._state = state

    @route("POST", "/shared-sandboxes/{thread_id}/archive-initial/status")
    async def shared_initial_archive_status(
        self, thread_id: str, body: SharedInitialArchiveStatusRequest
    ) -> dict[str, Any]:
        """Resolve a crash-committed pre-prompt baseline before reset/setup."""
        owner = self._context.require_shared_supervisor()
        if thread_id != body.origin_thread_id:
            raise HTTPException(status_code=422, detail="initial archive must name the origin")
        archive_boundary = self._context.archive_boundary
        if archive_boundary is None:
            return {"enabled": False, "prepared": False, "stored": False}
        try:
            return await self._context.endpoint_calls.run(
                owner.capture_initial_archive,
                thread_id,
                body.token,
                expected_spec=shared_spec(body),
                expected_container_id=body.expected_container_id,
                capture=lambda backend, _spec, members: archive_boundary.probe_initial(
                    backend,
                    expected_container_id=body.expected_container_id,
                    preparation_id=body.preparation_id,
                    members=members,
                ),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(
                exc,
                safe_detail=archive_failure_http_detail(exc),
            ) from exc

    @route("POST", "/shared-sandboxes/{thread_id}/archive-initial")
    async def capture_shared_initial_archive(
        self, thread_id: str, body: SharedInitialArchiveRequest
    ) -> dict[str, Any]:
        """Persist the exact prepared workspace before any resident/prompt."""
        owner = self._context.require_shared_supervisor()
        if thread_id != body.origin_thread_id:
            raise HTTPException(status_code=422, detail="initial archive must name the origin")
        if not _COMMIT_SHA.fullmatch(body.base_sha):
            raise HTTPException(status_code=422, detail="base SHA is invalid")
        archive_boundary = self._context.archive_boundary
        if archive_boundary is None:
            return {"enabled": False, "stored": False}
        try:
            return await self._context.endpoint_calls.run(
                owner.capture_initial_archive,
                thread_id,
                body.token,
                expected_spec=shared_spec(body),
                expected_container_id=body.expected_container_id,
                capture=lambda backend, _spec, members: archive_boundary.capture_initial(
                    backend,
                    expected_container_id=body.expected_container_id,
                    preparation_id=body.preparation_id,
                    base_sha=body.base_sha,
                    members=members,
                    defer_upload=body.defer_upload,
                ),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(
                exc,
                safe_detail=archive_failure_http_detail(exc),
            ) from exc

    @route("POST", "/shared-sandboxes/{thread_id}/admin-delete/prepare")
    async def prepare_sandbox_archive(
        self, thread_id: str, body: SandboxArchivePrepareRequest
    ) -> dict[str, Any]:
        """Prepare one admin deletion's archive of the exact stopped group."""
        self._context.require_shared_supervisor()
        try:
            return await self._context.endpoint_calls.run(
                self._state.sandbox_deletion_archive_service.prepare,
                _sandbox_archive_identity(thread_id, body),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @route("POST", "/shared-sandboxes/{thread_id}/admin-delete/upload")
    async def upload_sandbox_archive(
        self, thread_id: str, body: SandboxArchiveUploadRequest
    ) -> dict[str, Any]:
        """Upload the prepared archive to the single object the control plane signed."""
        self._context.require_shared_supervisor()
        try:
            return await self._context.endpoint_calls.run(
                self._state.sandbox_deletion_archive_service.upload,
                _sandbox_archive_identity(thread_id, body),
                sha256=body.sha256,
                size_bytes=body.size_bytes,
                upload_url=body.upload_url,
                upload_headers=body.upload_headers,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @route("POST", "/shared-sandboxes/{thread_id}/admin-delete/commit")
    async def commit_sandbox_archive_deletion(
        self, thread_id: str, body: SandboxArchiveCommitRequest
    ) -> dict[str, Any]:
        """Commit an admin deletion whose stored archive the control plane verified."""
        self._context.require_shared_supervisor()
        try:
            return await self._context.endpoint_calls.run(
                self._state.sandbox_deletion_archive_service.commit,
                _sandbox_archive_identity(thread_id, body),
                sha256=body.sha256,
                size_bytes=body.size_bytes,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @route("POST", "/shared-sandboxes/{thread_id}/admin-delete/discard")
    async def discard_sandbox_without_archive(
        self, thread_id: str, body: SandboxArchiveDiscardRequest
    ) -> dict[str, Any]:
        """Run an admin deletion that uploads no archive."""
        self._context.require_shared_supervisor()
        try:
            return await self._context.endpoint_calls.run(
                self._state.sandbox_deletion_archive_service.discard,
                ArchiveIdentity(
                    thread_id=thread_id,
                    request_id=body.request_id,
                    generation=body.generation,
                    expected_container_id=body.expected_container_id,
                    archive_key="",
                ),
                sandbox_id=body.sandbox_id,
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @route("POST", "/shared-sandboxes/{thread_id}/admin-delete/abort")
    async def abort_sandbox_archive(
        self, thread_id: str, body: SandboxArchivePrepareRequest
    ) -> dict[str, Any]:
        """Abort one prepared admin deletion."""
        self._context.require_shared_supervisor()
        try:
            return await self._context.endpoint_calls.run(
                self._state.sandbox_deletion_archive_service.abort,
                _sandbox_archive_identity(thread_id, body),
            )
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc


__all__ = ["ArchiveRoutes"]
