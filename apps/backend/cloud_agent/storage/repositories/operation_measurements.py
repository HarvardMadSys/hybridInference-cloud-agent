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
    "attempt_id": "attempt_id",
    "interaction_id": "interaction_id",
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
            an identical measurement was already there, ``"conflict"`` when the
            measurement id exists with different contents, and
            ``"run_conflict"`` when the experiment id already names a different
            experiment -- different operations, a different target, or another
            configuration version. The last two are refusals to be surfaced,
            never overwritten and never retried; a refused measurement is not
            stored.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            measurement_id = str(measurement["measurement_id"])
            # A row lock cannot protect an id that does not exist yet. The
            # transaction-scoped advisory lock serializes first writers for
            # this immutable id before either can create a run manifest.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                measurement_id,
            )
            existing_hash = await conn.fetchval(
                "SELECT payload_hash FROM agent_operation_measurements WHERE measurement_id = $1",
                measurement_id,
            )
            if existing_hash is not None and existing_hash != str(measurement["payload_hash"]):
                # Refuse before touching the run catalogue: otherwise a
                # colliding id under a fresh run commits a phantom run row.
                return "conflict"
            if run is not None:
                # The row is created first and locked second, in that order.
                # ``SELECT ... FOR UPDATE`` cannot lock a row that does not
                # exist yet, so two writers reporting one run for the first time
                # would both read nothing and then race the primary key -- one
                # of them surfacing a UniqueViolation instead of the outcome
                # this port promises. DO NOTHING makes the loser of that race
                # wait for the winner to commit and then continue to the lock.
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
                existing = await conn.fetchrow(
                    """
                    SELECT operations, target, expires_at, config_version,
                           admission_instances
                    FROM agent_operation_profiling_runs
                    WHERE run_id = $1
                    FOR UPDATE
                    """,
                    str(run["run_id"]),
                )
                if existing is None:  # pragma: no cover - the insert above made it
                    return "conflict"
                # Compared even when this call is the one that inserted the
                # row: an insert that lost the race is not an acceptance, and
                # neither is one that won it against a different manifest.
                conflict = _run_configuration_conflict(existing, run)
                if conflict is not None:
                    return "run_conflict"
                merged = _merge_admissions(existing["admission_instances"], run)
                if merged is None:
                    return "run_conflict"
                await conn.execute(
                    """
                    UPDATE agent_operation_profiling_runs
                    SET admission_instances = $2::jsonb
                    WHERE run_id = $1
                    """,
                    str(run["run_id"]),
                    json.dumps(merged),
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
                    startup_path, runtime_id, runtime_version, execution_profile,
                    observed_at, record, payload_hash
                )
                VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7, $8, $9,
                    $10, $11,
                    $12, $13, $14, $15,
                    $16, $17,
                    $18, $19, $20, $21,
                    $22, $23, $24, $25,
                    $26, $27::jsonb, $28
                )
                ON CONFLICT (measurement_id) DO NOTHING
                RETURNING measurement_id
                """,
                measurement_id,
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
                measurement.get("runtime_id"),
                measurement.get("runtime_version"),
                measurement.get("execution_profile"),
                measurement["observed_at"],
                json.dumps(dict(measurement["record"])),
                str(measurement["payload_hash"]),
            )
            if inserted is not None:
                # Catalogue recency means the latest newly stored sample, not
                # merely the first row that happened to create the manifest.
                await conn.execute(
                    """
                    UPDATE agent_operation_profiling_runs
                    SET received_at = NOW()
                    WHERE run_id = $1
                    """,
                    str(measurement["profiling_run_id"]),
                )
                return "stored"
            stored = await conn.fetchval(
                "SELECT payload_hash FROM agent_operation_measurements WHERE measurement_id = $1",
                measurement_id,
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
            ("attempt_id", attempt_id),
            ("interaction_id", interaction_id),
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
                       startup_path, runtime_id, runtime_version, execution_profile,
                       observed_at, received_at
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
                       startup_path, runtime_id, runtime_version, execution_profile,
                       observed_at, received_at, record, payload_hash
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
        unknown = sorted(name for name in (group_by or ()) if name not in _GROUPS)
        if unknown:
            # Silently dropping a dimension is how a caller ends up reading a
            # distribution of things they never asked to mix.
            raise ValueError(f"unknown grouping dimension(s): {', '.join(unknown)}")
        # The dimensions that decide whether two samples are comparable at all
        # cannot be grouped away. A caller may add to them, never remove them:
        # a p95 over two boundary versions is not a p95 of either.
        requested = list(group_by or ())
        dimensions = [name for name in _REQUIRED_GROUPS if name not in requested] + requested
        for name in _STARTUP_GROUPS:
            # Startup paths differ by precisely the work they skipped, so they
            # are separated whenever the samples are startups at all. Folding
            # them together produces an average of two different operations.
            if name not in dimensions:
                dimensions.append(name)
        selected = ", ".join(_GROUPS[name] for name in dimensions)
        # Percentiles use the samples the design calls eligible -- a completed
        # operation whose collection closed every boundary -- while the counts
        # report everything that was stored. Keeping failures out of the
        # distribution is not the same as hiding them, and only the second
        # would be a problem.
        eligible = "FILTER (WHERE collection_complete AND outcome IN ('completed', 'dispatched'))"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {selected},
                       COUNT(*) AS sample_count,
                       COUNT(DISTINCT interaction_id) AS interaction_count,
                       COUNT(*) {eligible} AS eligible_count,
                       COUNT(*) FILTER (WHERE NOT collection_complete) AS incomplete_count,
                       COUNT(*) FILTER (WHERE outcome NOT IN ('completed', 'dispatched'))
                           AS failed_count,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_us)
                           {eligible} AS p50_us,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_us)
                           {eligible} AS p95_us,
                       MIN(duration_us) {eligible} AS min_us,
                       MAX(duration_us) {eligible} AS max_us
                FROM agent_operation_measurements
                WHERE profiling_run_id = $1
                GROUP BY {selected}
                ORDER BY {selected}
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
    "runtime_id": "runtime_id",
    "runtime_version": "runtime_version",
    "execution_profile": "execution_profile",
}

#: Dimensions a caller may not group away.
_REQUIRED_GROUPS = (
    "operation",
    "request_kind",
    "boundary_version",
    "coverage_scope",
    "runtime_id",
    "runtime_version",
    "execution_profile",
)

#: Added whenever the samples are startups, because they separate the four
#: startup paths the runner classifies.
_STARTUP_GROUPS = ("startup_path",)


def _run_configuration_conflict(existing: Any, run: Mapping[str, Any]) -> str | None:
    """Return why an incoming manifest is not the experiment already stored.

    Compared are the parts that say *what an experiment is*: which operations
    it selects, which targets it scoped itself to, and which configuration
    version it was written for. A writer that disagrees on any of those is
    describing a different experiment, and its samples would be filed against
    a catalogue entry that says something else.

    ``expires_at`` is the *configured* absolute expiry, not the deadline a
    producer has left. The two are different values and only the first is
    identity: the configured instant is the same in every process reading one
    deployment setting, while the remaining time is re-derived per request and
    can be shortened by a producer. A writer that states a different moment is
    describing a different experiment -- one that ends tomorrow rather than in
    fifteen minutes -- and its samples would extend a catalogue entry that says
    otherwise.

    The stored value is a ``timestamptz`` and the incoming one is normalized to
    UTC, so two spellings of one instant compare equal and an unmodified retry
    passes.
    """
    if sorted(existing["operations"]) != sorted(str(item) for item in run["operations"]):
        return "operations"
    if json.loads(existing["target"]) != dict(run.get("target") or {}):
        return "target"
    if existing["expires_at"] != run["expires_at"]:
        return "expires_at"
    if int(existing["config_version"]) != int(run.get("config_version") or 1):
        return "config_version"
    return None


def _merge_admissions(existing: Any, run: Mapping[str, Any]) -> list[Any] | None:
    """Add this writer's admission instance to the manifest, idempotently.

    An experiment run by two control-plane replicas is two instances of one
    manifest, and each is allowed to report its own budget. A second report for
    an instance already listed has to agree with the first: a budget that
    changed mid-run is a different experiment, and keeping whichever arrived
    first would leave the page stating a limit nobody enforced.
    """
    merged = list(json.loads(existing))
    by_name = {
        str(item.get("instance")): item
        for item in merged
        if isinstance(item, dict) and item.get("instance")
    }
    for item in run.get("admission_instances") or []:
        if not isinstance(item, dict) or not item.get("instance"):
            continue
        name = str(item["instance"])
        if name in by_name:
            if by_name[name] != item:
                return None
            continue
        merged.append(item)
        by_name[name] = item
    return merged


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
        "runtime_id": row["runtime_id"],
        "runtime_version": row["runtime_version"],
        "execution_profile": row["execution_profile"],
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
