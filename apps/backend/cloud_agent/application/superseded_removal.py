"""What release says when a Host does not remove a container its moved groups left behind."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.ports.external.compute_provider import ProviderError

if TYPE_CHECKING:
    from cloud_agent.sandbox_manager_client import SandboxManagerError

logger = get_logger(__name__)

# Only exact, source-defined refusal messages may leave the manager. Shape
# checks cannot distinguish a fixed reason from arbitrary exception contents.
_SAFE_REASONS = frozenset(
    {
        "another sandbox archive is in progress",
        "archived sandbox identity changed during deletion",
        "sandbox archive generation is stale",
        "sandbox archive identity changed",
        "sandbox archive request cannot be discarded",
        "sandbox archive request is stale",
        "sandbox archive requires an idle group",
        "sandbox deletion requires an idle group",
        "sandbox discard request identity changed",
        "sandbox discard target is no longer absent",
        "sandbox lifecycle archive must finish before deletion",
        "shared sandbox container identity was lost",
        "shared sandbox lifecycle check is in progress",
        "shared workspace creation is in progress",
        "shared workspace final archive is blocked by native flush failure",
        "shared workspace final archive retry is pending",
        "shared workspace orphan finalization is pending",
        "shared workspace snapshot capture is in progress",
        "shared workspace start archive completion is pending",
        "shared workspace start is in progress",
        "shared workspace stop is in progress",
    }
)


# These identity fences cannot change for the already-authorized request. A
# busy archive can finish; replaying the same stale identity cannot fix it.
_STALE_REQUEST_REASONS = frozenset(
    {
        "sandbox archive generation is stale",
        "sandbox archive identity changed",
        "sandbox discard request identity changed",
        "sandbox archive request cannot be discarded",
        "sandbox discard target is no longer absent",
    }
)


def superseded_refusal(
    host: dict[str, Any],
    container: dict[str, Any],
    exc: SandboxManagerError,
    *,
    operation_id: str | None = None,
) -> ProviderError:
    """The error one refused or unanswered removal stops release with.

    Busy, unreachable or overloaded passes; the request is the same one next
    time. A refusal of the request itself waits for an administrator. Either
    way a recognized fixed reason is kept in the log and on the operation.
    Unknown details are omitted; the status and container still identify
    the failed request.
    """
    status = exc.status_code
    reason = str(exc)
    safe_reason = reason if reason in _SAFE_REASONS else None
    retryable = (status <= 0 or status in {408, 409, 425, 429} or status >= 500) and not (
        status == 409 and safe_reason in _STALE_REQUEST_REASONS
    )
    fields = {
        "event": "superseded_container_refused",
        "operation_id": operation_id,
        "request_id": container.get("request_id"),
        "host": host["host"],
        "sandbox_id": container["sandbox_id"],
        "container_id": container.get("container_id"),
        "generation": container["generation"],
        "status": status,
        "detail": safe_reason,
        "retryable": retryable,
    }
    # Text is the process default and drops ``extra``. Keep this bounded,
    # safe record in the message too; JSON escapes any identifier newlines.
    logger.warning(
        "superseded_container_refused %s", json.dumps(fields, sort_keys=True), extra=fields
    )
    said = f"HTTP {status}" if status > 0 else "no answer"
    if safe_reason is not None:
        said = f"{said}: {safe_reason}"
    return ProviderError(
        "superseded_container_not_removed",
        (
            "A container this Host's groups left behind could not be removed yet"
            if retryable
            else "The Host refused to remove a container its groups left behind"
        )
        + f" ({container['sandbox_id']}, {said})",
        retryable=retryable,
    )
