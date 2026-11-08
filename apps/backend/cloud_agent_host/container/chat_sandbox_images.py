"""Which image a retained chat container may be created from."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.archive.sandbox_checkpoints import checkpoint_image_reference
from cloud_agent_shared.environment_snapshots import snapshot_image_reference
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.container.chat_sandbox_models import ChatSandboxSpec


def require_create_image(
    spec: ChatSandboxSpec,
    image: str | None,
    *,
    checkpoint_id: str | None,
    environment_snapshot: tuple[str, str] | None,
) -> None:
    """Refuse an image that is not the checkpoint's, the snapshot's or the sandbox's own.

    A moved group is recreated from its checkpoint's local image and nothing
    else, and a group created from an installation snapshot from that
    snapshot's image. Any other override must be the spec's immutable digest.
    """
    if checkpoint_id is not None and environment_snapshot is not None:
        raise SandboxError("a chat sandbox comes from a checkpoint or a snapshot, not both")
    if checkpoint_id is not None:
        if image != checkpoint_image_reference(spec.sandbox_id, checkpoint_id):
            raise SandboxError("chat sandbox restore image does not match its checkpoint")
    elif environment_snapshot is not None:
        if image != snapshot_image_reference(*environment_snapshot):
            raise SandboxError("chat sandbox image does not match its environment snapshot")
    elif (
        image is not None
        and spec.sandbox_image_digest is not None
        and image != spec.sandbox_image_digest
    ):
        raise SandboxError("chat sandbox image override does not match its immutable digest")


__all__ = ["require_create_image"]
