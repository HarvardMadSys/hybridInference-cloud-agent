"""The steps every compute workflow takes: fenced writes, waits and alerts.

The reconciler and the rotation it delegates to share one
:class:`OperationSteps`. Each write carries the operation's fence (its id,
claim generation and worker), so a worker that lost its lease changes
nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.operational_health import deliver_event

if TYPE_CHECKING:
    from cloud_agent.ports.compute_resources import ComputeResourceRepository

#: Alerts are logged under the rotation's name, where they were first raised.
logger = get_logger("cloud_agent.application.compute_rotation")


def now() -> datetime:
    """The current time, in UTC."""
    return datetime.now(UTC)


def moment(value: Any) -> datetime | None:
    """A stored timestamp as a datetime: as read from a row, or in ISO form."""
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class OperationSteps:
    """Checkpoint, wait, finish and alert one compute operation under its lease."""

    def __init__(self, repository: ComputeResourceRepository) -> None:
        self.repository = repository

    @staticmethod
    def fence(operation: dict[str, Any]) -> dict[str, Any]:
        """The arguments that tie a write to this claim of the operation."""
        return {
            "operation_id": operation["id"],
            "generation": operation["generation"],
            "worker_id": operation["worker_id"],
        }

    async def allocation(self, allocation_id: str) -> dict[str, Any]:
        """Read the allocation again; raise when it no longer exists."""
        row = await self.repository.get_allocation(allocation_id)
        if row is None:
            raise ValueError("allocation_not_found")
        return row

    async def checkpoint(
        self, operation: dict[str, Any], step: str, result: dict[str, Any] | None = None
    ) -> None:
        """Record progress before a side effect; raise when the lease is lost.

        The in-memory operation follows: its step, and a new ``result`` merged
        from the old one and ``result``.
        """
        if not await self.repository.checkpoint_operation(
            **self.fence(operation), step=step, result=result
        ):
            raise ValueError("operation_lease_lost")
        operation["step"] = step
        operation["result"] = {**operation.get("result", {}), **(result or {})}

    async def finish(
        self,
        operation: dict[str, Any],
        state: str,
        step: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
        retry: float | None = None,
    ) -> None:
        """End this claim in ``state``; a lost lease makes it a no-op.

        ``retry`` is how many seconds pass before the operation can be claimed
        again; the repository's own default applies without it.
        """
        await self.repository.finish_operation(
            **self.fence(operation),
            state=state,
            step=step,
            message=message,
            result=result,
            retry_after_seconds=retry,
        )

    async def wait(
        self,
        operation: dict[str, Any],
        step: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
        retry: float = 30,
    ) -> None:
        """Hand the operation back to be claimed again in ``retry`` seconds."""
        await self.finish(operation, "waiting", step, message, result=result, retry=retry)

    @staticmethod
    async def notify(event: str, *, host: str, reason: str) -> None:
        """Log and deliver an alert; a delivery failure never stops the work."""
        logger.warning(event, extra={"host": host, "reason": reason})
        try:
            await deliver_event(event, {"host": host, "reason": reason})
        except Exception as exc:
            logger.warning(
                "compute_alert_delivery_failed",
                extra={"event": event, "error_type": type(exc).__name__},
            )

    async def alert(self, operation: dict[str, Any], event: str, *, host: str, reason: str) -> None:
        """Alert once per operation and event.

        The alert goes out before it is recorded, so a worker that stops in
        between sends it again rather than never.
        """
        sent = list((operation.get("result") or {}).get("alerts") or [])
        if event in sent:
            return
        await self.notify(event, host=host, reason=reason)
        await self.checkpoint(operation, operation["step"], {"alerts": [*sent, event]})
