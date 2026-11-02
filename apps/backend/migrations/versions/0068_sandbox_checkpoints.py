"""Record restorable sandbox checkpoints reported by host heartbeats."""

from alembic import op

revision = "0068_sandbox_checkpoints"
down_revision = "0067_failed_snapshot_wake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add one immutable receipt per checkpoint; nothing reads it for restore yet."""
    op.execute(
        """
        CREATE TABLE agent_sandbox_checkpoints (
            id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes(id) ON DELETE CASCADE,
            generation BIGINT NOT NULL CHECK (generation > 0),
            host TEXT NOT NULL,
            container_id TEXT NOT NULL,
            base_image TEXT NOT NULL,
            runtime_id TEXT NOT NULL,
            runtime_version TEXT,
            object_key TEXT NOT NULL,
            object_version_id TEXT,
            sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
            new_layer_bytes BIGINT NOT NULL CHECK (new_layer_bytes >= 0),
            layer_count INTEGER NOT NULL CHECK (layer_count > 0),
            members JSONB NOT NULL DEFAULT '[]'::jsonb,
            durations_ms JSONB NOT NULL DEFAULT '{}'::jsonb,
            format_version INTEGER NOT NULL CHECK (format_version > 0),
            state TEXT NOT NULL DEFAULT 'reported'
                CHECK (state IN ('reported', 'verified', 'superseded', 'deleted')),
            captured_at TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_sandbox_checkpoints_latest
            ON agent_sandbox_checkpoints (sandbox_id, captured_at DESC)
        """
    )


def downgrade() -> None:
    """Drop the receipts; the S3 objects they name are left in place."""
    op.execute("DROP TABLE agent_sandbox_checkpoints")
