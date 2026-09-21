"""Let an administrator start and stop operation collection without a redeploy.

Revision 0017 made an experiment something the database could describe after
the fact. This one makes it something the database can *command*: a singleton
control row an Admin Start/Stop advances, and an idempotency record so a lost
response is retried rather than duplicated.

Deliberately absent is any per-process state. A participant table would exist
to answer "has every process applied this revision", and the feature does not
ask that question: the control card reports the *configuration*, the sync is
allowed to lag, and a process that has not caught up yet simply keeps selecting
until the experiment's own deadline. Adding the table back would mean inventing
the acknowledgements that would fill it.

Four decisions are worth reading before changing anything here.

**The control row is seeded, not lazily created.** A singleton row that a
writer has to create is a row two writers can both decide to create, and
``SELECT ... FOR UPDATE`` cannot lock a row that does not exist. The migration
inserts revision 0 -- "no experiment, nothing ever commanded" -- and every
writer locks that existing row.

**``deployment`` is part of the key.** The same database may serve more than one
deployment, and the namespace must come from trusted configuration rather than
from a request: a browser that could name its deployment could stop another
one's experiment.

**``revision``, ``config_version`` and the renewal sequence are three different
orders and are stored as three different things.** ``config_version`` (on the
run catalogue, from 0017) says which immutable experiment this is; ``revision``
says which start/stop command was last applied; and the renewal sequence, which
never needs to be stored, says which authorization refresh a process last saw.
Collapsing them is how a renewal gets mistaken for a new experiment.

**Lifecycle is additive and never rewrites the manifest.** ``agent_operation_
profiling_runs`` keeps the ``expires_at`` the administrator asked for. When a
run is stopped early, ``stopped_at`` records that; the configured expiry is left
alone, because the delivery path compares a producer's manifest against this row
and a rewritten expiry would turn every in-flight upload into a ``run_conflict``.

Revision ID: 0018_profiling_control
Revises: 0017_operation_measurements
"""

from __future__ import annotations

from alembic import op

revision = "0018_profiling_control"
down_revision = "0017_operation_measurements"
branch_labels = None
depends_on = None

#: The deployment whose control row this schema is built around. Rows are keyed
#: by deployment, and this is the default namespace a single-deployment install
#: uses; a deployment that sets its own namespaces itself by writing its key.
DEFAULT_DEPLOYMENT = "default"

#: The key that answers for a deployment which has never been commanded under
#: its own name. Without it, naming a deployment would silently disable Admin
#: control until somebody inserted a row by hand -- a configuration change whose
#: symptom is "the button does nothing", which is the worst kind to debug.
#: ``''`` is safe as a sentinel because a configured name is never empty: the
#: control plane falls back to :data:`DEFAULT_DEPLOYMENT` rather than to "".
FALLBACK_DEPLOYMENT = ""

#: Lifecycle values ``agent_operation_profiling_runs.lifecycle`` may hold.
#: Declared as a constraint rather than free text so a typo cannot become a
#: fifth state that no reader knows how to render.
_LIFECYCLE_VALUES = ("pending", "active", "stopped", "expired", "failed")

#: The same values as a SQL ``IN`` list, so the constraint is generated from the
#: one declaration above rather than restated where it can drift from it.
_LIFECYCLE_SQL = ", ".join(f"'{value}'" for value in _LIFECYCLE_VALUES)

#: The comment on the column, generated the same way: a reader of the database
#: should not have to find this file to learn what the values mean.
_LIFECYCLE_COMMENT = (
    "One of: " + ", ".join(_LIFECYCLE_VALUES) + ". NULL means this experiment "
    "predates runtime control and no control state was ever recorded for it."
)

