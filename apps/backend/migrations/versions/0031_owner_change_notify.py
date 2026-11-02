"""Tell an owner's open tabs when one of their conversations or turns changes.

The chat list, the sidebar and an open conversation learned about a new state,
title or pull request link by asking again every few seconds. They now hold one
stream per tab, and Postgres notifies ``agent_owner_changes`` when a row they
show is inserted, changed or removed. The payload names the owner, the kind of
row (``job`` or ``thread``) and its id, as JSON; the stream reads the row and
sends its current values.

Each row also gets ``revision``, a counter a ``BEFORE UPDATE`` trigger raises by
one whenever the row actually changes. A tab keeps the highest revision it has
seen for each row, so a value read earlier and delivered later never replaces a
newer one. ``updated_at`` cannot serve: several writes leave it alone, and
``NOW()`` is when a transaction started, not when it committed.

Revision ID: 0031_owner_change_notify
Revises: 0030_job_event_notify
"""

from __future__ import annotations

from alembic import op

revision = "0031_owner_change_notify"
down_revision = "0030_job_event_notify"
branch_labels = None
depends_on = None

_TABLES = (("agent_jobs", "job"), ("agent_threads", "thread"))


def upgrade() -> None:
    """Add ``revision`` to jobs and threads, and notify their owner on change."""
    for table, _ in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN revision BIGINT NOT NULL DEFAULT 0")
    op.execute(
        """
        CREATE FUNCTION agent_bump_revision() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.revision := OLD.revision + 1;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_notify_owner_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            changed RECORD;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                changed := OLD;
            ELSE
                changed := NEW;
            END IF;
            PERFORM pg_notify(
                'agent_owner_changes',
                json_build_object(
                    'user', changed.user_id, 'kind', TG_ARGV[0], 'id', changed.id
                )::text
            );
            RETURN NULL;
        END;
        $$
        """
    )
    for table, kind in _TABLES:
        # Only a change bumps it: an UPDATE that rewrites a row to the same
        # values notifies nobody and leaves the revision where it was.
        op.execute(
            f"""
            CREATE TRIGGER {table}_bump_revision
                BEFORE UPDATE ON {table}
                FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*)
                EXECUTE FUNCTION agent_bump_revision()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_owner_inserted
                AFTER INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION agent_notify_owner_change('{kind}')
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_owner_changed
                AFTER UPDATE ON {table}
                FOR EACH ROW WHEN (OLD.revision IS DISTINCT FROM NEW.revision)
                EXECUTE FUNCTION agent_notify_owner_change('{kind}')
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table}_owner_deleted
                AFTER DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION agent_notify_owner_change('{kind}')
            """
        )


def downgrade() -> None:
    """Remove the triggers, their functions and the counters."""
    for table, _ in _TABLES:
        for trigger in ("owner_deleted", "owner_changed", "owner_inserted", "bump_revision"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_{trigger} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS agent_notify_owner_change()")
    op.execute("DROP FUNCTION IF EXISTS agent_bump_revision()")
    for table, _ in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS revision")
