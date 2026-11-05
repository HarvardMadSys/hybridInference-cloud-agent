"""Keep exact profiling counts in 64 transactional shards per run.

Revision ID: 0098_measurement_counts
Revises: 0097_admin_sandbox_deltas
"""

from alembic import op

revision = "0098_measurement_counts"
down_revision = "0097_admin_sandbox_deltas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill once while serializing writes, then account for actual row changes."""
    op.execute("""
        LOCK TABLE agent_operation_measurements IN SHARE ROW EXCLUSIVE MODE;
        CREATE TABLE agent_operation_measurement_counts (
            profiling_run_id TEXT NOT NULL,
            shard INTEGER NOT NULL CHECK (shard BETWEEN 0 AND 63),
            stored_count BIGINT NOT NULL CHECK (stored_count >= 0),
            PRIMARY KEY (profiling_run_id, shard)
        );
        INSERT INTO agent_operation_measurement_counts
        SELECT profiling_run_id,
               mod(get_byte(decode(md5(measurement_id),'hex'),0),64),
               count(*)
        FROM agent_operation_measurements GROUP BY 1,2;
        CREATE FUNCTION agent_count_measurement() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                UPDATE agent_operation_measurement_counts SET stored_count=stored_count-1
                WHERE profiling_run_id=OLD.profiling_run_id
                  AND shard=mod(get_byte(decode(md5(OLD.measurement_id),'hex'),0),64);
            END IF;
            IF TG_OP <> 'DELETE' THEN
                INSERT INTO agent_operation_measurement_counts(profiling_run_id,shard,stored_count)
                VALUES (NEW.profiling_run_id,
                    mod(get_byte(decode(md5(NEW.measurement_id),'hex'),0),64), 1)
                ON CONFLICT (profiling_run_id,shard) DO UPDATE
                SET stored_count=agent_operation_measurement_counts.stored_count+1;
            END IF;
            RETURN NULL;
        END $$;
        CREATE TRIGGER trg_agent_measurement_count_insert AFTER INSERT ON agent_operation_measurements
            FOR EACH ROW EXECUTE FUNCTION agent_count_measurement();
        CREATE TRIGGER trg_agent_measurement_count_delete AFTER DELETE ON agent_operation_measurements
            FOR EACH ROW EXECUTE FUNCTION agent_count_measurement();
        CREATE TRIGGER trg_agent_measurement_count_update AFTER UPDATE OF measurement_id,profiling_run_id
            ON agent_operation_measurements FOR EACH ROW EXECUTE FUNCTION agent_count_measurement();
    """)


def downgrade() -> None:
    """Remove derived counts without deleting or changing any measurement."""
    op.execute("""
        DROP TRIGGER trg_agent_measurement_count_insert ON agent_operation_measurements;
        DROP TRIGGER trg_agent_measurement_count_delete ON agent_operation_measurements;
        DROP TRIGGER trg_agent_measurement_count_update ON agent_operation_measurements;
        DROP FUNCTION agent_count_measurement();
        DROP TABLE agent_operation_measurement_counts;
    """)
