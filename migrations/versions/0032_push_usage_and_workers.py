"""Push usage, the message queue, tool approvals and publication; wake the workers.

Two kinds of waiting are replaced here.

**Owners' tabs.** After ``0031`` a tab learns about its turns and conversations
from the change stream, but four things it shows still lived in tables nothing
announced, so the tab kept asking for them on a clock: the run's usage (every
15 s), the messages waiting to become turns (2-5 s), tool calls waiting for the
owner's approval (3 s), and the workspace's publication progress (5 s). Each of
those tables now notifies ``agent_owner_changes`` like ``0031``'s triggers do,
with a ``kind`` the stream forwards as a hint: ``usage`` and ``mcp`` name the
job, ``queue`` and ``publication`` name the conversation. The tab then reads
that one thing again.

**Background workers.** The snapshot coordinator woke every 5 s, the publisher
every 15 s, and the usage collector every 2 s, to look for work. They now wait
on ``agent_background_work`` as well, whose payload says which of them has
something new:

* ``snapshot``: a snapshot is created or set pending again by a new turn, a
  workspace becomes quiet (no operation, no active turn), a turn ends, or an
  earlier snapshot is published. A capture the coordinator *deferred* goes
  back to pending without a wake-up, so a host that is busy is retried on the
  coordinator's own clock and not in a tight loop.
* ``publish``: a snapshot is captured or published, or a workspace starts
  importing its published base.
* ``ledger``: an attempt stops running (its closing usage read is due), or a
  refresh is scheduled. A schedule may lie a few seconds ahead; the collector
  waits until the earliest one.

Every worker keeps its clock as a backstop, so a lost notification costs at
most what it cost before.

Revision ID: 0032_push_usage_and_workers
Revises: 0031_owner_change_notify
"""

from __future__ import annotations

from alembic import op

revision = "0032_push_usage_and_workers"
down_revision = "0031_owner_change_notify"
branch_labels = None
depends_on = None

_FUNCTIONS = (
    "agent_notify_owner(text, text, text)",
    "agent_notify_usage_changed()",
    "agent_notify_queue_changed()",
    "agent_notify_mcp_changed()",
    "agent_notify_publication_changed()",
    "agent_notify_background(text)",
    "agent_notify_background_snapshot_state()",
    "agent_wake_background_from_trigger()",
)

# (trigger, table) in creation order; dropped in reverse.
_TRIGGERS = (
    ("agent_attempt_ledger_usage_inserted", "agent_attempt_ledger"),
    ("agent_attempt_ledger_usage_changed", "agent_attempt_ledger"),
    ("agent_pending_messages_queue_changed", "agent_pending_messages"),
    ("agent_mcp_invocations_inserted", "agent_mcp_invocations"),
    ("agent_mcp_invocations_changed", "agent_mcp_invocations"),
    ("agent_mcp_invocations_deleted", "agent_mcp_invocations"),
    ("agent_sandbox_snapshots_publication_changed", "agent_sandbox_snapshots"),
    ("agent_sandbox_snapshots_background", "agent_sandbox_snapshots"),
    ("agent_sandboxes_snapshot_due", "agent_sandboxes"),
    ("agent_sandboxes_import_due", "agent_sandboxes"),
    ("agent_thread_sandbox_memberships_quiet", "agent_thread_sandbox_memberships"),
    ("agent_jobs_snapshot_due", "agent_jobs"),
    ("agent_attempts_ledger_due", "agent_attempts"),
    ("agent_attempt_ledger_refresh_scheduled", "agent_attempt_ledger"),
)


