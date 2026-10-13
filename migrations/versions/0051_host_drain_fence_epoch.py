"""Fence host drain ownership across token reuse and old-writer updates.

Revision ID: 0051_host_drain_fence_epoch
Revises: 0051_web_search_admission
"""

from alembic import op

revision = "0051_host_drain_fence_epoch"
down_revision = "0051_web_search_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a database-owned generation to the published V1 drain fence."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
        ADD COLUMN drain_fence_epoch UUID NOT NULL DEFAULT gen_random_uuid(),
        DROP CONSTRAINT ck_agent_runner_host_drain_fence,
        ADD CONSTRAINT ck_agent_runner_host_drain_fence CHECK (
            (
                drain_fence_token IS NULL
                AND drain_fence_status_before IS NULL
                AND NOT drain_fence_active
            )
            OR (
                drain_fence_token IS NOT NULL
                AND drain_fence_status_before IS NOT NULL
                AND drain_fence_status_before IN ('online', 'draining')
                AND (NOT drain_fence_active OR status = 'draining')
            )
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_rotate_host_drain_fence_epoch() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status IS DISTINCT FROM OLD.status
                OR NEW.credential_hash IS DISTINCT FROM OLD.credential_hash
                OR NEW.drain_fence_token IS DISTINCT FROM OLD.drain_fence_token
                OR NEW.drain_fence_status_before IS DISTINCT FROM OLD.drain_fence_status_before
                OR NEW.drain_fence_active IS DISTINCT FROM OLD.drain_fence_active
                OR NEW.compute_node_id IS DISTINCT FROM OLD.compute_node_id
            THEN
                NEW.drain_fence_epoch := gen_random_uuid();
            ELSE
                -- This generation belongs to the database. Ignore attempts to
                -- choose one directly when none of the state it fences changed.
                NEW.drain_fence_epoch := OLD.drain_fence_epoch;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_runner_hosts_rotate_drain_fence_epoch
            BEFORE UPDATE ON agent_runner_hosts
            FOR EACH ROW EXECUTE FUNCTION agent_rotate_host_drain_fence_epoch()
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_guard_active_host_drain_fence_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.drain_fence_active THEN
                RAISE EXCEPTION 'active host drain fence prevents deletion'
                    USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_runner_hosts_guard_active_drain_fence_delete
            BEFORE DELETE ON agent_runner_hosts
            FOR EACH ROW EXECUTE FUNCTION agent_guard_active_host_drain_fence_delete()
        """
    )


def downgrade() -> None:
    """Restore the exact V1 fence shape and its stricter receipt constraint."""
    op.execute(
        "DROP TRIGGER IF EXISTS agent_runner_hosts_guard_active_drain_fence_delete "
        "ON agent_runner_hosts"
    )
    op.execute("DROP FUNCTION IF EXISTS agent_guard_active_host_drain_fence_delete()")
    op.execute(
        "DROP TRIGGER IF EXISTS agent_runner_hosts_rotate_drain_fence_epoch ON agent_runner_hosts"
    )
    op.execute("DROP FUNCTION IF EXISTS agent_rotate_host_drain_fence_epoch()")
    # V1 permits inactive receipts only while online. Clear receipts that an
    # old writer moved to another status before restoring that exact constraint.
    op.execute(
        """
        UPDATE agent_runner_hosts
        SET drain_fence_token = NULL, drain_fence_status_before = NULL
        WHERE NOT drain_fence_active
          AND drain_fence_token IS NOT NULL
          AND status <> 'online'
        """
    )
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
        DROP CONSTRAINT ck_agent_runner_host_drain_fence,
        ADD CONSTRAINT ck_agent_runner_host_drain_fence CHECK (
            (
                drain_fence_token IS NULL
                AND drain_fence_status_before IS NULL
                AND NOT drain_fence_active
            )
            OR (
                drain_fence_token IS NOT NULL
                AND drain_fence_status_before IS NOT NULL
                AND drain_fence_status_before IN ('online', 'draining')
                AND (
                    (drain_fence_active AND status = 'draining')
                    OR (NOT drain_fence_active AND status = 'online')
                )
            )
        ),
        DROP COLUMN drain_fence_epoch
        """
    )
