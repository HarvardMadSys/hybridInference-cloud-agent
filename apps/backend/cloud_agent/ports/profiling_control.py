"""Port: the Admin-owned lifecycle of an operation-measurement experiment.

One capability, because the three things it owns are read and written together
and splitting them would let them drift:

* **The control row** -- a per-deployment singleton naming the run that is in
  force, a monotonically increasing ``revision``, and the tombstone of the last
  stop. This is the only thing a Start or a Stop changes, and ``revision`` is
  the order those changes happened in.
* **The command log** -- one row per accepted Start or Stop, keyed by the
  administrator's request id. A response that was lost is retried with the same
  id and the same action, and the answer is the run that was already created
  rather than a second experiment.
* **Participant state** -- which control-plane process, runner or broker has
  applied which revision, bounded by a TTL so a decommissioned replica stops
  being reported as missing.

Three orders that must never be confused, and each is a different column or a
different object:

* ``config_version`` on the run manifest says *which immutable experiment* this
  is. It never changes for a given run.
* ``revision`` says *which control command was applied last*. It advances on
  every Start and every Stop, including a Stop of an experiment that had
  already expired.
* the renewal sequence lives only in memory, in the collector, and says *which
  authorization refresh a process has seen*. It is deliberately not stored: a
  renewal is not a command and must never advance the revision, or a lease that
  quietly lapsed would look like an administrator's decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.domain.profiling_control import ControlCommandResult

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


class ProfilingControl(Protocol):
    """Port: the persisted Admin control state for operation measurement."""

    async def get_control_state(self, *, deployment: str) -> dict[str, Any] | None:
        """Return the deployment's control row, or ``None`` when none exists.

        ``None`` is not "off": it means this deployment has never been
        commanded, which is what the Admin surface reports as "not configured"
        rather than inventing an off state for a row that was never written.
        """
        ...

    async def start_run(
        self,
        *,
        deployment: str,
        request_id: str,
        fingerprint: str,
        expected_revision: int,
        run: Mapping[str, Any],
        actor_id: str | None,
        now: datetime,
    ) -> ControlCommandResult:
        """Create the experiment, move the control row and record the command."""
        ...

    async def stop_run(
        self,
        *,
        deployment: str,
        request_id: str,
        fingerprint: str,
        run_id: str,
        actor_id: str | None,
        now: datetime,
        reason: str,
    ) -> ControlCommandResult:
        """Stop the named run, or explain why it is not the run to stop."""
        ...

    async def expire_run(
        self, *, deployment: str, run_id: str, now: datetime
    ) -> ControlCommandResult:
        """Land the terminal state of a run whose deadline has passed."""
        ...

    async def record_participant(
        self,
        *,
        deployment: str,
        participant_id: str,
        kind: str,
        host: str | None,
        run_id: str | None,
        applied_revision: int,
        capability: str | None,
        state: str,
        detail: str,
        budget: Mapping[str, Any] | None,
        ttl_seconds: int,
        now: datetime,
    ) -> None:
        """Record one process's applied revision and what it reported."""
        ...

    async def list_participants(
        self, *, deployment: str, now: datetime, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the participants that have not expired, newest first."""
        ...

    async def prune_participants(self, *, deployment: str, now: datetime) -> int:
        """Delete expired participant rows and return how many went."""
        ...

    async def count_stored_measurements(self, *, run_id: str) -> int:
        """Return how many frozen records this experiment actually stored."""
        ...

    async def mark_run_lifecycle(
        self,
        *,
        run_id: str,
        lifecycle: str,
        now: datetime,
        deployment: str,
        source: str,
        created_by: str | None,
        control_revision: int,
        stop_reason: str = "",
    ) -> None:
        """Attach lifecycle and provenance to a run the control plane created."""
        ...

    async def get_run(self, *, run_id: str) -> dict[str, Any] | None:
        """Return one experiment manifest with its lifecycle, or ``None``."""
        ...

    async def list_runs(self, *, deployment: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the deployment's experiments, most recently started first."""
        ...


__all__ = [
    "ControlCommandResult",
    "ProfilingControl",
]
