"""Postgres implementation of the operation measurement ports.

One table holds the records and one holds the experiments they belong to. The
statements are the whole implementation; there is no cache and no second copy
of a measurement's meaning, because the summary columns exist to be *filtered*
and every question about what a record actually says is answered from the
frozen JSONB beside them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    import asyncpg

    from cloud_agent.ports.operation_measurements import MeasurementWriteResult


#: The columns a filter may name. A caller passes a field name, never a
#: fragment of SQL, so a filter cannot become an injection point.
_FILTERS: dict[str, str] = {
    "run_id": "profiling_run_id",
    "outcome": "outcome",
    "collection_complete": "collection_complete",
    "job_id": "job_id",
    "thread_id": "thread_id",
}


class PostgresOperationMeasurementRepository:
    """Opt-in platform operation measurements and their experiment catalogue."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    # -- write -------------------------------------------------------------

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
            an identical measurement was already there, and ``"conflict"``
            when the id exists with different contents -- which must be
            surfaced, never overwritten and never retried.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if run is not None:
                await conn.execute(
                    """
                    INSERT INTO agent_operation_profiling_runs (
                        run_id, operations, target, expires_at, config_version,
                        admission_instances
                    )
                    VALUES ($1, $2::text[], $3::jsonb, $4, $5, $6::jsonb)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    str(run["run_id"]),
                    list(run["operations"]),
                    json.dumps(dict(run.get("target") or {})),
                    run["expires_at"],
                    int(run.get("config_version") or 1),
                    json.dumps(list(run.get("admission_instances") or [])),
                )
            inserted = await conn.fetchval(
                """
                INSERT INTO agent_operation_measurements (
                    measurement_id, interaction_id, profiling_run_id, schema_version,
                    owner_id, job_id, thread_id, sandbox_id, host_id,
                    attempt_id, lease_generation,
                    operation, request_kind, boundary_version, coverage_scope,
                    producer, producer_instance_id,
                    duration_us, outcome, collection_complete, end_to_end_complete,
                    startup_path, observed_at, record, payload_hash
                )
                VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7, $8, $9,
                    $10, $11,
                    $12, $13, $14, $15,
                    $16, $17,
                    $18, $19, $20, $21,
                    $22, $23, $24::jsonb, $25
                )
                ON CONFLICT (measurement_id) DO NOTHING
                RETURNING measurement_id
                """,
                str(measurement["measurement_id"]),
                str(measurement["interaction_id"]),
                str(measurement["profiling_run_id"]),
                int(measurement["schema_version"]),
                measurement.get("owner_id"),
                measurement.get("job_id"),
                measurement.get("thread_id"),
                measurement.get("sandbox_id"),
                measurement.get("host_id"),
                measurement.get("attempt_id"),
                measurement.get("lease_generation"),
                str(measurement["operation"]),
                str(measurement.get("request_kind") or ""),
                int(measurement["boundary_version"]),
                str(measurement.get("coverage_scope") or ""),
                str(measurement["producer"]),
                str(measurement.get("producer_instance_id") or ""),
                int(measurement["duration_us"]),
                str(measurement["outcome"]),
                bool(measurement["collection_complete"]),
                bool(measurement.get("end_to_end_complete", False)),
                measurement.get("startup_path"),
                measurement["observed_at"],
                json.dumps(dict(measurement["record"])),
                str(measurement["payload_hash"]),
            )
            if inserted is not None:
                return "stored"
            stored = await conn.fetchval(
                "SELECT payload_hash FROM agent_operation_measurements WHERE measurement_id = $1",
                str(measurement["measurement_id"]),
            )
            if stored == str(measurement["payload_hash"]):
                return "duplicate"
            # The row is left exactly as it is. A retry that disagrees with the
            # sample it names is a schema or identity problem, and replacing
            # the first record with the second would hide it.
            return "conflict"

    # -- read --------------------------------------------------------------

    async def list_profiling_runs(
        self, *, since: datetime | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the experiment catalogue, most recent first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT run_id, operations, target, expires_at, config_version,
                       admission_instances, created_at, received_at
                FROM agent_operation_profiling_runs
                WHERE ($1::timestamptz IS NULL OR received_at >= $1)
                ORDER BY received_at DESC, run_id DESC
                LIMIT $2
                """,
                since,
                limit,
            )
        return [_run_row(row) for row in rows]

    async def get_profiling_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one experiment, or ``None`` when it was never recorded."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT run_id, operations, target, expires_at, config_version,
                       admission_instances, created_at, received_at
                FROM agent_operation_profiling_runs
                WHERE run_id = $1
                """,
                run_id,
            )
        return _run_row(row) if row is not None else None

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
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one keyset page of measurements, newest first.

        Keyset on ``(received_at, measurement_id)`` rather than ``OFFSET``: a
        new sample arriving mid-page would shift every later row and make an
        offset cursor skip one and repeat another.
        """
        conditions: list[str] = []
        params: list[Any] = []

        def bind(value: Any) -> str:
            params.append(value)
            return f"${len(params)}"

        for field, value in (
            ("run_id", run_id),
            ("outcome", outcome),
            ("collection_complete", collection_complete),
            ("job_id", job_id),
            ("thread_id", thread_id),
        ):
            if value is None:
                continue
            conditions.append(f"{_FILTERS[field]} = {bind(value)}")
        for column, values in (
            ("operation", operations),
            ("request_kind", request_kinds),
            ("coverage_scope", coverage_scopes),
        ):
            if not values:
                continue
            conditions.append(f"{column} = ANY({bind(list(values))}::text[])")
        if after is not None:
            received_at, measurement_id = after
            conditions.append(
                f"(received_at, measurement_id) < ({bind(received_at)}, {bind(measurement_id)})"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT measurement_id, interaction_id, profiling_run_id, schema_version,
                       owner_id, job_id, thread_id, sandbox_id, host_id,
                       attempt_id, lease_generation,
                       operation, request_kind, boundary_version, coverage_scope,
                       producer, producer_instance_id,
                       duration_us, outcome, collection_complete, end_to_end_complete,
                       startup_path, observed_at, received_at
                FROM agent_operation_measurements
                {where}
                ORDER BY received_at DESC, measurement_id DESC
                LIMIT {bind(limit)}
                """,
                *params,
            )
        return [_summary_row(row) for row in rows]

    async def get_operation_measurement(self, measurement_id: str) -> dict[str, Any] | None:
        """Return one stored measurement with its frozen record, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT measurement_id, interaction_id, profiling_run_id, schema_version,
                       owner_id, job_id, thread_id, sandbox_id, host_id,
                       attempt_id, lease_generation,
                       operation, request_kind, boundary_version, coverage_scope,
                       producer, producer_instance_id,
                       duration_us, outcome, collection_complete, end_to_end_complete,
                       startup_path, observed_at, received_at, record, payload_hash
                FROM agent_operation_measurements
                WHERE measurement_id = $1
                """,
                measurement_id,
            )
        if row is None:
            return None
        detail = _summary_row(row)
        detail["record"] = json.loads(row["record"])
        detail["payload_hash"] = row["payload_hash"]
        return detail

    async def summarize_operation_measurements(
        self, *, run_id: str, group_by: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return counts and percentiles over a whole experiment, not one page.

        Computed in SQL because the page a reader is looking at is not the
        population a percentile is about.
        """
        dimensions = [name for name in (group_by or _DEFAULT_GROUPS) if name in _GROUPS]
        selected = ", ".join(_GROUPS[name] for name in dimensions)
        prefix = f"{selected}, " if selected else ""
        grouping = f"GROUP BY {selected} ORDER BY {selected}" if selected else ""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {prefix}
                       COUNT(*) AS sample_count,
                       COUNT(DISTINCT interaction_id) AS interaction_count,
                       COUNT(*) FILTER (WHERE NOT collection_complete) AS incomplete_count,
                       COUNT(*) FILTER (WHERE outcome <> 'completed'
                                          AND outcome <> 'dispatched') AS failed_count,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_us) AS p50_us,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_us) AS p95_us,
                       MIN(duration_us) AS min_us,
                       MAX(duration_us) AS max_us
                FROM agent_operation_measurements
                WHERE profiling_run_id = $1
                {grouping}
                """,
                run_id,
            )
        return [{**dict(row), "group": {name: row[name] for name in dimensions}} for row in rows]


#: The dimensions a distribution may be folded by, and the column each is.
#: A caller names a dimension, never a SQL fragment.
_GROUPS: dict[str, str] = {
    "operation": "operation",
    "request_kind": "request_kind",
    "boundary_version": "boundary_version",
    "coverage_scope": "coverage_scope",
    "startup_path": "startup_path",
}

_DEFAULT_GROUPS = ("operation", "request_kind", "boundary_version", "coverage_scope")


def _summary_row(row: Any) -> dict[str, Any]:
    """Return one measurement's summary columns as plain data."""
    return {
        "measurement_id": row["measurement_id"],
        "interaction_id": row["interaction_id"],
        "profiling_run_id": row["profiling_run_id"],
        "schema_version": row["schema_version"],
        "owner_id": row["owner_id"],
        "job_id": row["job_id"],
        "thread_id": row["thread_id"],
        "sandbox_id": row["sandbox_id"],
        "host_id": row["host_id"],
        "attempt_id": row["attempt_id"],
        "lease_generation": row["lease_generation"],
        "operation": row["operation"],
        "request_kind": row["request_kind"],
        "boundary_version": row["boundary_version"],
        "coverage_scope": row["coverage_scope"],
        "producer": row["producer"],
        "producer_instance_id": row["producer_instance_id"],
        "duration_us": row["duration_us"],
        "outcome": row["outcome"],
        "collection_complete": row["collection_complete"],
        "end_to_end_complete": row["end_to_end_complete"],
        "startup_path": row["startup_path"],
        "observed_at": row["observed_at"],
        "received_at": row["received_at"],
    }


def _run_row(row: Any) -> dict[str, Any]:
    """Return one experiment catalogue entry as plain data."""
    return {
        "run_id": row["run_id"],
        "operations": list(row["operations"]),
        "target": json.loads(row["target"]),
        "expires_at": row["expires_at"],
        "config_version": row["config_version"],
        "admission_instances": json.loads(row["admission_instances"]),
        "created_at": row["created_at"],
        "received_at": row["received_at"],
    }


__all__ = [
    "PostgresOperationMeasurementRepository",
]
