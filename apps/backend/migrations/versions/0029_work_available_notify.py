"""Tell waiting hosts the moment there may be work to claim.

Hosts asked for work every five seconds, so a new message waited for the next
ask: 2.5 s on average. A host that finds nothing now parks a request on
``/v1/agent/worker/wait``, and the control plane answers it when Postgres
notifies ``agent_work_available``.

The notification comes from triggers rather than from the code paths that queue
work, because a turn becomes claimable in many of them: a new message, a goal
round or a sent pending message inserts one; a turn that ends releases the
queued follow-up behind it; a released or reaped claim puts one back. Each of
those writes ``agent_jobs``, and the triggers see it whichever path made it:

* an insert of a ``queued`` job;
* any change of ``state`` except to ``running``. That one is a claim, and a
  claim makes nothing else claimable.

They are row triggers, so a statement that changes no row, such as the reaper's
periodic update with nothing expired, notifies nobody. The notification carries
no payload; Postgres folds repeats in one transaction into one and delivers it
only at commit, so a woken host always sees the row that woke it.

Anything else that makes a turn claimable is not signalled: a moved host pin, a
finished fork releasing its source chat, compute that became available. Hosts
wait no longer than the poll they replace, so those are found when the wait
times out, no later than before.

Revision ID: 0029_work_available_notify
Revises: 0028_host_settings
"""

from __future__ import annotations

from alembic import op

revision = "0029_work_available_notify"
down_revision = "0028_host_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Notify ``agent_work_available`` when a job is queued, requeued or ends."""
    op.execute(
        """
        CREATE FUNCTION agent_notify_work_available() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('agent_work_available', '');
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_jobs_work_queued
            AFTER INSERT ON agent_jobs
            FOR EACH ROW WHEN (NEW.state = 'queued')
            EXECUTE FUNCTION agent_notify_work_available()
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_jobs_work_released
            AFTER UPDATE OF state ON agent_jobs
            FOR EACH ROW
            WHEN (OLD.state IS DISTINCT FROM NEW.state AND NEW.state <> 'running')
            EXECUTE FUNCTION agent_notify_work_available()
        """
    )


def downgrade() -> None:
    """Remove the triggers and their function; idle hosts fall back to polling."""
    op.execute("DROP TRIGGER IF EXISTS agent_jobs_work_released ON agent_jobs")
    op.execute("DROP TRIGGER IF EXISTS agent_jobs_work_queued ON agent_jobs")
    op.execute("DROP FUNCTION IF EXISTS agent_notify_work_available()")
