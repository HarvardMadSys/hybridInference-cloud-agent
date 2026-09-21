"""Postgres implementation of the Admin profiling-control port.

The whole feature turns on this file being right about three things, so they are
stated once here rather than assumed at each caller.

**The control row is locked, not searched.** The migration seeds one row per
deployment, and every mutation takes ``SELECT ... FOR UPDATE`` on it. A row that
has to be created on demand is a row two writers can both decide to create, and
a lock on a row that does not exist protects nothing -- the mistake the design
calls out by name.

**The request is checked before the revision.** A Start whose response was lost
is retried with the same request id and the same body, and it must return the
experiment that was already created rather than refusing on a revision that the
first attempt itself advanced. So the command log is read first, and only a
request id that has never been seen reaches the revision check.

**Stop writes a tombstone, never an erasure.** ``revision`` advances, the
deployment's desired state becomes ``off`` and the run it pointed at is recorded
as stopped with the instant and the reason. The run's configured ``expires_at``
is not touched: the delivery path compares an arriving manifest against it, so
rewriting it would refuse every record that was already in flight -- the exact
``run_conflict`` the design warns about.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.profiling_control import (
    CONTROL_OUTCOME_APPLIED,
    CONTROL_OUTCOME_REFUSED,
    CONTROL_OUTCOME_REPLAYED,
    CONTROL_REFUSED_ALREADY_ACTIVE,
    CONTROL_REFUSED_ALREADY_FINISHED,
    CONTROL_REFUSED_CONFLICTING_REQUEST,
    CONTROL_REFUSED_STALE_REVISION,
    CONTROL_REFUSED_STOPPING,
    CONTROL_REFUSED_UNKNOWN_RUN,
    ControlCommandResult,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    import asyncpg

#: Lifecycle values, in the order an experiment moves through them.
LIFECYCLE_PENDING = "pending"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_STOPPED = "stopped"
LIFECYCLE_EXPIRED = "expired"
LIFECYCLE_FAILED = "failed"

#: Lifecycles that are over: nothing more will be collected for the run, and a
#: new experiment may start.
TERMINAL_LIFECYCLES = frozenset({LIFECYCLE_STOPPED, LIFECYCLE_EXPIRED, LIFECYCLE_FAILED})

#: Participant kinds. The control plane's own root producers are one kind
#: because they share a process and therefore a policy snapshot.
PARTICIPANT_CONTROL_PLANE = "control_plane"
PARTICIPANT_RUNNER = "runner"
PARTICIPANT_BROKER = "broker"

#: What a participant reported about the revision it holds.
PARTICIPANT_APPLIED = "applied"
PARTICIPANT_UNSUPPORTED = "unsupported"
PARTICIPANT_UNCONFIRMED = "unconfirmed"
PARTICIPANT_EXHAUSTED = "exhausted"


class PostgresProfilingControlRepository:
    """The deployment control row, its command log and its participant state."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    # -- control row -------------------------------------------------------

    async def get_control_state(self, *, deployment: str) -> dict[str, Any] | None:
        """Return the deployment's control row, or ``None`` when none exists.

        ``None`` is not "off": it means this deployment has never been
        commanded, which is what the Admin surface reports as "not configured"
        rather than inventing an off state for a row that was never written.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT deployment, revision, desired_state, current_run_id,
                       updated_at, updated_by, last_stop_revision,
                       last_stopped_at, last_stopped_run_id
                FROM agent_operation_profiling_control
                WHERE deployment = $1
                """,
                deployment,
            )
            if row is None and deployment:
                row = await conn.fetchrow(
                    """
                    SELECT deployment, revision, desired_state, current_run_id,
                           updated_at, updated_by, last_stop_revision,
                           last_stopped_at, last_stopped_run_id
                    FROM agent_operation_profiling_control
                    WHERE deployment = $1
                    """,
                    _FALLBACK_DEPLOYMENT,
                )
        return _control_row(row) if row is not None else None

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
        async with self._pool.acquire() as conn, conn.transaction():
            # Advisory lock on the deployment, taken before the row lock and
            # released with the transaction. The row lock alone would still let
            # two Start transactions interleave their idempotency lookups before
            # either took it, and the loser would surface a unique violation
            # instead of the refusal this port promises.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 1))",
                f"profiling-control:{deployment}",
            )
            replay = await _replay_command(conn, deployment=deployment, request_id=request_id)
            if replay is not None:
                if replay["fingerprint"] != fingerprint or replay["action"] != "start":
                    return _refuse(
                        CONTROL_REFUSED_CONFLICTING_REQUEST,
                        revision=await _revision(conn, deployment),
                        detail="this request id was used for a different command",
                    )
                return _result_from_command(replay, CONTROL_OUTCOME_REPLAYED)

            control = await _lock_control(conn, deployment=deployment)
            if control is None:
                # No control row: this deployment never opted in. Refusing is
                # the honest answer -- writing a row here would create the
                # deployment's control state from a request.
                return ControlCommandResult(
                    kind=CONTROL_OUTCOME_REFUSED,
                    revision=0,
                    reason=CONTROL_REFUSED_UNKNOWN_RUN,
                    detail="this deployment has no profiling control row",
                )
            if control["desired_state"] == "on" and control["current_run_id"]:
                return _refuse(
                    CONTROL_REFUSED_ALREADY_ACTIVE,
                    revision=int(control["revision"]),
                    run_id=str(control["current_run_id"]),
                    detail="another experiment is active; stop it first",
                )
            if int(control["revision"]) != int(expected_revision):
                # Checked *after* the idempotency lookup, so a retry of a
                # command that already applied is replayed rather than refused
                # by the revision it advanced itself.
                return _refuse(
                    CONTROL_REFUSED_STALE_REVISION,
                    revision=int(control["revision"]),
                    detail=(
                        f"expected revision {expected_revision}, "
                        f"deployment is on {control['revision']}"
                    ),
                )
            stopping = await _stop_propagation_pending(conn, deployment=deployment, now=now)
            if stopping is not None:
                # Starting while the previous experiment's authorizations may
                # still be live would let two experiments collect at once, and
                # the resulting samples would be filed under whichever run the
                # producer happened to hold. Waiting is the only safe answer.
                return _refuse(
                    CONTROL_REFUSED_STOPPING,
                    revision=int(control["revision"]),
                    run_id=stopping,
                    detail="the previous experiment is still propagating its stop",
                )

            revision = int(control["revision"]) + 1
            await _insert_run(conn, run=run, deployment=deployment, actor_id=actor_id, now=now)
            await conn.execute(
                """
                UPDATE agent_operation_profiling_runs
                SET lifecycle = $2,
                    source = 'admin',
                    created_by = $3,
                    control_revision = $4,
                    started_at = $5,
                    deployment = $1,
                    received_at = $5
                WHERE run_id = $6
                """,
                deployment,
                LIFECYCLE_PENDING,
                actor_id,
                revision,
                now,
                str(run["run_id"]),
            )
            # Upsert rather than update: the row this deployment locked may be
            # the shared fallback, and adopting it would name the experiment in
            # a namespace the deployment does not own.
            await conn.execute(
                """
                INSERT INTO agent_operation_profiling_control (
                    deployment, revision, desired_state, current_run_id,
                    updated_at, updated_by
                )
                VALUES ($1, $2, 'on', $3, $4, $5)
                ON CONFLICT (deployment) DO UPDATE
                SET revision = EXCLUDED.revision,
                    desired_state = EXCLUDED.desired_state,
                    current_run_id = EXCLUDED.current_run_id,
                    updated_at = EXCLUDED.updated_at,
                    updated_by = EXCLUDED.updated_by
                """,
                deployment,
                revision,
                str(run["run_id"]),
                now,
                actor_id,
            )
            await _record_command(
                conn,
                deployment=deployment,
                request_id=request_id,
                action="start",
                fingerprint=fingerprint,
                revision=revision,
                run_id=str(run["run_id"]),
                actor_id=actor_id,
            )
            state = await _read_control(conn, deployment=deployment)
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_APPLIED,
                revision=revision,
                run_id=str(run["run_id"]),
                expires_at=run["expires_at"],
                state=state,
            )

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
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 1))",
                f"profiling-control:{deployment}",
            )
            replay = await _replay_command(conn, deployment=deployment, request_id=request_id)
            if replay is not None:
                if replay["fingerprint"] != fingerprint or replay["action"] != "stop":
                    return _refuse(
                        CONTROL_REFUSED_CONFLICTING_REQUEST,
                        revision=await _revision(conn, deployment),
                        detail="this request id was used for a different command",
                    )
                return _result_from_command(replay, CONTROL_OUTCOME_REPLAYED)

            control = await _lock_control(conn, deployment=deployment)
            if control is None:
                return ControlCommandResult(
                    kind=CONTROL_OUTCOME_REFUSED,
                    revision=0,
                    reason=CONTROL_REFUSED_UNKNOWN_RUN,
                    detail="this deployment has no profiling control row",
                )
            if control["current_run_id"] is None:
                # Nothing is active. A Stop here is idempotent in the only way
                # that matters: it does not move the revision, so a double click
                # cannot make a later Start look stale.
                return ControlCommandResult(
                    kind=CONTROL_OUTCOME_REPLAYED,
                    revision=int(control["revision"]),
                    run_id=None,
                    state=_control_row(control),
                )
            if str(control["current_run_id"]) != run_id:
                # A page left open on the previous experiment must not be able
                # to stop the one that replaced it.
                return _refuse(
                    CONTROL_REFUSED_ALREADY_FINISHED,
                    revision=int(control["revision"]),
                    run_id=str(control["current_run_id"]),
                    detail="that experiment is not the one in force",
                )

            revision = int(control["revision"]) + 1
            await conn.execute(
                """
                UPDATE agent_operation_profiling_runs
                SET lifecycle = $2, stopped_at = $3, stop_reason = $4
                WHERE run_id = $1
                """,
                run_id,
                LIFECYCLE_STOPPED,
                now,
                reason,
            )
            await conn.execute(
                """
                UPDATE agent_operation_profiling_control
                SET revision = $2,
                    desired_state = 'off',
                    current_run_id = NULL,
                    updated_at = $3,
                    updated_by = $4,
                    last_stop_revision = $2,
                    last_stopped_at = $3,
                    last_stopped_run_id = $5
                WHERE deployment = $1
                """,
                deployment,
                revision,
                now,
                actor_id,
                run_id,
            )
            await _record_command(
                conn,
                deployment=deployment,
                request_id=request_id,
                action="stop",
                fingerprint=fingerprint,
                revision=revision,
                run_id=run_id,
                actor_id=actor_id,
            )
            state = await _read_control(conn, deployment=deployment)
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_APPLIED,
                revision=revision,
                run_id=run_id,
                reason=reason,
                state=state,
            )

    async def expire_run(
        self, *, deployment: str, run_id: str, now: datetime
    ) -> ControlCommandResult:
        """Land the terminal state of a run whose deadline has passed."""
        async with self._pool.acquire() as conn, conn.transaction():
            control = await _lock_control(conn, deployment=deployment)
            if control is None:
                return ControlCommandResult(
                    kind=CONTROL_OUTCOME_REFUSED,
                    revision=0,
                    reason=CONTROL_REFUSED_UNKNOWN_RUN,
                    detail="this deployment has no profiling control row",
                )
            row = await conn.fetchrow(
                """
                SELECT run_id, lifecycle, expires_at
                FROM agent_operation_profiling_runs
                WHERE run_id = $1
                """,
                run_id,
            )
            if row is None:
                return ControlCommandResult(
                    kind=CONTROL_OUTCOME_REFUSED,
                    revision=int(control["revision"]),
                    reason=CONTROL_REFUSED_UNKNOWN_RUN,
                    detail="no such experiment",
                )
            if str(row["lifecycle"] or "") in TERMINAL_LIFECYCLES:
                # Already terminal: recorded, and recording it again is not a
                # command and must not advance the revision.
                return ControlCommandResult(
                    kind=CONTROL_OUTCOME_REPLAYED,
                    revision=int(control["revision"]),
                    run_id=run_id,
                )
            revision = int(control["revision"]) + 1
            await conn.execute(
                """
                UPDATE agent_operation_profiling_runs
                SET lifecycle = $2, stopped_at = COALESCE(stopped_at, $3),
                    stop_reason = COALESCE(stop_reason, 'expired')
                WHERE run_id = $1
                """,
                run_id,
                LIFECYCLE_EXPIRED,
                now,
            )
            if str(control["current_run_id"] or "") == run_id:
                await conn.execute(
                    """
                    UPDATE agent_operation_profiling_control
                    SET revision = $2,
                        desired_state = 'off',
                        current_run_id = NULL,
                        updated_at = $3,
                        last_stop_revision = $2,
                        last_stopped_at = $3,
                        last_stopped_run_id = $4
                    WHERE deployment = $1
                    """,
                    deployment,
                    revision,
                    now,
                    run_id,
                )
            return ControlCommandResult(
                kind=CONTROL_OUTCOME_APPLIED,
                revision=revision,
                run_id=run_id,
                reason="expired",
            )

    # -- participants ------------------------------------------------------

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
        expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_operation_profiling_participants (
                    deployment, participant_id, kind, host, run_id,
                    applied_revision, capability, state, detail, budget,
                    confirmed_at, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12)
                ON CONFLICT (deployment, participant_id) DO UPDATE
                SET kind = EXCLUDED.kind,
                    host = EXCLUDED.host,
                    run_id = EXCLUDED.run_id,
                    applied_revision = EXCLUDED.applied_revision,
                    capability = EXCLUDED.capability,
                    state = EXCLUDED.state,
                    detail = EXCLUDED.detail,
                    budget = EXCLUDED.budget,
                    confirmed_at = EXCLUDED.confirmed_at,
                    expires_at = EXCLUDED.expires_at
                """,
                deployment,
                participant_id,
                kind,
                host,
                run_id,
                int(applied_revision),
                capability,
                state,
                detail,
                json.dumps(dict(budget or {})),
                now,
                expires_at,
            )

    async def list_participants(
        self, *, deployment: str, now: datetime, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the participants that have not expired, newest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT deployment, participant_id, kind, host, run_id,
                       applied_revision, capability, state, detail, budget,
                       confirmed_at, expires_at
                FROM agent_operation_profiling_participants
                WHERE deployment = $1
                  AND expires_at > $2
                  AND ($3::text IS NULL OR run_id = $3)
                ORDER BY confirmed_at DESC, participant_id DESC
                """,
                deployment,
                now,
                run_id,
            )
        return [_participant_row(row) for row in rows]

    async def prune_participants(self, *, deployment: str, now: datetime) -> int:
        """Delete expired participant rows and return how many went."""
        async with self._pool.acquire() as conn:
            deleted = await conn.execute(
                """
                DELETE FROM agent_operation_profiling_participants
                WHERE deployment = $1 AND expires_at <= $2
                """,
                deployment,
                now,
            )
        # asyncpg answers a DELETE with its command tag, e.g. "DELETE 3".
        try:
            return int(str(deleted).rsplit(" ", 1)[-1])
        except (TypeError, ValueError):  # pragma: no cover - driver tag shape
            return 0

    # -- runs --------------------------------------------------------------

    async def count_stored_measurements(self, *, run_id: str) -> int:
        """Return how many frozen records this experiment actually stored."""
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM agent_operation_measurements
                    WHERE profiling_run_id = $1
                    """,
                    run_id,
                )
                or 0
            )

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
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_operation_profiling_runs
                SET lifecycle = $2,
                    deployment = COALESCE(deployment, $3),
                    source = COALESCE(source, $4),
                    created_by = COALESCE(created_by, $5),
                    control_revision = COALESCE(control_revision, $6),
                    stop_reason = COALESCE(NULLIF($7, ''), stop_reason)
                WHERE run_id = $1
                """,
                run_id,
                lifecycle,
                deployment,
                source,
                created_by,
                int(control_revision),
                stop_reason,
            )

    async def get_run(self, *, run_id: str) -> dict[str, Any] | None:
        """Return one experiment manifest with its lifecycle, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT run_id, operations, target, expires_at, config_version,
                       admission_instances, created_at, received_at,
                       source, lifecycle, created_by, control_revision,
                       started_at, stopped_at, stop_reason, deployment
                FROM agent_operation_profiling_runs
                WHERE run_id = $1
                """,
                run_id,
            )
        return _run_detail_row(row) if row is not None else None

    async def list_runs(self, *, deployment: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the deployment's experiments, most recently started first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT run_id, operations, target, expires_at, config_version,
                       admission_instances, created_at, received_at,
                       source, lifecycle, created_by, control_revision,
                       started_at, stopped_at, stop_reason, deployment
                FROM agent_operation_profiling_runs
                WHERE deployment = $1
                ORDER BY COALESCE(started_at, created_at) DESC, run_id DESC
                LIMIT $2
                """,
                deployment,
                limit,
            )
        return [_run_detail_row(row) for row in rows]


def _control_row(row: Any) -> dict[str, Any]:
    """Return one control row as plain data."""
    return {
        "deployment": row["deployment"],
        "revision": int(row["revision"]),
        "desired_state": row["desired_state"],
        "current_run_id": row["current_run_id"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "last_stop_revision": (
            None if row["last_stop_revision"] is None else int(row["last_stop_revision"])
        ),
        "last_stopped_at": row["last_stopped_at"],
        "last_stopped_run_id": row["last_stopped_run_id"],
    }


def _run_detail_row(row: Any) -> dict[str, Any]:
    """Return one experiment manifest with its lifecycle as plain data."""
    return {
        "run_id": row["run_id"],
        "operations": list(row["operations"]),
        "target": json.loads(row["target"]),
        "expires_at": row["expires_at"],
        "config_version": int(row["config_version"]),
        "admission_instances": json.loads(row["admission_instances"]),
        "created_at": row["created_at"],
        "received_at": row["received_at"],
        "source": row["source"],
        "lifecycle": row["lifecycle"],
        "created_by": row["created_by"],
        "control_revision": (
            None if row["control_revision"] is None else int(row["control_revision"])
        ),
        "started_at": row["started_at"],
        "stopped_at": row["stopped_at"],
        "stop_reason": row["stop_reason"],
        "deployment": row["deployment"],
    }


def _participant_row(row: Any) -> dict[str, Any]:
    """Return one participant row as plain data."""
    return {
        "participant_id": row["participant_id"],
        "kind": row["kind"],
        "host": row["host"],
        "run_id": row["run_id"],
        "applied_revision": int(row["applied_revision"]),
        "capability": row["capability"],
        "state": row["state"],
        "detail": row["detail"],
        "budget": json.loads(row["budget"]),
        "confirmed_at": row["confirmed_at"],
        "expires_at": row["expires_at"],
    }


#: The row that answers for a deployment which has never been commanded under
#: its own name. The migration seeds it, so naming a deployment does not disable
#: Admin control until somebody inserts a row by hand.
_FALLBACK_DEPLOYMENT = ""


async def _lock_control(conn: Any, *, deployment: str) -> Any:
    """Lock and return the deployment's control row, or ``None`` when absent.

    The deployment's own row wins; the fallback row answers when it does not
    exist yet. A Start under a new name therefore begins from the fallback's
    revision 0 and writes the deployment's own row from then on -- so the first
    Start is the thing that creates a namespace, rather than an operator having
    to create one before the button works.
    """
    row = await conn.fetchrow(
        """
        SELECT deployment, revision, desired_state, current_run_id,
               updated_at, updated_by, last_stop_revision,
               last_stopped_at, last_stopped_run_id
        FROM agent_operation_profiling_control
        WHERE deployment = $1
        FOR UPDATE
        """,
        deployment,
    )
    if row is not None or not deployment:
        return row
    return await conn.fetchrow(
        """
        SELECT deployment, revision, desired_state, current_run_id,
               updated_at, updated_by, last_stop_revision,
               last_stopped_at, last_stopped_run_id
        FROM agent_operation_profiling_control
        WHERE deployment = $1
        FOR UPDATE
        """,
        _FALLBACK_DEPLOYMENT,
    )


async def _revision(conn: Any, deployment: str) -> int:
    """Return the deployment's current revision, or 0 when it has no row."""
    value = await conn.fetchval(
        "SELECT revision FROM agent_operation_profiling_control WHERE deployment = $1",
        deployment,
    )
    return int(value or 0)


async def _read_control(conn: Any, *, deployment: str) -> dict[str, Any]:
    """Return the control row as plain data, read inside the transaction."""
    row = await conn.fetchrow(
        """
        SELECT deployment, revision, desired_state, current_run_id,
               updated_at, updated_by, last_stop_revision,
               last_stopped_at, last_stopped_run_id
        FROM agent_operation_profiling_control
        WHERE deployment = $1
        """,
        deployment,
    )
    return _control_row(row) if row is not None else {}


async def _stop_propagation_pending(conn: Any, *, deployment: str, now: Any) -> str | None:
    """Return the run whose stop may still be propagating, if any.

    A Stop is not instantaneous across processes: a runner that has not yet
    synced still holds an authorization with up to its own lease left. Starting
    a new experiment inside that window would let two runs collect at once, so a
    Start waits for the window to close.

    The window is measured from the tombstone the Stop wrote, and it is bounded
    rather than waiting for acknowledgements: an offline host must not be able
    to block an administrator indefinitely, which is exactly the failure the
    design forbids.
    """
    row = await conn.fetchrow(
        """
        SELECT last_stopped_run_id, last_stopped_at, revision
        FROM agent_operation_profiling_control
        WHERE deployment = $1
        """,
        deployment,
    )
    if row is None or row["last_stopped_at"] is None or not row["last_stopped_run_id"]:
        return None
    elapsed = (now - row["last_stopped_at"]).total_seconds()
    if elapsed >= _STOP_PROPAGATION_SECONDS:
        return None
    # A participant that has already confirmed the stopping revision proves the
    # window is closed *for that process*; the guard is only about the ones that
    # have not answered, so it clears as soon as none are outstanding.
    outstanding = await conn.fetchval(
        """
        SELECT COUNT(*) FROM agent_operation_profiling_participants
        WHERE deployment = $1
          AND expires_at > $2
          AND run_id = $3
          AND applied_revision < $4
        """,
        deployment,
        now,
        str(row["last_stopped_run_id"]),
        int(row["revision"]),
    )
    if not outstanding:
        return None
    return str(row["last_stopped_run_id"])


#: How long a stopped experiment's outstanding authorizations are assumed to
#: live. It is the collector's authorization ceiling plus the client's own
#: worst-case sync, stated here because the guard is a database concern; the
#: design's contract is 20 seconds for every old authorization to lapse.
_STOP_PROPAGATION_SECONDS = 20.0


async def _insert_run(
    conn: Any, *, run: Mapping[str, Any], deployment: str, actor_id: str | None, now: Any
) -> None:
    """Write the immutable manifest, without touching an existing catalogue row.

    ``ON CONFLICT DO NOTHING`` because the run id is server-generated and a
    collision means a retry, not a second experiment; the caller's fingerprint
    check is what decides whether the retry is the same command.
    """
    await conn.execute(
        """
        INSERT INTO agent_operation_profiling_runs (
            run_id, operations, target, expires_at, config_version,
            admission_instances, created_at, received_at, source,
            lifecycle, created_by, control_revision, started_at, deployment
        )
        VALUES ($1, $2::text[], $3::jsonb, $4, $5, $6::jsonb, $7, $7, 'admin',
                $8, $9, $10, $7, $11)
        ON CONFLICT (run_id) DO NOTHING
        """,
        str(run["run_id"]),
        list(run["operations"]),
        json.dumps(dict(run.get("target") or {})),
        run["expires_at"],
        int(run.get("config_version") or 1),
        json.dumps(list(run.get("admission_instances") or [])),
        now,
        LIFECYCLE_PENDING,
        actor_id,
        int(run.get("control_revision") or 0),
        deployment,
    )


async def _record_command(
    conn: Any,
    *,
    deployment: str,
    request_id: str,
    action: str,
    fingerprint: str,
    revision: int,
    run_id: str | None,
    actor_id: str | None,
) -> None:
    """Record an accepted command so a retry is answered instead of re-applied."""
    await conn.execute(
        """
        INSERT INTO agent_operation_profiling_commands (
            deployment, request_id, action, fingerprint, revision, run_id,
            status, result, actor_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, 'applied', $7::jsonb, $8)
        ON CONFLICT (deployment, request_id) DO NOTHING
        """,
        deployment,
        request_id,
        action,
        fingerprint,
        int(revision),
        run_id,
        json.dumps({"revision": int(revision), "run_id": run_id}),
        actor_id,
    )


async def _replay_command(conn: Any, *, deployment: str, request_id: str) -> Any:
    """Return the recorded command for this request id, if there is one."""
    return await conn.fetchrow(
        """
        SELECT action, fingerprint, revision, run_id, status, result
        FROM agent_operation_profiling_commands
        WHERE deployment = $1 AND request_id = $2
        """,
        deployment,
        request_id,
    )


def _result_from_command(row: Any, kind: str) -> ControlCommandResult:
    """Return the original answer to a command that was already applied."""
    result = json.loads(row["result"])
    return ControlCommandResult(
        kind=kind,
        revision=int(row["revision"]),
        run_id=row["run_id"],
        reason=str(result.get("reason") or ""),
        detail=str(result.get("detail") or ""),
    )


def _refuse(
    reason: str,
    *,
    revision: int,
    detail: str,
    run_id: str | None = None,
) -> ControlCommandResult:
    """Return a refusal that still states the revision the deployment is on.

    A refusal is not an exception: the most common one is "somebody else moved
    this deployment", and the caller needs the number it must re-read before
    trying again, which an exception would not carry.
    """
    return ControlCommandResult(
        kind=CONTROL_OUTCOME_REFUSED,
        revision=int(revision),
        run_id=run_id,
        reason=reason,
        detail=detail,
    )


__all__ = [
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_EXPIRED",
    "LIFECYCLE_FAILED",
    "LIFECYCLE_PENDING",
    "LIFECYCLE_STOPPED",
    "PARTICIPANT_APPLIED",
    "PARTICIPANT_BROKER",
    "PARTICIPANT_CONTROL_PLANE",
    "PARTICIPANT_EXHAUSTED",
    "PARTICIPANT_RUNNER",
    "PARTICIPANT_UNCONFIRMED",
    "PARTICIPANT_UNSUPPORTED",
    "TERMINAL_LIFECYCLES",
    "PostgresProfilingControlRepository",
]
