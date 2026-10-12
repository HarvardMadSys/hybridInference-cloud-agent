"""Store administrator-managed Host deployment profiles and encrypted inputs."""

from alembic import op

revision = "0038_compute_deployment_profiles"
down_revision = "0037_admin_sandbox_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_compute_deployment_profiles (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            configuration JSONB NOT NULL,
            source_bundle BYTEA NOT NULL,
            source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[a-f0-9]{64}$'),
            secret_ciphertext TEXT NOT NULL,
            revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (jsonb_typeof(configuration) = 'object')
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_compute_deployment_profiles")
