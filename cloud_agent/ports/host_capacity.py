"""Port: what each machine is actually doing -- CPU, memory, containers.

Separate from the pool above on purpose. A host can hold scheduling quota and
still be unusable, and the two have opposite remedies; the operator chart
reads this, the scheduler reads that.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.capacity_protocol import CAPACITY_HISTORY_RETENTION_S

if TYPE_CHECKING:
    from cloud_agent.capacity_protocol import CapacityHistoryPointWrite


class HostCapacityRepository(Protocol):
    """Port: what each machine is actually doing -- CPU, memory, containers."""

    async def record_capacity_history_point(
        self, *, host: str, capacity_epoch: Any, point: CapacityHistoryPointWrite
    ) -> bool:
        """Keep one observation for a host, epoch and minute.

        ``ON CONFLICT DO NOTHING`` against both the primary key and the sample
        uniqueness constraint, and that is the whole correctness argument: the
        first fresh observation selected for a minute is the one that is kept.
        Later ones in the same minute are not averaged, not summed and not
        allowed to replace it — three runner replicas report independently, and
        a chart built from whichever of them wrote last would move for reasons
        that have nothing to do with the machine.

        Args:
            host: The host the observation belongs to.
            capacity_epoch: The generation it was observed under. Passed
                through from the row that was read, never re-derived, so a
                delayed write cannot stamp an old sample with a new epoch.
            point: The scalar observation.

        Returns:
            ``True`` when this call inserted the minute's point.
        """
        ...

    async def list_capacity_history_candidates(
        self, *, after_host: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return one page of hosts whose stored observation is still fresh.

        Keyset pagination on the host name, not ``OFFSET``: the history task
        walks the pool every minute, and an offset it restarts from would
        re-read the same prefix forever while the hosts behind it are never
        sampled.

        The snapshot's scalars are projected as text rather than cast in SQL.
        A cast would make one corrupt document fail the whole batch, and a
        batch that fails at the same row never gets past it; parsing happens
        where a bad value costs a single point.

        Args:
            after_host: Last host of the previous page, or ``None`` to start.
            limit: Page size ceiling.

        Returns:
            Rows ordered by host, each carrying its epoch, sample identity and
            projected scalars.
        """
        ...

    async def prune_capacity_history(
        self, *, older_than_seconds: float = CAPACITY_HISTORY_RETENTION_S, limit: int = 2000
    ) -> int:
        """Delete one bounded batch of history older than the retention window.

        Bounded because this runs beside the reaper and the publisher on the
        same database: an unbounded ``DELETE`` after a control-plane outage
        would hold locks for as long as it took to remove the backlog, and the
        work it delayed is recovering jobs.

        Args:
            older_than_seconds: Retention window.
            limit: Rows this call may remove.

        Returns:
            How many rows were removed.
        """
        ...

    async def host_capacity_history(
        self, *, host: str, range_seconds: int
    ) -> dict[str, Any] | None:
        """Read one host's stored capacity history for the current epoch.

        The epoch and the points come from a single read-only repeatable-read
        view, which is what stops a re-enrollment landing between them from
        returning a window labelled with one generation and filled from
        another.

        Args:
            host: Enrollment name.
            range_seconds: Window length ending at the read's own minute.

        Returns:
            ``{"capacity_epoch", "since", "until", "observed_at", "rows"}``,
            or ``None`` when no such host exists. A host that has never
            reported capacity answers with an empty point list rather than
            ``None``: it exists, and "not measured" is a different answer from
            "not here".
        """
        ...
