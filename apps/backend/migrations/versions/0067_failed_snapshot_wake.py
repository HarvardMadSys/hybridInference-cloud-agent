"""Wake the capture coordinator when a workspace snapshot fails.

Revision ID: 0067_failed_snapshot_wake
Revises: 0066_admin_job_listing_index

A failed snapshot no longer holds back the later snapshots of its group: the
next capture is cut from the same published base and supersedes it. A turn
that settled while the failed snapshot was on its way left a pending snapshot
behind it, which becomes capturable the moment that snapshot fails. The
coordinator sleeps until it is told of work, and nothing told it, so the
capture waited for its idle timeout. Now a snapshot entering ``failed`` wakes
it, as one entering ``published`` does.

Publication needs no such notice. A later snapshot is captured only once every
earlier one has finished, so a failure never leaves a captured one behind it.

``CREATE OR REPLACE FUNCTION`` is transactional, so the offline SQL runs inside
the legacy copy's one conversion transaction like every other revision's. The
trigger is unchanged and keeps calling the function by name.
"""

from alembic import op

revision = "0067_failed_snapshot_wake"
down_revision = "0066_admin_job_listing_index"
branch_labels = None
depends_on = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION agent_notify_background_snapshot_state() RETURNS trigger
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
            PERFORM agent_notify_background('publish');{failed}
        END IF;
    END IF;
    RETURN NULL;
END;
$$
"""

#: A failed snapshot is terminal, so this wake-up cannot repeat into a loop.
_FAILED = """
        ELSIF NEW.state = 'failed' THEN
            -- The group's later pending snapshot supersedes it, and may be
            -- capturable now.
            PERFORM agent_notify_background('snapshot');"""


def upgrade() -> None:
    """Announce a failed snapshot to the capture coordinator."""
    op.execute(_FUNCTION.replace("{failed}", _FAILED))


def downgrade() -> None:
    """Restore the function ``0032_push_usage_and_workers`` created."""
    op.execute(_FUNCTION.replace("{failed}", ""))
