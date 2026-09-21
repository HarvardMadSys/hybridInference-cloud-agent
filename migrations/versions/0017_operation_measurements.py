"""Store opt-in platform operation measurements for the Admin reading surface.

The profiler emits one record per selected root operation and, until now, that
record went only to a log line and an optional JSONL file. That made a baseline
something an operator assembled by hand from whichever host happened to keep
the file, and it made a file read and a sandbox startup impossible to read
together: they are the same kind of measurement and belong in one table.

Two tables, and the split is deliberate. ``agent_operation_measurements`` holds
one immutable record per root request -- identity, the attribution the control
plane resolved rather than the payload's own claims, a filterable summary, and
the validated record itself. ``agent_operation_profiling_runs`` is the
experiment catalogue the measurements point at: what a run selected, until
when, and which admission instances held what budget. The run is a manifest,
not a quota allocator; the per-process budget stays where it is enforced.

Every column is additive. Nothing existing is renamed, dropped or backfilled,
and a deployment that never enables persistence keeps exactly the schema it
had.

Revision ID: 0017_operation_measurements
Revises: 0016_job_attachments
"""

from __future__ import annotations

from alembic import op

revision = "0017_operation_measurements"
down_revision = "0016_job_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the measurement table, the run catalogue, and their read indexes."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_operation_profiling_runs (
            run_id TEXT PRIMARY KEY,
            operations TEXT[] NOT NULL,
            target JSONB NOT NULL DEFAULT '{}'::jsonb,
            expires_at TIMESTAMPTZ NOT NULL,
            config_version INTEGER NOT NULL DEFAULT 1,
            admission_instances JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_agent_operation_profiling_runs_operations
                CHECK (cardinality(operations) > 0),
            CONSTRAINT ck_agent_operation_profiling_runs_target_object
                CHECK (jsonb_typeof(target) = 'object'),
            -- The budget is per admission instance, so its shape is checked
            -- here rather than trusted: a manifest that claimed a fleet-wide
            -- cap would be read as one by the Admin page.
            CONSTRAINT ck_agent_operation_profiling_runs_admissions
                CHECK (jsonb_typeof(admission_instances) = 'array')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_operation_measurements (
            measurement_id TEXT PRIMARY KEY,
            interaction_id TEXT NOT NULL,
            profiling_run_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            owner_id TEXT,
            job_id TEXT,
            thread_id TEXT,
            sandbox_id TEXT,
            host_id TEXT,
            attempt_id BIGINT,
            lease_generation INTEGER,
            operation TEXT NOT NULL,
            request_kind TEXT NOT NULL DEFAULT '',
            boundary_version INTEGER NOT NULL,
            coverage_scope TEXT NOT NULL DEFAULT '',
            producer TEXT NOT NULL,
            producer_instance_id TEXT NOT NULL DEFAULT '',
            duration_us BIGINT NOT NULL,
            outcome TEXT NOT NULL,
            collection_complete BOOLEAN NOT NULL,
            end_to_end_complete BOOLEAN NOT NULL DEFAULT FALSE,
            startup_path TEXT,
            runtime_id TEXT,
            runtime_version TEXT,
            execution_profile TEXT,
            observed_at TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            record JSONB NOT NULL,
            payload_hash TEXT NOT NULL,
            CONSTRAINT ck_agent_operation_measurements_duration
                CHECK (duration_us >= 0),
            -- A record is an object. An array or a scalar would make every
            -- later reader guess at its shape rather than reject it once.
            CONSTRAINT ck_agent_operation_measurements_record_object
                CHECK (jsonb_typeof(record) = 'object')
        )
        """
    )
    # The three read paths the Admin surface actually walks: an experiment's
    # samples in arrival order, one chat's history, and one attempt's startup.
    # Each carries measurement_id as a tiebreaker so a cursor cannot skip or
    # repeat a row when two share a timestamp.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_operation_measurements_run
            ON agent_operation_measurements (profiling_run_id, received_at, measurement_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_operation_measurements_job_attempt
            ON agent_operation_measurements (job_id, attempt_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_operation_measurements_thread
            ON agent_operation_measurements (thread_id, received_at, measurement_id)
        """
    )


def downgrade() -> None:
    """Drop both tables. Only downgrade with the profiling writers stopped."""
    op.execute("DROP INDEX IF EXISTS ix_agent_operation_measurements_thread")
    op.execute("DROP INDEX IF EXISTS ix_agent_operation_measurements_job_attempt")
    op.execute("DROP INDEX IF EXISTS ix_agent_operation_measurements_run")
    op.execute("DROP TABLE IF EXISTS agent_operation_measurements")
    op.execute("DROP TABLE IF EXISTS agent_operation_profiling_runs")
