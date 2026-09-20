"""Port: the opt-in platform operation measurements an experiment produced.

Declared here, beside the code that calls it, so a writer depends on the
operations it needs rather than on the whole persistence surface.

The table is append-only in practice. A measurement is one root request's
frozen record, and its ``measurement_id`` is what the producing process minted,
so a retry is the same row rather than a second sample: the port's job is to
make that idempotent and to refuse a same-id write whose contents differ,
because last-write-wins would silently replace a sample with a retry that
disagrees with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

#: The four answers a write can have. A refusal is a result rather than an
#: exception because it is not a failure of the write: the record was rejected
#: on purpose, and the caller has to decide what to do about it. Returning it
#: makes that decision explicit instead of something a bare ``try`` can skip.
#:
#: ``"conflict"`` is the measurement id already naming different contents;
#: ``"run_conflict"`` is the experiment id already naming a different
#: experiment. Neither is retryable: sending the same record again cannot
#: resolve either, and looping would turn a disagreement into an endless retry.
MeasurementWriteResult = Literal["stored", "duplicate", "conflict", "run_conflict"]

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime


class OperationMeasurements(Protocol):
    """Port: store, catalogue and read back the opt-in operation measurements.

    One capability rather than two: the same table is written once and read
    once, and splitting the protocol would have put one table behind two
    declarations free to drift apart. A caller still depends on the object it
    is handed, so the Admin routes take this port and use the read half while
    the writers use the write half.
    """

    async def record_operation_measurement(
        self, *, measurement: Mapping[str, Any], run: Mapping[str, Any] | None = None
    ) -> MeasurementWriteResult:
        """Store one frozen measurement, idempotently.

        Args:
            measurement: The record to store. ``measurement_id`` and
                ``payload_hash`` are the identity: a repeat of both is the same
                sample and is accepted without writing a second row, while the
                same id with a different hash is a conflict the caller must
                surface rather than overwrite.
            run: The experiment catalogue entry, when this is the first
                measurement the writer has seen from that run.

        Returns:
            ``"stored"`` when this call inserted the row, ``"duplicate"`` when
            an identical measurement was already there, ``"conflict"`` when the
            measurement id exists with different contents, and
            ``"run_conflict"`` when the experiment id already names a different
            experiment -- different operations, a different target, or another
            configuration version. The last two are refusals to be surfaced,
            never overwritten and never retried; a refused measurement is not
            stored.
        """
        ...

    async def list_profiling_runs(
        self, *, since: datetime | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the experiment catalogue, most recent first."""
        ...

    async def get_profiling_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one experiment, or ``None`` when it was never recorded."""
        ...

    async def list_operation_measurements(
        self,
        *,
        run_id: str | None = None,
        operations: Sequence[str] | None = None,
        request_kinds: Sequence[str] | None = None,
        coverage_scopes: Sequence[str] | None = None,
        outcome: str | None = None,
        collection_complete: bool | None = None,
        job_id: str | None = None,
        thread_id: str | None = None,
        attempt_id: int | None = None,
        interaction_id: str | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one keyset page of measurements, newest first.

        Keyset on ``(received_at, measurement_id)`` rather than ``OFFSET``: a
        new sample arriving mid-page would shift every later row and make an
        offset cursor skip one and repeat another.

        ``attempt_id`` and ``interaction_id`` are the two precise keys the
        Admin surface joins on. They are not interchangeable: an attempt names
        one execution and only startup records carry one, while an interaction
        correlates the list, create and stream-attach requests of one terminal
        open and is the only way to read those three together.
        """
        ...

    async def get_operation_measurement(self, measurement_id: str) -> dict[str, Any] | None:
        """Return one stored measurement with its frozen record, or ``None``."""
        ...

    async def summarize_operation_measurements(
        self, *, run_id: str, group_by: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return counts and percentiles over a whole experiment, not one page.

        Computed in SQL because the page a reader is looking at is not the
        population a percentile is about.
        """
        ...


__all__ = [
    "MeasurementWriteResult",
    "OperationMeasurements",
]
