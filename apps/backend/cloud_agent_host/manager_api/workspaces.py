"""A member's workspace: its files, download, Git view, environment and SSH state."""

from __future__ import annotations

import asyncio
import tempfile
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from cloud_agent_host.container.chat_sandbox import CHAT_WORKDIR, ArchiveExportTooLarge
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.manager_api.context import supervisor_error
from cloud_agent_host.manager_api.models import (  # noqa: TC001 — FastAPI resolves the request models.
    EnvironmentPrepareRequest,
    FileWriteRequest,
    SshAccessRequest,
)
from cloud_agent_host.manager_api.routing import route
from cloud_agent_host.manager_api.workspace_commands import _run_git, _run_git_metadata
from cloud_agent_host.operation_call import open_call
from cloud_agent_host.retained_workspace import (
    RetainedWorkspaceError,
    read_retained_file,
    write_retained_file,
)
from cloud_agent_host.ssh_access import apply_ssh_access
from cloud_agent_host.workspace_download import write_workspace_download
from cloud_agent_shared.operation_profiling import PROFILING_CONTEXT_HEADER
from cloud_agent_shared.sandbox_restore import START_FENCE_HEADER, parse_start_fence
from cloud_agent_shared.workspace_browser import WorkspacePathError, normalize_workspace_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent_host.container.chat_sandbox import RetainedChatBackend
    from cloud_agent_host.manager_api.context import ManagerContext
    from cloud_agent_host.operation_call import SandboxManagerCall


