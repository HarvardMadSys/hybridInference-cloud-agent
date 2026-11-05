"""Maintain an indexed owner-scoped projection of eligible recent requests.

Revision ID: 0093_recent_requests
Revises: 0092_targeted_work_wakes
"""

from alembic import op

revision = "0093_recent_requests"
down_revision = "0092_targeted_work_wakes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Populate the projection and maintain it for old and new application writers."""
    op.execute("""
        CREATE TABLE agent_recent_requests (
            message_id BIGINT PRIMARY KEY REFERENCES agent_thread_messages(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            thread_id TEXT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX idx_agent_recent_requests_owner
            ON agent_recent_requests(user_id, created_at DESC, message_id DESC);
        CREATE INDEX idx_agent_recent_requests_thread ON agent_recent_requests(thread_id);
        CREATE FUNCTION agent_project_recent_request() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- Assistant/tool output never contributes to insights; avoid a
            -- thread lock and eligibility joins on that higher-volume path.
            IF NEW.role <> 'user' THEN
                DELETE FROM agent_recent_requests WHERE message_id=NEW.id;
                RETURN NEW;
            END IF;
            -- Serialize with soft-delete/restore so a concurrent message is
            -- neither resurrected after deletion nor lost during restoration.
            PERFORM 1 FROM agent_threads WHERE id=NEW.thread_id FOR SHARE;
            DELETE FROM agent_recent_requests WHERE message_id = NEW.id;
            INSERT INTO agent_recent_requests(message_id,user_id,thread_id,created_at)
            SELECT NEW.id,j.user_id,NEW.thread_id,NEW.created_at
            FROM agent_jobs j JOIN agent_threads t ON t.id=j.thread_id AND t.user_id=j.user_id
            WHERE j.id=NEW.job_id AND j.thread_id=NEW.thread_id AND t.deleted_at IS NULL
              AND NEW.role='user' AND j.fork_source_job_id IS NULL
              AND (j.turn_source <> 'goal' OR NEW.mid_turn);
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_agent_recent_request AFTER INSERT OR UPDATE OF
            role,job_id,thread_id,created_at,mid_turn ON agent_thread_messages
            FOR EACH ROW EXECUTE FUNCTION agent_project_recent_request();
        CREATE FUNCTION agent_project_recent_thread() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM agent_recent_requests WHERE thread_id=NEW.id;
            IF NEW.deleted_at IS NULL THEN
                INSERT INTO agent_recent_requests(message_id,user_id,thread_id,created_at)
                SELECT m.id,j.user_id,m.thread_id,m.created_at
                FROM agent_thread_messages m JOIN agent_jobs j
                  ON j.id=m.job_id AND j.thread_id=m.thread_id
                WHERE m.thread_id=NEW.id AND j.user_id=NEW.user_id
                  AND m.role='user' AND j.fork_source_job_id IS NULL
                  AND (j.turn_source <> 'goal' OR m.mid_turn)
                ON CONFLICT (message_id) DO NOTHING;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_agent_recent_thread AFTER UPDATE OF deleted_at ON agent_threads
            FOR EACH ROW WHEN (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
            EXECUTE FUNCTION agent_project_recent_thread();
        INSERT INTO agent_recent_requests(message_id,user_id,thread_id,created_at)
        SELECT m.id,j.user_id,m.thread_id,m.created_at
        FROM agent_thread_messages m
        JOIN agent_jobs j ON j.id=m.job_id AND j.thread_id=m.thread_id
        JOIN agent_threads t ON t.id=m.thread_id AND t.user_id=j.user_id
        WHERE t.deleted_at IS NULL AND m.role='user' AND j.fork_source_job_id IS NULL
          AND (j.turn_source <> 'goal' OR m.mid_turn);
    """)


def downgrade() -> None:
    """Remove the derived projection without changing source conversations."""
    op.execute("""
        DROP TRIGGER trg_agent_recent_thread ON agent_threads;
        DROP TRIGGER trg_agent_recent_request ON agent_thread_messages;
        DROP FUNCTION agent_project_recent_thread();
        DROP FUNCTION agent_project_recent_request();
        DROP TABLE agent_recent_requests;
    """)
