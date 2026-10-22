"""Persist coalesced owner changes for incremental browser notifications.

Revision ID: 0101_notification_state_deltas
Revises: 0100_memory_archive_pages
"""

from __future__ import annotations

from alembic import context, op

revision = "0101_notification_state_deltas"
down_revision = "0100_memory_archive_pages"
branch_labels = None
depends_on = None

_ACTIVE_INDEX = "idx_agent_jobs_notification_active"
_ACTIVE_DEFINITION = (
    f"{_ACTIVE_INDEX} ON agent_jobs (user_id,id) WHERE state IN ('queued','running')"
)


def _build_active_index() -> None:
    """Build over lifetime job history without blocking job writes."""
    if context.is_offline_mode():
        op.execute(f"DROP INDEX IF EXISTS {_ACTIVE_INDEX}")
        op.execute(f"CREATE INDEX {_ACTIVE_DEFINITION}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_ACTIVE_INDEX}")
        op.execute(f"CREATE INDEX CONCURRENTLY {_ACTIVE_DEFINITION}")


def _drop_active_index() -> None:
    if context.is_offline_mode():
        op.execute(f"DROP INDEX IF EXISTS {_ACTIVE_INDEX}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_ACTIVE_INDEX}")


_NOTIFY_FUNCTION = """
CREATE OR REPLACE FUNCTION agent_notify_owner_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    changed RECORD;
    durable BOOLEAN := TRUE;
    terminal_job BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        changed := OLD;
    ELSE
        changed := NEW;
    END IF;
    IF TG_OP = 'INSERT' AND TG_ARGV[0] = 'job' THEN
        durable := NEW.fork_source_job_id IS NULL;
    ELSIF TG_OP = 'DELETE' AND TG_ARGV[0] = 'job' THEN
        durable := OLD.fork_source_job_id IS NULL;
    ELSIF TG_OP = 'UPDATE' AND TG_ARGV[0] = 'thread' THEN
        durable := OLD.user_id IS DISTINCT FROM NEW.user_id
            OR OLD.title IS DISTINCT FROM NEW.title
            OR OLD.repo IS DISTINCT FROM NEW.repo
            OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at;
    ELSIF TG_OP = 'UPDATE' AND TG_ARGV[0] = 'job' THEN
        durable := OLD.fork_source_job_id IS DISTINCT FROM NEW.fork_source_job_id
            OR (NEW.fork_source_job_id IS NULL AND (
                OLD.user_id IS DISTINCT FROM NEW.user_id
                OR OLD.thread_id IS DISTINCT FROM NEW.thread_id
                OR OLD.state IS DISTINCT FROM NEW.state
                OR OLD.current_attempt_id IS DISTINCT FROM NEW.current_attempt_id
                OR OLD.cancel_requested IS DISTINCT FROM NEW.cancel_requested
            ));
    END IF;
    IF TG_ARGV[0] = 'job' THEN
        IF TG_OP <> 'DELETE' THEN
            terminal_job := NEW.fork_source_job_id IS NULL
                AND NEW.state IN ('succeeded','failed','cancelled');
        END IF;
    END IF;
    IF durable THEN
        INSERT INTO agent_notification_changes (
            kind,entity_id,user_id,change_xid,changed_at,terminal
        ) VALUES (
            TG_ARGV[0],changed.id,changed.user_id,pg_current_xact_id(),
            clock_timestamp(),terminal_job
        )
        ON CONFLICT (kind,entity_id) DO UPDATE SET
            user_id=EXCLUDED.user_id,
            change_xid=EXCLUDED.change_xid,
            changed_at=EXCLUDED.changed_at,
            terminal=EXCLUDED.terminal;
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

_ORIGINAL_NOTIFY_FUNCTION = """
CREATE OR REPLACE FUNCTION agent_notify_owner_change() RETURNS trigger
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


def upgrade() -> None:
    """Add indexed, coalesced changes without copying historical jobs."""
    _build_active_index()
    op.execute(
        """
        CREATE TABLE agent_notification_changes (
            kind TEXT NOT NULL CHECK (kind IN ('job','thread')),
            entity_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            change_xid xid8 NOT NULL,
            changed_at TIMESTAMPTZ NOT NULL,
            terminal BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY (kind,entity_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_notification_changes_owner_xid
            ON agent_notification_changes (user_id,change_xid,kind,entity_id)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_notification_changes_owner_recent
            ON agent_notification_changes (user_id,changed_at DESC,entity_id DESC)
            WHERE kind='job' AND terminal
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_notification_changes_retention
            ON agent_notification_changes (changed_at,kind,entity_id)
        """
    )
    op.execute(
        """
        CREATE TABLE agent_notification_change_floors (
            user_id TEXT PRIMARY KEY,
            floor_xid xid8 NOT NULL
        )
        """
    )
    op.execute(_NOTIFY_FUNCTION)


def downgrade() -> None:
    """Restore ephemeral push-only notifications and discard derived cursors."""
    _drop_active_index()
    op.execute(_ORIGINAL_NOTIFY_FUNCTION)
    op.execute("DROP TABLE IF EXISTS agent_notification_change_floors")
    op.execute("DROP TABLE IF EXISTS agent_notification_changes")
