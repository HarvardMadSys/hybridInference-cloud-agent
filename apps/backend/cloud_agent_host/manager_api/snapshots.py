"""Installation snapshots: capturing an environment run's container, and retiring them."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from cloud_agent_host.container.chat_sandbox_models import ENVIRONMENT_TEST_LABEL
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.environment_tests import environment_run_spec
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.manager_api.models import (  # noqa: TC001 — FastAPI resolves the request models.
    EnvironmentSnapshotCaptureRequest,
    EnvironmentSnapshotDeleteRequest,
)
from cloud_agent_host.manager_api.routing import route

if TYPE_CHECKING:
    from cloud_agent_host.manager_api.context import ManagerContext

# The sandbox manager's own logger: its snapshot events keep their name.
logger = get_logger("cloud_agent_host.sandbox_manager")


class SnapshotRoutes:
    """What the runner asks of this host's installation snapshots."""

    def __init__(self, context: ManagerContext) -> None:
        self._context = context

    @route("POST", "/environment-snapshots/capture")
    async def capture_installation_snapshot(
        self, body: EnvironmentSnapshotCaptureRequest
    ) -> dict[str, Any]:
        """Commit and upload a stopped environment run's container, and nothing else.

        Only a container labelled as that exact run qualifies, so a runner
        cannot have a chat's workspace uploaded as someone's snapshot.
        """
        archive_controller = self._context.archive_controller
        retained_manager = self._context.retained_manager
        if archive_controller is None or retained_manager is None:
            raise HTTPException(status_code=409, detail="snapshots are not configured here")
        spec = environment_run_spec(body.run_id, body.lease_generation, body.sandbox_image_digest)
        try:
            # ``docker container inspect`` can take its whole timeout on a slow
            # daemon, so it runs off the event loop like the capture itself.
            inspection = await self._context.endpoint_calls.run(
                retained_manager.inspect, spec.container_name
            )
            if (
                inspection is None
                or inspection.container_id != body.container_id
                or inspection.labels.get(ENVIRONMENT_TEST_LABEL) != body.run_id
            ):
                raise HTTPException(status_code=404, detail="environment run container not found")
            return await self._context.endpoint_calls.run(
                archive_controller.capture_environment_snapshot,
                spec,
                inspection,
                owner=body.owner,
                snapshot_id=body.snapshot_id,
            )
        except (SandboxError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc

    @route("POST", "/environment-snapshots/images")
    async def environment_snapshot_images(self) -> dict[str, Any]:
        """Name the snapshot images on this host, so the runner can ask which are retired."""
        archive_controller = self._context.archive_controller
        if archive_controller is None:
            return {"snapshot_ids": []}
        try:
            return {
                "snapshot_ids": await self._context.endpoint_calls.run(
                    archive_controller.environment_snapshot_images
                )
            }
        except SandboxError as exc:
            raise HTTPException(status_code=503, detail=str(exc)[:300]) from exc

    @route("POST", "/environment-snapshots/delete")
    async def delete_environment_snapshots(
        self, body: EnvironmentSnapshotDeleteRequest
    ) -> dict[str, Any]:
        """Drop retired snapshots' local images, and delete the stored objects named."""
        archive_controller = self._context.archive_controller
        if archive_controller is None:
            return {"deleted": []}
        deleted: list[str] = []

        def run() -> None:
            for item in body.snapshots:
                with contextlib.suppress(Exception):
                    archive_controller.remove_environment_snapshot_image(
                        item.owner, item.snapshot_id
                    )
                if item.object_key is None:
                    continue
                try:
                    archive_controller.delete_environment_snapshot(
                        owner=item.owner,
                        snapshot_id=item.snapshot_id,
                        key=item.object_key,
                        version_id=item.version_id,
                    )
                except Exception as exc:
                    # Without the delete permission the bucket's lifecycle
                    # rule removes the object later.
                    logger.warning(
                        "environment snapshot object was not deleted",
                        extra={
                            "event": "environment_snapshot_delete_failed",
                            "snapshot_id": item.snapshot_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                deleted.append(item.snapshot_id)

        await self._context.endpoint_calls.run(run)
        return {"deleted": deleted}


__all__ = ["SnapshotRoutes"]
