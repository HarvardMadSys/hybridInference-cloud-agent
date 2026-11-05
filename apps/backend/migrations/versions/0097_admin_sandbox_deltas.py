"""Track admin inventory changes without serializing unrelated writers.

Revision 0097 follows the separately reviewed snapshot-title migration. A
per-entity marker coalesces heartbeats; xid8 watermarks do not assume that
sequence allocation order is transaction commit order. Snapshot pages contain
only the explicit, content-free admin projection, never source table records.
"""

from alembic import op

revision = "0097_admin_sandbox_deltas"
down_revision = "0096_snapshot_title_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_admin_sandbox_changes (
            kind text NOT NULL CHECK (kind IN ('sandbox','host')),
            entity_id text NOT NULL,
            change_xid xid8 NOT NULL,
            changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (kind,entity_id)
        );
        CREATE INDEX idx_agent_admin_sandbox_changes_xid
            ON agent_admin_sandbox_changes (change_xid,kind,entity_id);
        CREATE INDEX idx_agent_admin_sandbox_changes_age
            ON agent_admin_sandbox_changes (changed_at);
        CREATE TABLE agent_admin_sandbox_change_floor (
            id boolean PRIMARY KEY DEFAULT true CHECK (id),
            floor_xid xid8 NOT NULL DEFAULT '0'
        );
        INSERT INTO agent_admin_sandbox_change_floor (id) VALUES (true);
        CREATE TABLE agent_admin_sandbox_inventory_snapshots (
            id text PRIMARY KEY,
            scope text NOT NULL,
            observed_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            database_snapshot pg_snapshot NOT NULL,
            total_count bigint NOT NULL
        );
        CREATE INDEX idx_agent_admin_sandbox_inventory_expiry
            ON agent_admin_sandbox_inventory_snapshots (expires_at);
        CREATE TABLE agent_admin_sandbox_inventory_pages (
            snapshot_id text NOT NULL REFERENCES agent_admin_sandbox_inventory_snapshots(id)
                ON DELETE CASCADE,
            page integer NOT NULL,
            sandboxes jsonb NOT NULL,
            PRIMARY KEY (snapshot_id,page)
        );
        CREATE FUNCTION agent_mark_admin_sandbox_change(kind text, entity text)
        RETURNS void LANGUAGE SQL AS $$
            INSERT INTO agent_admin_sandbox_changes (kind,entity_id,change_xid)
            VALUES (kind,entity,pg_current_xact_id())
            ON CONFLICT (kind,entity_id) DO UPDATE SET
                -- A transaction with a smaller XID may write this entity later.
                -- Store the last writer, not the numerically greatest XID.
                change_xid=EXCLUDED.change_xid,
                changed_at=clock_timestamp()
        $$;
        CREATE FUNCTION agent_track_admin_sandbox_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
                PERFORM agent_mark_admin_sandbox_change('sandbox',OLD.id);
            END IF;
            PERFORM agent_mark_admin_sandbox_change('sandbox',COALESCE(NEW.id,OLD.id));
            RETURN NULL;
        END $$;
        CREATE TRIGGER trg_agent_admin_sandbox_change
            AFTER INSERT OR UPDATE OR DELETE ON agent_sandboxes
            FOR EACH ROW EXECUTE FUNCTION agent_track_admin_sandbox_change();
        CREATE FUNCTION agent_track_admin_membership_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND
                (NEW.sandbox_id,NEW.resident_slot_id,NEW.active_attempt_id) IS NOT DISTINCT FROM
                (OLD.sandbox_id,OLD.resident_slot_id,OLD.active_attempt_id) THEN
                RETURN NULL;
            END IF;
            IF TG_OP<>'INSERT' THEN
                PERFORM agent_mark_admin_sandbox_change('sandbox',OLD.sandbox_id);
            END IF;
            IF TG_OP<>'DELETE' THEN
                PERFORM agent_mark_admin_sandbox_change('sandbox',NEW.sandbox_id);
            END IF;
            RETURN NULL;
        END $$;
        CREATE TRIGGER trg_agent_admin_membership_change
            AFTER INSERT OR UPDATE OR DELETE ON agent_thread_sandbox_memberships
            FOR EACH ROW EXECUTE FUNCTION agent_track_admin_membership_change();
        CREATE FUNCTION agent_track_admin_origin_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE sandbox text;
        BEGIN
            SELECT id INTO sandbox FROM agent_sandboxes WHERE origin_thread_id=NEW.id;
            IF sandbox IS NOT NULL THEN
                PERFORM agent_mark_admin_sandbox_change('sandbox',sandbox);
            END IF;
            RETURN NULL;
        END $$;
        CREATE TRIGGER trg_agent_admin_origin_change
            AFTER UPDATE OF title,title_source,repo ON agent_threads
            FOR EACH ROW WHEN ((OLD.title,OLD.title_source,OLD.repo) IS DISTINCT FROM
                               (NEW.title,NEW.title_source,NEW.repo))
            EXECUTE FUNCTION agent_track_admin_origin_change();
        CREATE FUNCTION agent_track_admin_host_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='UPDATE' AND (NEW.host,NEW.last_heartbeat_at) IS NOT DISTINCT FROM
                (OLD.host,OLD.last_heartbeat_at) THEN
                RETURN NULL;
            END IF;
            IF TG_OP='UPDATE' AND NEW.host IS DISTINCT FROM OLD.host THEN
                PERFORM agent_mark_admin_sandbox_change('host',OLD.host);
            END IF;
            PERFORM agent_mark_admin_sandbox_change('host',COALESCE(NEW.host,OLD.host));
            RETURN NULL;
        END $$;
        CREATE TRIGGER trg_agent_admin_host_change
            AFTER INSERT OR UPDATE OR DELETE ON agent_runner_hosts
            FOR EACH ROW EXECUTE FUNCTION agent_track_admin_host_change();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER trg_agent_admin_host_change ON agent_runner_hosts;
        DROP TRIGGER trg_agent_admin_origin_change ON agent_threads;
        DROP TRIGGER trg_agent_admin_membership_change ON agent_thread_sandbox_memberships;
        DROP TRIGGER trg_agent_admin_sandbox_change ON agent_sandboxes;
        DROP FUNCTION agent_track_admin_host_change();
        DROP FUNCTION agent_track_admin_origin_change();
        DROP FUNCTION agent_track_admin_membership_change();
        DROP FUNCTION agent_track_admin_sandbox_change();
        DROP FUNCTION agent_mark_admin_sandbox_change(text,text);
        DROP TABLE agent_admin_sandbox_inventory_pages;
        DROP TABLE agent_admin_sandbox_inventory_snapshots;
        DROP TABLE agent_admin_sandbox_change_floor;
        DROP TABLE agent_admin_sandbox_changes;
    """)
