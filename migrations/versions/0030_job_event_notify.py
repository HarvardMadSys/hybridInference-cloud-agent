"""Tell an owner's event stream the moment its job has something new.

The stream behind a chat read ``agent_job_events`` and its job row once a
second, whether or not anything had happened: up to a second of delay on every
event and two queries a second for every open chat. It now waits for Postgres to
notify ``agent_job_events`` with the job's id, which these triggers do when

* an event is stored for the job, and
* the job's ``state`` changes, which is how a stream learns that the job ended
  and it should close.

The notification only says which job to read. The stream still reads the table
from its cursor, so a notification that is lost costs a delay, never an event.
They are row triggers on the same writes that store the rows; Postgres folds
repeats of one job in a transaction into one and delivers it only at commit, so
a woken stream always finds the row that woke it.

Revision ID: 0030_job_event_notify
Revises: 0029_work_available_notify
"""

from __future__ import annotations

from alembic import op

revision = "0030_job_event_notify"
down_revision = "0029_work_available_notify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Notify ``agent_job_events`` with the job id on a new event or state."""
    op.execute(
        """
        CREATE FUNCTION agent_notify_job_event_stored() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('agent_job_events', NEW.job_id);
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_notify_job_state_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('agent_job_events', NEW.id);
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_job_events_stored
            AFTER INSERT ON agent_job_events
            FOR EACH ROW
            EXECUTE FUNCTION agent_notify_job_event_stored()
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_jobs_state_changed
            AFTER UPDATE OF state ON agent_jobs
            FOR EACH ROW
            WHEN (OLD.state IS DISTINCT FROM NEW.state)
            EXECUTE FUNCTION agent_notify_job_state_changed()
        """
    )


def downgrade() -> None:
    """Remove the triggers and their functions; the code before this reads on a clock."""
    op.execute("DROP TRIGGER IF EXISTS agent_jobs_state_changed ON agent_jobs")
    op.execute("DROP TRIGGER IF EXISTS agent_job_events_stored ON agent_job_events")
    op.execute("DROP FUNCTION IF EXISTS agent_notify_job_state_changed()")
    op.execute("DROP FUNCTION IF EXISTS agent_notify_job_event_stored()")