def _relative_path(raw: str | None) -> str:
    try:
        return normalize_workspace_path(raw)
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class WorkspaceRoutes:
    """What an owner reads and changes in one member's workspace."""

    def __init__(self, context: ManagerContext) -> None:
        self._context = context

    @route("GET", "/workspaces/{workspace_id}/files")
    def files(
        self,
        workspace_id: str,
        request: Request,
        path: str = Query("", max_length=4096),
    ) -> dict[str, Any]:
        """List a directory or open a file."""
        settings = self._context.settings
        start_seq = parse_start_fence(request.headers.get(START_FENCE_HEADER))
        relative = _relative_path(path)
        # A path alone cannot say what this request is: the empty path is the
        # root listing, but a non-empty one may be a file preview or a
        # directory expansion, and only the read can tell them apart. So the
        # call is opened for the candidates the caller offered and filed by the
        # kind its own result reports.
        candidates = ("files.list", "files.open") if path else ("files.list",)
        call = open_call(request.headers.get(PROFILING_CONTEXT_HEADER), operations=candidates)

        def _read(retained: Any, active: SandboxManagerCall) -> dict[str, Any]:
            body = read_retained_file(
                retained,
                relative,
                max_preview_bytes=int(settings.applied["workspace_file_preview_max_kib"]) * 1024,
                max_directory_entries=int(settings.applied["workspace_directory_max_entries"]),
                observer=active.recorder,
            )
            kind = str(body.get("kind") or "")
            if kind == "directory":
                active.set_operation("files.list")
            elif kind in {"file", "symlink"}:
                active.set_operation("files.open")
            return body

        return self._context.retained_operation(workspace_id, _read, call=call, start_seq=start_seq)

    @route("PUT", "/workspaces/{workspace_id}/files")
    def write_file(
        self,
        workspace_id: str,
        request: Request,
        body: FileWriteRequest,
        path: str = Query(..., min_length=1, max_length=4096),
    ) -> dict[str, Any]:
        """Replace one file's content."""
        settings = self._context.settings
        relative = _relative_path(path)
        return self._context.retained_operation(
            workspace_id,
            lambda retained, _call: write_retained_file(
                retained,
                relative,
                body.content,
                max_write_bytes=int(settings.applied["workspace_file_write_max_mib"]) * 1024 * 1024,
                max_preview_bytes=int(settings.applied["workspace_file_preview_max_kib"]) * 1024,
                max_directory_entries=int(settings.applied["workspace_directory_max_entries"]),
            ),
            start_seq=parse_start_fence(request.headers.get(START_FENCE_HEADER)),
        )

    @route("GET", "/workspaces/{workspace_id}/archive")
    async def workspace_archive(self, workspace_id: str, request: Request) -> StreamingResponse:
        """Send workspace files without Git metadata, or refuse the download whole.

        Docker's archive boundary reads ``/workspace`` without running anything
        inside the sandbox. Git metadata is filtered from the download only,
        and the archive is spooled before the first byte is sent: a workspace over the download limit is
        refused with 413 rather than arriving as a truncated file. The open
        operation keeps the sandbox from being stopped or deleted underneath
        the copy, and the command slots bound how many copies run at once.
        """
        limit_mib = int(self._context.settings.applied["workspace_download_max_mib"])
        spool = tempfile.TemporaryFile(  # noqa: SIM115 — handed to the response body.
            prefix="freeinference-workspace-download-"
        )

        def export(retained: RetainedChatBackend, _call: SandboxManagerCall) -> dict[str, Any]:
            try:
                with tempfile.TemporaryFile(prefix="freeinference-workspace-export-") as source:
                    retained.manager.export_archive(
                        retained.spec, CHAT_WORKDIR, source, max_bytes=limit_mib * 1024 * 1024
                    )
                    source.seek(0)
                    write_workspace_download(source, spool)
            except ArchiveExportTooLarge as exc:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"The workspace is larger than this server's {limit_mib} MiB "
                        "download limit."
                    ),
                ) from exc
            return {}

        try:
            await self._context.owner_command(
                workspace_id,
                export,
                start_seq=parse_start_fence(request.headers.get(START_FENCE_HEADER)),
            )
        except BaseException:
            spool.close()
            raise
        size = spool.tell()
        spool.seek(0)

        async def body() -> AsyncIterator[bytes]:
            try:
                while chunk := await asyncio.to_thread(spool.read, 1024 * 1024):
                    yield chunk
            finally:
                spool.close()

        return StreamingResponse(
            body(),
            media_type="application/gzip",
            headers={"Content-Length": str(size), "Cache-Control": "no-store"},
        )

    @route("GET", "/workspaces/{workspace_id}/environment/status")
    async def environment_status(self, workspace_id: str, binding_id: str) -> dict[str, Any]:
        """Report how far the workspace's environment preparation has come."""
        context = self._context

        def read_status() -> dict[str, Any]:
            return context.environment_preparation.status(
                context.retained_owner(workspace_id).backend(workspace_id), binding_id
            )

        return await context.endpoint_calls.run(read_status)

    @route("POST", "/workspaces/{workspace_id}/environment/cancel")
    async def environment_cancel(self, workspace_id: str, activation_owner: str) -> dict[str, bool]:
        """Cancel the named activation's environment preparation."""
        context = self._context

        def cancel() -> None:
            context.retained_owner(workspace_id).cancel_environment(workspace_id, activation_owner)

        # The supervisor's lock stays off the event loop, as in ``prepare``.
        await context.endpoint_calls.run(cancel)
        return {"cancelled": True}

    @route("POST", "/workspaces/{workspace_id}/environment/prepare")
    async def prepare_environment(
        self, workspace_id: str, request: Request, body: EnvironmentPrepareRequest
    ) -> dict[str, Any]:
        """Prepare the workspace for its exact environment binding."""
        context = self._context
        # The supervisor's lock stays off the event loop. Preparing starts the
        # group, which proves its network; a boot already prepared runs nothing.
        owner = await context.endpoint_calls.run(context.retained_owner, workspace_id)
        return await context.endpoint_calls.run(
            owner.prepare_environment,
            workspace_id,
            body.environment,
            context.environment_preparation,
            activation_owner=body.activation_owner,
            start_seq=parse_start_fence(request.headers.get(START_FENCE_HEADER)),
        )

    @route("PUT", "/workspaces/{workspace_id}/ssh-access")
    async def sync_ssh_access(self, workspace_id: str, body: SshAccessRequest) -> dict[str, Any]:
        """Converge a running group to its owner's SSH state; a stopped one waits.

        A stopped group has no bridge and no member tmpfs to update; its next
        turn carries the state again before anything runs in it.
        """
        context = self._context

        def sync() -> dict[str, Any]:
            retained = context.retained_owner(workspace_id).backend(workspace_id)
            current = retained.manager.inspect(retained.spec.container_name)
            if current is None or not current.running:
                return {"newest": False, "running": False}
            retained.manager.require_network(retained.spec)
            return {
                **apply_ssh_access(
                    retained.manager,
                    retained.spec,
                    body.access,
                    starting_thread_id=workspace_id if body.starting else None,
                ),
                "running": True,
            }

        try:
            return await context.endpoint_calls.run(sync)
        except RetainedWorkspaceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except (SandboxError, ValueError) as exc:
            raise supervisor_error(exc) from exc

    @route("GET", "/workspaces/{workspace_id}/git")
    async def git(
        self,
        workspace_id: str,
        request: Request,
        base_sha: str | None = Query(None, max_length=64),
        metadata: bool = False,
    ) -> dict[str, Any]:
        """Inspect the workspace's Git state against ``base_sha``."""
        settings = self._context.settings
        operation = _run_git_metadata if metadata else _run_git
        return await self._context.owner_command(
            workspace_id,
            lambda retained, _call: operation(
                retained,
                "/workspace",
                base_sha,
                timeout_s=settings.applied["workspace_command_timeout_s"],
                max_output_bytes=int(settings.applied["workspace_command_output_max_mib"])
                * 1024
                * 1024,
            ),
            start_seq=parse_start_fence(request.headers.get(START_FENCE_HEADER)),
        )


__all__ = ["WorkspaceRoutes"]
