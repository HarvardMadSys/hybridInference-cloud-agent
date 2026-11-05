"""Record the complete retained-sandbox lifecycle as an append-only trajectory.

Revision ID: 0046_admin_sandbox_trajectory
Revises: 0045_admin_sandbox_deletion
"""

from alembic import op

revision = "0046_admin_sandbox_trajectory"
down_revision = "0045_admin_sandbox_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an append-only lifecycle ledger after archive/delete bookkeeping."""
    op.execute(
        """
        CREATE TABLE agent_sandbox_events (
            id BIGSERIAL PRIMARY KEY,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes (id) ON DELETE RESTRICT,
            generation BIGINT NOT NULL CHECK (generation > 0),
            event_type TEXT NOT NULL
                CHECK (char_length(event_type) BETWEEN 1 AND 128),
            from_state TEXT,
            to_state TEXT,
            source TEXT NOT NULL
                CHECK (source IN ('system', 'admin', 'host', 'migration')),
            actor_id TEXT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(details) = 'object'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_sandbox_events_sandbox "
        "ON agent_sandbox_events (sandbox_id, id DESC)"
    )
    op.execute(
        """
        CREATE FUNCTION agent_record_sandbox_lifecycle_event()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, to_state, source, details, created_at
                ) VALUES (
                    NEW.id, NEW.generation, 'created', NEW.state, 'system',
                    jsonb_build_object(
                        'snapshot_state', NEW.snapshot_state,
                        'container_present', NEW.container_id IS NOT NULL,
                        'inventory_archived', NEW.archived_at IS NOT NULL,
                        'deletion_state', NEW.deletion_state
                    ),
                    NEW.created_at
                );
                RETURN NEW;
            END IF;

            IF OLD.state IS DISTINCT FROM NEW.state THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, from_state, to_state, source
                ) VALUES (
                    NEW.id, NEW.generation, 'state_changed', OLD.state, NEW.state, 'system'
                );
            END IF;
            IF OLD.snapshot_state IS DISTINCT FROM NEW.snapshot_state THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, from_state, to_state, source
                ) VALUES (
                    NEW.id, NEW.generation, 'snapshot_state_changed',
                    OLD.snapshot_state, NEW.snapshot_state, 'system'
                );
            END IF;
            IF OLD.container_id IS DISTINCT FROM NEW.container_id THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, source, details
                ) VALUES (
                    NEW.id, NEW.generation, 'container_changed', 'system',
                    jsonb_build_object(
                        'previous_present', OLD.container_id IS NOT NULL,
                        'current_present', NEW.container_id IS NOT NULL
                    )
                );
            END IF;
            IF OLD.archived_at IS DISTINCT FROM NEW.archived_at THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, from_state, to_state, source
                ) VALUES (
                    NEW.id,
                    NEW.generation,
                    'inventory_visibility_changed',
                    CASE WHEN OLD.archived_at IS NULL THEN 'visible' ELSE 'archived' END,
                    CASE WHEN NEW.archived_at IS NULL THEN 'visible' ELSE 'archived' END,
                    'system'
                );
            END IF;
            IF OLD.deletion_state IS DISTINCT FROM NEW.deletion_state THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, from_state, to_state, source
                ) VALUES (
                    NEW.id,
                    NEW.generation,
                    'deletion_state_changed',
                    OLD.deletion_state,
                    NEW.deletion_state,
                    'system'
                );
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_sandbox_lifecycle_event
        AFTER INSERT OR UPDATE OF state, snapshot_state, container_id, archived_at, deletion_state
        ON agent_sandboxes
        FOR EACH ROW EXECUTE FUNCTION agent_record_sandbox_lifecycle_event()
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_reject_sandbox_event_mutation()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'agent_sandbox_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_sandbox_events_append_only
        BEFORE UPDATE OR DELETE ON agent_sandbox_events
        FOR EACH ROW EXECUTE FUNCTION agent_reject_sandbox_event_mutation()
        """
    )
    # Existing rows have no trustworthy transition history. Record exactly
    # what this migration observed instead of inventing a creation sequence.
    op.execute(
        """
        INSERT INTO agent_sandbox_events (
            sandbox_id, generation, event_type, to_state, source, details
        )
        SELECT id, generation, 'current_state_imported', state, 'migration',
               jsonb_strip_nulls(jsonb_build_object(
                   'snapshot_state', snapshot_state,
                   'container_present', container_id IS NOT NULL,
                   'inventory_archived', archived_at IS NOT NULL,
                   'archived_at', archived_at,
                   'deletion_state', deletion_state,
                   'deletion_requested_at', deletion_requested_at,
                   'deleted_at', deleted_at,
                   'original_created_at', created_at
               ))
        FROM agent_sandboxes
        """
    )


def downgrade() -> None:
    """Remove lifecycle history without changing sandbox archive/delete state."""
    op.execute("DROP TRIGGER IF EXISTS trg_agent_sandbox_lifecycle_event ON agent_sandboxes")
    op.execute("DROP FUNCTION IF EXISTS agent_record_sandbox_lifecycle_event()")
    op.execute("DROP TABLE IF EXISTS agent_sandbox_events")
    op.execute("DROP FUNCTION IF EXISTS agent_reject_sandbox_event_mutation()")