def upgrade() -> None:
    """Add the control row, its command log and the run lifecycle columns."""
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS agent_operation_profiling_control (
            deployment TEXT PRIMARY KEY,
            revision BIGINT NOT NULL DEFAULT 0,
            desired_state TEXT NOT NULL DEFAULT 'off',
            current_run_id TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT,
            -- The revision at which the last run was stopped or expired, and
            -- when. Kept beside the current run rather than derived from the
            -- command log because the "is the previous experiment still
            -- propagating?" guard reads it on every Start, and a tombstone
            -- that has to be reconstructed is one that can be reconstructed
            -- wrongly.
            last_stop_revision BIGINT,
            last_stopped_at TIMESTAMPTZ,
            last_stopped_run_id TEXT,
            CONSTRAINT ck_agent_operation_profiling_control_desired_state
                CHECK (desired_state IN ('off', 'on')),
            CONSTRAINT ck_agent_operation_profiling_control_revision
                CHECK (revision >= 0),
            -- "on" without a run is not a state anybody can act on, and a
            -- reader that had to guess which of the two to believe would
            -- eventually guess "collect".
            CONSTRAINT ck_agent_operation_profiling_control_on_has_run
                CHECK (desired_state <> 'on' OR current_run_id IS NOT NULL)
        )
        """
    )
    # Seeded, so every writer has a row to lock. ``ON CONFLICT DO NOTHING``
    # makes the upgrade idempotent and leaves an existing control row -- and
    # therefore a running experiment -- exactly as it was.
    op.execute(
        f"""
        INSERT INTO agent_operation_profiling_control (deployment, revision, desired_state)
        VALUES ('{DEFAULT_DEPLOYMENT}', 0, 'off'), ('{FALLBACK_DEPLOYMENT}', 0, 'off')
        ON CONFLICT (deployment) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_operation_profiling_commands (
            deployment TEXT NOT NULL,
            request_id TEXT NOT NULL,
            action TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            revision BIGINT NOT NULL,
            run_id TEXT,
            status TEXT NOT NULL,
            result JSONB NOT NULL DEFAULT '{}'::jsonb,
            actor_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (deployment, request_id),
            CONSTRAINT ck_agent_operation_profiling_commands_action
                CHECK (action IN ('start', 'stop')),
            -- A command that was accepted carries the revision it created, so
            -- a retry can answer with the same revision instead of re-applying
            -- the command it already applied.
            CONSTRAINT ck_agent_operation_profiling_commands_revision
                CHECK (revision >= 0),
            CONSTRAINT ck_agent_operation_profiling_commands_result_object
                CHECK (jsonb_typeof(result) = 'object')
        )
        """
    )
    # The idempotency lookup is by (deployment, request_id), which the primary
    # key already serves. This index serves the other read: what was commanded
    # recently, for an operator looking at why the control row says what it says.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_operation_profiling_commands_created
            ON agent_operation_profiling_commands (deployment, created_at DESC)
        """
    )

    # Lifecycle is additive and nullable on purpose: a run catalogued by a
    # producer before this revision existed has no lifecycle, and "this
    # experiment predates runtime control" is an answer the Admin surface can
    # give. A default of 'active' would have invented a state for history.
    op.execute(
        """
        ALTER TABLE agent_operation_profiling_runs
            ADD COLUMN IF NOT EXISTS source TEXT,
            ADD COLUMN IF NOT EXISTS lifecycle TEXT,
            ADD COLUMN IF NOT EXISTS created_by TEXT,
            ADD COLUMN IF NOT EXISTS control_revision BIGINT,
            ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS stopped_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS stop_reason TEXT,
            ADD COLUMN IF NOT EXISTS deployment TEXT
        """
    )
    op.execute(
        f"""
        ALTER TABLE agent_operation_profiling_runs
            ADD CONSTRAINT ck_agent_operation_profiling_runs_lifecycle
            CHECK (lifecycle IS NULL OR lifecycle IN ({_LIFECYCLE_SQL}))
        """
    )
    # The Admin control card asks one question on every poll -- which runs are
    # live for this deployment -- and a sequential scan of the catalogue is the
    # wrong way to answer a page that refreshes every five seconds.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_operation_profiling_runs_lifecycle
            ON agent_operation_profiling_runs (deployment, lifecycle, received_at DESC)
        """
    )
    op.execute(
        f"""
        COMMENT ON COLUMN agent_operation_profiling_runs.lifecycle IS
            '{_LIFECYCLE_COMMENT.replace("'", "''")}'
        """
    )


def downgrade() -> None:
    """Drop the control surface and the lifecycle columns it added.

    Only downgrade with collection switched off. Dropping the control row while
    a producer is collecting leaves it collecting until its own bounded
    authorization lapses, which is the behaviour the design wants -- the local
    deadline is what stops it, not the presence of this row.
    """
    op.execute("DROP INDEX IF EXISTS ix_agent_operation_profiling_runs_lifecycle")
    op.execute(
        """
        COMMENT ON COLUMN agent_operation_profiling_runs.lifecycle IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE agent_operation_profiling_runs
            DROP CONSTRAINT IF EXISTS ck_agent_operation_profiling_runs_lifecycle
        """
    )
    op.execute(
        """
        ALTER TABLE agent_operation_profiling_runs
            DROP COLUMN IF EXISTS deployment,
            DROP COLUMN IF EXISTS stop_reason,
            DROP COLUMN IF EXISTS stopped_at,
            DROP COLUMN IF EXISTS started_at,
            DROP COLUMN IF EXISTS control_revision,
            DROP COLUMN IF EXISTS created_by,
            DROP COLUMN IF EXISTS lifecycle,
            DROP COLUMN IF EXISTS source
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_agent_operation_profiling_commands_created")
    op.execute("DROP TABLE IF EXISTS agent_operation_profiling_commands")
    op.execute("DROP TABLE IF EXISTS agent_operation_profiling_control")
