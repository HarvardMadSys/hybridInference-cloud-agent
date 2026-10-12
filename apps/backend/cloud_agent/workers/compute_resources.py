"""Control-plane compute workers; browser lifetime never controls renewal."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cloud_agent.compute_config import current_poll_seconds
from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.application.compute_autoscale import CloudLabAutoscaler
    from cloud_agent.application.compute_reconcile import ComputeReconciler

logger = get_logger(__name__)


class ComputeResourceWorker:
    """Bounded workers with durable per-allocation exclusion in Postgres."""

    def __init__(
        self,
        reconciler: ComputeReconciler,
        *,
        autoscaler: CloudLabAutoscaler | None = None,
        concurrency: int = 4,
    ) -> None:
        self.reconciler = reconciler
        self.repository = reconciler.repository
        self.autoscaler = autoscaler
        self.concurrency = concurrency
        self.identity = str(uuid4())

    async def run(self) -> None:
        """Run scheduling independently of long SSH deployment operations."""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._schedule())
            for slot in range(self.concurrency):
                group.create_task(self._consume(f"{self.identity}:{slot}"))

    async def _schedule(self) -> None:
        while True:
            try:
                await self.reconciler.schedule()
            except Exception as exc:
                logger.error("compute_scheduler_failed", extra={"error_type": type(exc).__name__})
            if self.autoscaler is not None:
                try:
                    await self.autoscaler.reconcile()
                except Exception as exc:
                    logger.error(
                        "compute_autoscaler_failed",
                        extra={"error_type": type(exc).__name__},
                    )
            await asyncio.sleep(current_poll_seconds(self.reconciler.commands.configuration))

    async def _consume(self, worker_id: str) -> None:
        while True:
            try:
                operation = await self.repository.claim_operation(worker_id=worker_id)
                if operation is not None:
                    await self.execute_claimed(operation)
                    continue
            except Exception as exc:
                logger.error("compute_worker_failed", extra={"error_type": type(exc).__name__})
            await asyncio.sleep(5)

    async def execute_claimed(self, operation: dict[str, Any]) -> None:
        """Cancel local work after lease loss; checkpoints make recovery explicit."""
        execution = asyncio.create_task(self.reconciler.execute(operation))
        heartbeat = asyncio.create_task(self._heartbeat(operation, execution))
        try:
            await execution
        except asyncio.CancelledError:
            # Cancellation from shutdown must propagate. A lost database lease
            # only ends this operation; the consumer may take unrelated work.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception as exc:
            logger.error(
                "compute_operation_failed",
                extra={"operation_id": operation["id"], "error_type": type(exc).__name__},
            )
            await self.repository.finish_operation(
                operation_id=operation["id"],
                generation=operation["generation"],
                worker_id=operation["worker_id"],
                state="unknown",
                step=operation["step"],
                message="An internal error interrupted the operation. Its checkpoint is preserved for recovery.",
            )
        finally:
            execution.cancel()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, execution, return_exceptions=True)

    async def _heartbeat(self, operation: dict[str, Any], execution: asyncio.Task[None]) -> None:
        try:
            while not execution.done():
                await asyncio.sleep(20)
                valid = await self.repository.renew_operation_lease(
                    operation_id=operation["id"],
                    generation=operation["generation"],
                    worker_id=operation["worker_id"],
                )
                if not valid:
                    execution.cancel()
                    return
        except Exception:
            # Without a proven lease this process must stop starting side effects.
            execution.cancel()