def upgrade() -> None:
    """Install the owner hints and the background wake-ups."""
    # -- Owners' tabs -----------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION agent_notify_owner(owner TEXT, kind TEXT, row_id TEXT) RETURNS void
        LANGUAGE sql AS $$
            SELECT pg_notify(
                'agent_owner_changes',
                json_build_object('user', owner, 'kind', kind, 'id', row_id)::text
            )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_notify_usage_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM agent_notify_owner(j.user_id, 'usage', j.id)
            FROM agent_attempts a JOIN agent_jobs j ON j.id = a.job_id
            WHERE a.id = NEW.attempt_id;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_attempt_ledger_usage_inserted
            AFTER INSERT ON agent_attempt_ledger
            FOR EACH ROW EXECUTE FUNCTION agent_notify_usage_changed()
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_attempt_ledger_usage_changed
            AFTER UPDATE ON agent_attempt_ledger
            FOR EACH ROW
            WHEN (OLD.snapshot IS DISTINCT FROM NEW.snapshot
                  OR OLD.queried_at IS DISTINCT FROM NEW.queried_at
                  OR OLD.last_error_code IS DISTINCT FROM NEW.last_error_code)
            EXECUTE FUNCTION agent_notify_usage_changed()
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_notify_queue_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            changed RECORD;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                changed := OLD;
            ELSE
                changed := NEW;
            END IF;
            PERFORM agent_notify_owner(changed.user_id, 'queue', changed.thread_id);
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_pending_messages_queue_changed
            AFTER INSERT OR UPDATE OR DELETE ON agent_pending_messages
            FOR EACH ROW EXECUTE FUNCTION agent_notify_queue_changed()
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_notify_mcp_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            changed RECORD;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                changed := OLD;
            ELSE
                changed := NEW;
            END IF;
            PERFORM agent_notify_owner(j.user_id, 'mcp', j.id)
            FROM agent_jobs j WHERE j.id = changed.job_id;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_mcp_invocations_inserted
            AFTER INSERT ON agent_mcp_invocations
            FOR EACH ROW EXECUTE FUNCTION agent_notify_mcp_changed()
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_mcp_invocations_changed
            AFTER UPDATE OF status ON agent_mcp_invocations
            FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
            EXECUTE FUNCTION agent_notify_mcp_changed()
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_mcp_invocations_deleted
            AFTER DELETE ON agent_mcp_invocations
            FOR EACH ROW EXECUTE FUNCTION agent_notify_mcp_changed()
        """
    )
    # Publication progress is the newest snapshot of the workspace, which
    # every conversation in it (a fork shares its origin's) shows.
    op.execute(
        """
        CREATE FUNCTION agent_notify_publication_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM agent_notify_owner(m.user_id, 'publication', m.thread_id)
            FROM agent_thread_sandbox_memberships m
            WHERE m.sandbox_id = NEW.sandbox_id;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_sandbox_snapshots_publication_changed
            AFTER INSERT OR UPDATE OF state ON agent_sandbox_snapshots
            FOR EACH ROW EXECUTE FUNCTION agent_notify_publication_changed()
        """
    )

    # -- Background workers -----------------------------------------------
    op.execute(
        """
        CREATE FUNCTION agent_notify_background(kind TEXT) RETURNS void
        LANGUAGE sql AS $$ SELECT pg_notify('agent_background_work', kind) $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_notify_background_snapshot_state() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM agent_notify_background('snapshot');
            ELSIF NEW.state IS DISTINCT FROM OLD.state THEN
                -- Back to pending from capturing is a deferral: retried on the
                -- coordinator's clock, never woken into a loop.
                IF NEW.state = 'pending' AND OLD.state <> 'capturing' THEN
                    PERFORM agent_notify_background('snapshot');
                ELSIF NEW.state = 'captured' THEN
                    PERFORM agent_notify_background('publish');
                ELSIF NEW.state = 'published' THEN
                    PERFORM agent_notify_background('snapshot');
                    PERFORM agent_notify_background('publish');
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_sandbox_snapshots_background
            AFTER INSERT OR UPDATE OF state ON agent_sandbox_snapshots
            FOR EACH ROW EXECUTE FUNCTION agent_notify_background_snapshot_state()
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_wake_background_from_trigger() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM agent_notify_background(TG_ARGV[0]);
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_sandboxes_snapshot_due
            AFTER UPDATE ON agent_sandboxes
            FOR EACH ROW
            WHEN ((NEW.snapshot_state = 'pending'
                   AND OLD.snapshot_state IS DISTINCT FROM 'pending')
                  OR (NEW.active_operation_count = 0 AND OLD.active_operation_count <> 0))
            EXECUTE FUNCTION agent_wake_background_from_trigger('snapshot')
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_sandboxes_import_due
            AFTER UPDATE ON agent_sandboxes
            FOR EACH ROW
            WHEN (NEW.snapshot_state = 'importing'
                  AND OLD.snapshot_state IS DISTINCT FROM 'importing')
            EXECUTE FUNCTION agent_wake_background_from_trigger('publish')
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_thread_sandbox_memberships_quiet
            AFTER UPDATE OF active_attempt_id ON agent_thread_sandbox_memberships
            FOR EACH ROW
            WHEN (OLD.active_attempt_id IS NOT NULL AND NEW.active_attempt_id IS NULL)
            EXECUTE FUNCTION agent_wake_background_from_trigger('snapshot')
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_jobs_snapshot_due
            AFTER UPDATE OF state ON agent_jobs
            FOR EACH ROW
            WHEN (OLD.state IS DISTINCT FROM NEW.state
                  AND NEW.state IN ('succeeded', 'failed', 'cancelled'))
            EXECUTE FUNCTION agent_wake_background_from_trigger('snapshot')
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_attempts_ledger_due
            AFTER UPDATE OF status ON agent_attempts
            FOR EACH ROW
            WHEN (OLD.status = 'running' AND NEW.status <> 'running')
            EXECUTE FUNCTION agent_wake_background_from_trigger('ledger')
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_attempt_ledger_refresh_scheduled
            AFTER UPDATE OF next_refresh_at ON agent_attempt_ledger
            FOR EACH ROW
            WHEN (NEW.next_refresh_at IS NOT NULL
                  AND NEW.next_refresh_at IS DISTINCT FROM OLD.next_refresh_at)
            EXECUTE FUNCTION agent_wake_background_from_trigger('ledger')
        """
    )


def downgrade() -> None:
    """Remove the triggers and their functions; every reader is back on its clock."""
    for trigger, table in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for function in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
